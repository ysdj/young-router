from __future__ import annotations

from . import image_generation as _image_generation_module
from . import responses_execution as _responses_execution_module
from . import responses_surfaces as _responses_surfaces_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import tools as _tools_module


from .base import (
    Any,
    Dict,
    List,
    Optional,
    _INTERNAL_CONTEXT_PREFIXES,
    _RESPONSES_BRIDGE_NAMESPACE_KEY,
    _RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY,
    _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
    _RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY,
    _RESPONSES_IMAGE_INPUT_SUPPORT_KEY,
    _ROUTE_TRACE_ENV,
    _ROUTE_TRACE_LIST_SCAN_ITEMS,
    _ROUTE_TRACE_LOGGER,
    _ROUTE_TRACE_PREVIEW_CHARS_ENV,
    _ROUTE_TRACE_PREVIEW_DEFAULT_CHARS,
    _ROUTE_TRACE_PREVIEW_MAX_CHARS,
    _ROUTE_TRACE_STATE_FILE_ENV,
    _STREAM_ERROR_FALLBACK_METADATA_KEY,
    _STREAM_FALLBACK_METADATA_KEY,
    _STREAM_IDLE_TIMEOUT_METADATA_KEY,
    _STREAM_START_TIMEOUT_METADATA_KEY,
    _SUPPORTED_UPSTREAM_URL_SURFACES_KEY,
    _SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY,
    _SUPPORTS_RESPONSES_HOSTED_TOOLS_KEY,
    _SUPPORTS_RESPONSES_WEB_SEARCH_KEY,
    _SUPPORTS_WEB_SEARCH_KEY,
    _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES,
    _UPSTREAM_URL_SURFACE_KEY,
    _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
    _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
    _WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY,
    _WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY,
    _XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY,
    datetime,
    json,
    os,
    re,
    timezone,
)



def _route_trace_bool(value: Any) -> bool:
    value = str(value or "").strip().lower()
    return value in {"1", "true", "yes", "on", "debug"}


def _route_trace_state_token(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.splitlines()[0].strip()


def _route_trace_enabled() -> bool:
    state_file = os.getenv(_ROUTE_TRACE_STATE_FILE_ENV, "").strip()
    if state_file:
        try:
            with open(state_file, "r", encoding="utf-8") as handle:
                return _route_trace_bool(_route_trace_state_token(handle.read()))
        except FileNotFoundError:
            pass
        except OSError:
            return False

    return _route_trace_bool(os.getenv(_ROUTE_TRACE_ENV, ""))


def _clean_trace_text(value: str, *, limit: int = 320) -> tuple[str, bool]:
    clean = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***", str(value or ""))
    clean = re.sub(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}", "Bearer ***", clean)
    clean = re.sub(
        r"data:image/[^;,]+;base64,[A-Za-z0-9+/=]+",
        "data:image/...;base64,<redacted>",
        clean,
    )
    clean = " ".join(clean.split())
    return clean[:limit], len(clean) > limit


def _sanitize_trace_text(value: str, *, limit: int = 320) -> str:
    return _clean_trace_text(value, limit=limit)[0]


def _trace_preview_limit() -> int:
    value = os.getenv(_ROUTE_TRACE_PREVIEW_CHARS_ENV, "")
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = _ROUTE_TRACE_PREVIEW_DEFAULT_CHARS
    return max(80, min(limit, _ROUTE_TRACE_PREVIEW_MAX_CHARS))


def _trace_block_kind(role: str, text: str) -> str:
    stripped = str(text or "").strip()
    lowered = stripped.lower()
    if any(lowered.startswith(prefix) for prefix in _INTERNAL_CONTEXT_PREFIXES):
        return "internal_context"
    if role.lower() in {"user", "human"}:
        return "user_request"
    if role:
        return role.lower()
    return "text"


def _trace_text_blocks(
    value: Any,
    *,
    role: Optional[str] = None,
    blocks: Optional[list[dict[str, Any]]] = None,
    depth: int = 0,
) -> list[dict[str, Any]]:
    if blocks is None:
        blocks = []
    if value is None or depth > 7 or len(blocks) >= 40:
        return blocks

    if isinstance(value, str):
        text, truncated = _clean_trace_text(value, limit=_trace_preview_limit())
        if text:
            block: dict[str, Any] = {
                "role": role or "",
                "text": text,
                "kind": _trace_block_kind(role or "", text),
            }
            if truncated:
                block["truncated"] = True
            blocks.append(block)
        return blocks

    if isinstance(value, list):
        items = value[-_ROUTE_TRACE_LIST_SCAN_ITEMS:]
        for item in items:
            _trace_text_blocks(item, role=role, blocks=blocks, depth=depth + 1)
            if len(blocks) >= 40:
                break
        return blocks

    if not isinstance(value, dict):
        return blocks

    next_role = value.get("role") if isinstance(value.get("role"), str) else role
    value_type = value.get("type")
    if (
        isinstance(value_type, str)
        and value_type in {"input_image", "image_url"}
    ) or "image_url" in value:
        blocks.append(
            {
                "role": next_role or "",
                "text": "[image input]",
                "kind": _trace_block_kind(next_role or "", "[image input]"),
            }
        )
        return blocks
    if (
        isinstance(value_type, str)
        and value_type in {"input_file", "file"}
    ) or "file_id" in value:
        blocks.append(
            {
                "role": next_role or "",
                "text": "[file input]",
                "kind": _trace_block_kind(next_role or "", "[file input]"),
            }
        )
        return blocks

    for key in ("content", "text", "input", "message"):
        if key in value:
            _trace_text_blocks(
                value.get(key),
                role=next_role,
                blocks=blocks,
                depth=depth + 1,
            )
            if len(blocks) >= 40:
                break
    return blocks


def _trace_tool_types(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    tool_types: list[str] = []
    for tool in tools[:20]:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if isinstance(tool_type, str) and tool_type not in tool_types:
            tool_types.append(tool_type)
    return tool_types


def _trace_tool_names(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    tool_names: list[str] = []
    def append_name(name: Any) -> None:
        if (
            isinstance(name, str)
            and name
            and name not in tool_names
            and len(tool_names) < 40
        ):
            tool_names.append(name)

    def visit_tool(tool: Any, depth: int = 0) -> None:
        if not isinstance(tool, dict):
            return
        if depth > 2 or len(tool_names) >= 40:
            return
        name: Optional[str] = None
        if tool.get("type") == "function":
            function = tool.get("function")
            function_dict = function if isinstance(function, dict) else {}
            candidate = function_dict.get("name") or tool.get("name")
            name = candidate if isinstance(candidate, str) else None
        else:
            candidate = tool.get("name")
            name = candidate if isinstance(candidate, str) else None
        append_name(name)
        child_tools = tool.get("tools")
        if isinstance(child_tools, list):
            for child_tool in child_tools[:40]:
                visit_tool(child_tool, depth + 1)
                if len(tool_names) >= 40:
                    break

    for tool in tools[:80]:
        visit_tool(tool)
        if len(tool_names) >= 40:
            break
    return tool_names


def _trace_request_preview(
    request_kwargs: Optional[dict],
    *,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    source = "unknown"
    source_value: Any = None
    if request_kwargs.get("input") is not None:
        source = "input"
        source_value = request_kwargs.get("input")
    elif messages is not None:
        source = "messages"
        source_value = messages
    elif request_kwargs.get("messages") is not None:
        source = "messages"
        source_value = request_kwargs.get("messages")

    blocks = _trace_text_blocks(source_value)
    limit = _trace_preview_limit()
    user_blocks = [
        block
        for block in blocks
        if block.get("role", "").lower() in {"user", "human"}
        and block.get("kind") == "user_request"
    ]
    internal_context_blocks = [
        block for block in blocks if block.get("kind") == "internal_context"
    ]
    latest_user_block = user_blocks[-1] if user_blocks else {}
    latest_user = str(latest_user_block.get("text") or "")
    latest_user_truncated = bool(latest_user_block.get("truncated"))
    tail_blocks = blocks[-3:]
    preview_source = " | ".join(
        f"{block['role']}: {block['text']}" if block.get("role") else block["text"]
        for block in tail_blocks
    )
    preview, preview_join_truncated = _clean_trace_text(preview_source, limit=limit)
    preview_truncated = preview_join_truncated or any(
        bool(block.get("truncated")) for block in tail_blocks
    )

    return {
        "source": source,
        "latest_user": latest_user,
        "latest_user_kind": latest_user_block.get("kind") if latest_user_block else None,
        "latest_user_truncated": latest_user_truncated,
        "preview": preview,
        "preview_truncated": preview_truncated,
        "preview_limit": limit,
        "scan_direction": "tail" if isinstance(source_value, list) else None,
        "scan_item_limit": _ROUTE_TRACE_LIST_SCAN_ITEMS
        if isinstance(source_value, list)
        else None,
        "text_block_count": len(blocks),
        "internal_context_block_count": len(internal_context_blocks),
        "message_count": len(source_value) if isinstance(source_value, list) else None,
        "tool_types": _trace_tool_types(request_kwargs.get("tools")),
        "tool_choice": _sanitize_trace_text(str(request_kwargs.get("tool_choice")), limit=120)
        if request_kwargs.get("tool_choice") is not None
        else None,
    }


def _trace_text_length(value: Any, *, depth: int = 0) -> int:
    if value is None or depth > 8:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_trace_text_length(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        return sum(_trace_text_length(item, depth=depth + 1) for item in value.values())
    return 0


def _trace_content_part_types(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    types: list[str] = []
    for part in value[:40]:
        part_type: Optional[str] = None
        if isinstance(part, dict):
            candidate = part.get("type")
            if isinstance(candidate, str) and candidate.strip():
                part_type = candidate.strip()
        elif isinstance(part, str):
            part_type = "text"
        else:
            part_type = type(part).__name__
        if part_type and part_type not in types:
            types.append(part_type)
    return types


def _trace_responses_input_shape(request_kwargs: Optional[dict]) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    source = request_kwargs.get("input")
    if source is None:
        source = request_kwargs.get("messages")
    if not isinstance(source, list):
        return {"kind": type(source).__name__ if source is not None else None}

    top_type_counts: dict[str, int] = {}
    role_counts: dict[str, int] = {}
    content_type_counts: dict[str, int] = {}
    part_type_counts: dict[str, int] = {}
    tool_output_count = 0
    tool_call_count = 0
    status_counts: dict[str, int] = {}
    text_lengths: list[int] = []
    sample_items: list[dict[str, Any]] = []

    def add_count(counts: dict[str, int], key: Any) -> None:
        label = str(key).strip() if key is not None else "<missing>"
        if not label:
            label = "<empty>"
        counts[label] = counts.get(label, 0) + 1

    for index, item in enumerate(source):
        if isinstance(item, dict):
            item_type = item.get("type")
            role = item.get("role")
            content = item.get("content")
            status = item.get("status")
            content_kind = type(content).__name__ if content is not None else None
            text_len = _trace_text_length(content)
            add_count(top_type_counts, item_type)
            if role is not None:
                add_count(role_counts, role)
            if content_kind is not None:
                add_count(content_type_counts, content_kind)
            for part_type in _trace_content_part_types(content):
                add_count(part_type_counts, part_type)
            if isinstance(status, str):
                add_count(status_counts, status)
            if (
                isinstance(item_type, str)
                and item_type in {"function_call_output", "tool_output", "tool_result"}
            ) or role == "tool":
                tool_output_count += 1
            if _trace_tool_call_item(item) is not None:
                tool_call_count += 1
            if text_len:
                text_lengths.append(text_len)
            if index < 8 or index >= max(8, len(source) - 8):
                sample: dict[str, Any] = {
                    "index": index,
                    "type": item_type,
                    "role": role,
                    "content_kind": content_kind,
                    "text_len": text_len,
                }
                part_types = _trace_content_part_types(content)
                if part_types:
                    sample["part_types"] = part_types
                if isinstance(status, str):
                    sample["status"] = status
                sample_items.append(sample)
        else:
            add_count(top_type_counts, type(item).__name__)
            text_len = _trace_text_length(item)
            if text_len:
                text_lengths.append(text_len)
            if index < 8 or index >= max(8, len(source) - 8):
                sample_items.append(
                    {
                        "index": index,
                        "type": type(item).__name__,
                        "text_len": text_len,
                    }
                )

    text_lengths_sorted = sorted(text_lengths)
    total_text_len = sum(text_lengths)
    longest_text_len = max(text_lengths) if text_lengths else 0
    p95_text_len = (
        text_lengths_sorted[min(len(text_lengths_sorted) - 1, int(len(text_lengths_sorted) * 0.95))]
        if text_lengths_sorted
        else 0
    )
    return {
        "kind": "list",
        "item_count": len(source),
        "top_type_counts": top_type_counts,
        "role_counts": role_counts,
        "content_type_counts": content_type_counts,
        "content_part_type_counts": part_type_counts,
        "status_counts": status_counts,
        "tool_call_count": tool_call_count,
        "tool_output_count": tool_output_count,
        "total_text_len": total_text_len,
        "longest_text_len": longest_text_len,
        "p95_text_len": p95_text_len,
        "sample_items": sample_items,
        "sample_truncated_middle": len(source) > len(sample_items),
    }


def _trace_limited_value(value: Any, *, limit: int = 240, depth: int = 0) -> Any:
    value = _streaming_module._jsonable(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _sanitize_trace_text(value, limit=limit)
    if depth >= 4:
        return _sanitize_trace_text(str(value), limit=limit)
    if isinstance(value, list):
        return [
            _trace_limited_value(item, limit=limit, depth=depth + 1)
            for item in value[:20]
        ]
    if isinstance(value, dict):
        limited: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 30:
                limited["_truncated"] = True
                break
            limited[str(key)] = _trace_limited_value(
                item,
                limit=limit,
                depth=depth + 1,
            )
        return limited
    return _sanitize_trace_text(str(value), limit=limit)


def _trace_function_name(value: Any) -> Optional[str]:
    for attr in ("__qualname__", "__name__"):
        name = getattr(value, attr, None)
        if isinstance(name, str) and name.strip():
            return _sanitize_trace_text(name.strip(), limit=160)
    return None


def _trace_proxy_request_values(request_kwargs: Optional[dict]) -> list[str]:
    request_kwargs = request_kwargs or {}
    values: list[str] = []
    containers: list[Any] = [request_kwargs]
    for key in ("litellm_params", "litellm_metadata", "metadata"):
        container = request_kwargs.get(key)
        if isinstance(container, dict):
            containers.append(container)

    for container in containers:
        if not isinstance(container, dict):
            continue
        proxy_request = container.get("proxy_server_request")
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
            if isinstance(value, str) and value.strip() and value not in values:
                values.append(value.strip())
    return values


def _trace_requested_endpoint(request_kwargs: Optional[dict]) -> Optional[str]:
    for value in _trace_proxy_request_values(request_kwargs):
        lowered = value.lower()
        if "/v1/images/generations" in lowered:
            return "/v1/images/generations"
        if "/v1/responses" in lowered:
            return "/v1/responses"
        if "/v1/chat/completions" in lowered:
            return "/v1/chat/completions"
        if "/v1/completions" in lowered:
            return "/v1/completions"
    return None


def _trace_client_surface(request_kwargs: Optional[dict]) -> str:
    request_kwargs = request_kwargs or {}
    endpoint = _trace_requested_endpoint(request_kwargs)
    call_type = request_kwargs.get("call_type")
    if endpoint == "/v1/images/generations" or (
        isinstance(call_type, str)
        and call_type.lower() in {"image_generation", "aimage_generation"}
    ):
        return "image_generation"
    if endpoint == "/v1/responses" or _image_generation_module._request_is_responses_api(request_kwargs):
        return "responses"
    if endpoint in {"/v1/chat/completions", "/v1/completions"}:
        return "chat"
    if request_kwargs.get("input") is not None:
        return "responses"
    if request_kwargs.get("messages") is not None:
        return "chat"
    return "unknown"


def _trace_effective_upstream_surface(request_kwargs: Optional[dict]) -> str:
    request_kwargs = request_kwargs or {}
    if _trace_client_surface(request_kwargs) == "image_generation":
        return "image_generation"
    if request_kwargs.get("use_chat_completions_api") is True:
        return "chat"
    metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    if metadata.get(_RESPONSES_CHAT_BRIDGE_METADATA_KEY) is True:
        return "chat"
    if _responses_surfaces_module._request_uses_responses_endpoint(request_kwargs):
        return "responses"
    model_info = _image_generation_module._request_model_info(request_kwargs)
    surface = model_info.get(_UPSTREAM_URL_SURFACE_KEY)
    if surface == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        return "responses"
    if surface in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        return "chat"
    return _trace_client_surface(request_kwargs)


def _trace_interface_summary(
    request_kwargs: Optional[dict],
    *,
    call_type: Any = None,
    method_name: Any = None,
) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    model_info = _image_generation_module._request_model_info(request_kwargs)
    api_base = _image_generation_module._request_api_base(request_kwargs)
    call_type_value = call_type if call_type is not None else request_kwargs.get("call_type")
    method_value = method_name if method_name is not None else None
    if method_value is None:
        method_value = _trace_function_name(request_kwargs.get("original_generic_function"))
    supported_surfaces = model_info.get(_SUPPORTED_UPSTREAM_URL_SURFACES_KEY)
    return {
        "client_surface": _trace_client_surface(request_kwargs),
        "effective_upstream_surface": _trace_effective_upstream_surface(request_kwargs),
        "requested_endpoint": _trace_requested_endpoint(request_kwargs),
        "call_type": _sanitize_trace_text(str(call_type_value), limit=80)
        if call_type_value is not None
        else None,
        "method_name": _sanitize_trace_text(str(method_value), limit=160)
        if method_value is not None
        else None,
        "stream": request_kwargs.get("stream") is True,
        "is_responses_api": _image_generation_module._request_is_responses_api(request_kwargs),
        "use_chat_completions_api": request_kwargs.get("use_chat_completions_api") is True,
        "api_base_host": _image_generation_module._api_base_host(api_base),
        "upstream_url_surface": model_info.get(_UPSTREAM_URL_SURFACE_KEY),
        "supported_upstream_url_surfaces": _trace_limited_value(supported_surfaces, limit=120),
        "supports_responses_image_input": model_info.get(_RESPONSES_IMAGE_INPUT_SUPPORT_KEY),
        "supports_responses_hosted_tools": model_info.get(
            _SUPPORTS_RESPONSES_HOSTED_TOOLS_KEY
        ),
        "supports_responses_client_tools": model_info.get(
            _SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY
        ),
        "supports_responses_web_search": model_info.get(_SUPPORTS_RESPONSES_WEB_SEARCH_KEY),
        "supports_web_search": model_info.get(_SUPPORTS_WEB_SEARCH_KEY),
    }


def _trace_reasoning_summary(request_kwargs: Optional[dict]) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    reasoning = request_kwargs.get("reasoning")
    reasoning_dict = reasoning if isinstance(reasoning, dict) else {}
    text_config = request_kwargs.get("text")
    text_dict = text_config if isinstance(text_config, dict) else {}
    effort = request_kwargs.get("reasoning_effort") or reasoning_dict.get("effort")
    summary = {
        "present": reasoning is not None or request_kwargs.get("reasoning_effort") is not None,
        "effort": _sanitize_trace_text(str(effort), limit=80) if effort is not None else None,
        "reasoning": _trace_limited_value(reasoning, limit=240)
        if reasoning is not None
        else None,
        "reasoning_effort": _sanitize_trace_text(
            str(request_kwargs.get("reasoning_effort")),
            limit=80,
        )
        if request_kwargs.get("reasoning_effort") is not None
        else None,
        "text_verbosity": text_dict.get("verbosity"),
    }
    return summary


def _trace_generation_summary(request_kwargs: Optional[dict]) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    keys = (
        "temperature",
        "top_p",
        "max_output_tokens",
        "max_tokens",
        "max_completion_tokens",
        "parallel_tool_calls",
        "response_format",
        "service_tier",
        "truncation",
        "seed",
    )
    return {
        key: _trace_limited_value(request_kwargs.get(key), limit=200)
        for key in keys
        if request_kwargs.get(key) is not None
    }


def _trace_exposed_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    exposed: list[dict[str, Any]] = []
    def visit_tool(tool: Any, inherited_namespace: Optional[str] = None, depth: int = 0) -> None:
        if not isinstance(tool, dict):
            return
        if depth > 2 or len(exposed) >= 40:
            return
        tool_type = tool.get("type")
        function = tool.get("function")
        function_dict = function if isinstance(function, dict) else {}
        name = function_dict.get("name") or tool.get("name")
        namespace = tool.get(_RESPONSES_BRIDGE_NAMESPACE_KEY) or tool.get("namespace")
        if not isinstance(namespace, str) or not namespace.strip():
            namespace = inherited_namespace
        item = {
            "type": tool_type if isinstance(tool_type, str) else None,
            "name": name if isinstance(name, str) else None,
        }
        if isinstance(namespace, str) and namespace.strip():
            item["namespace"] = namespace.strip()
        exposed.append(item)
        child_namespace = namespace
        if tool_type == "namespace" and isinstance(name, str) and name.strip():
            child_namespace = name.strip()
        child_tools = tool.get("tools")
        if isinstance(child_tools, list):
            for child_tool in child_tools[:40]:
                visit_tool(child_tool, child_namespace, depth + 1)
                if len(exposed) >= 40:
                    break

    for tool in tools[:40]:
        visit_tool(tool)
        if len(exposed) >= 40:
            break
    return exposed


def _trace_tools_summary(request_kwargs: Optional[dict]) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    tools = request_kwargs.get("tools")
    tool_count = len(tools) if isinstance(tools, list) else 0
    return {
        "count": tool_count,
        "types": _trace_tool_types(tools),
        "names": _trace_tool_names(tools),
        "exposed": _trace_exposed_tools(tools),
        "tool_choice": _trace_limited_value(request_kwargs.get("tool_choice"), limit=160)
        if request_kwargs.get("tool_choice") is not None
        else None,
        "parallel_tool_calls": request_kwargs.get("parallel_tool_calls"),
        "has_web_search_tool": _tools_module._request_has_web_search_tool(request_kwargs),
        "has_litellm_web_search_bridge": _tools_module._request_has_litellm_web_search_bridge(
            request_kwargs
        ),
        "has_image_generation_tool": _tools_module._request_has_image_generation_tool(request_kwargs),
        "has_image_input": _image_generation_module._request_has_image_input(request_kwargs),
        "has_web_search_options": "web_search_options" in request_kwargs,
    }


def _trace_metadata_flags(request_kwargs: Optional[dict]) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    flag_keys = (
        _RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
        _RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY,
        _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
        _RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY,
        _STREAM_FALLBACK_METADATA_KEY,
        _STREAM_ERROR_FALLBACK_METADATA_KEY,
        _STREAM_IDLE_TIMEOUT_METADATA_KEY,
        _STREAM_START_TIMEOUT_METADATA_KEY,
        _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
        _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
        _WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY,
        _WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY,
        _XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY,
        "codex_compaction_optimized",
        "codex_compaction_max_output_tokens",
        "external_web_search_synthesis",
        "external_web_search_continuation",
        "responses_function_tool_bridge_preemptive_reason",
        _RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY,
        "responses_chat_bridge_preemptive_reason",
    )
    flags: dict[str, Any] = {}
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, metadata_key) or {}
        for key in flag_keys:
            if key in metadata:
                flags[key] = _trace_limited_value(metadata.get(key), limit=160)
    return flags


def _trace_request_summary(
    request_kwargs: Optional[dict],
    *,
    messages: Optional[List[Dict[str, Any]]] = None,
    call_type: Any = None,
    method_name: Any = None,
) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    return {
        "model": request_kwargs.get("model"),
        "model_group": _responses_execution_module._request_model_group(request_kwargs),
        "deployment_id": _routing_module._deployment_id_from_request(request_kwargs),
        "route_key": _routing_module._deployment_route_key_from_request(request_kwargs),
        "target_order": _image_generation_module._request_target_order(request_kwargs),
        "excluded_deployment_ids": sorted(_image_generation_module._request_excluded_deployment_ids(request_kwargs)),
        "preview": _trace_request_preview(request_kwargs, messages=messages),
        "input_shape": _trace_responses_input_shape(request_kwargs),
        "interface": _trace_interface_summary(
            request_kwargs,
            call_type=call_type,
            method_name=method_name,
        ),
        "reasoning": _trace_reasoning_summary(request_kwargs),
        "generation": _trace_generation_summary(request_kwargs),
        "tools": _trace_tools_summary(request_kwargs),
        "metadata_flags": _trace_metadata_flags(request_kwargs),
    }


_TRACE_TOOL_CALL_TYPES = {
    "function",
    "function_call",
    "custom_tool_call",
    "tool_call",
    "tool_search_call",
    "web_search_call",
    "image_generation_call",
    "computer_call",
}


def _trace_tool_call_name(item: dict[str, Any], item_type: str) -> Optional[str]:
    function = item.get("function")
    function_dict = function if isinstance(function, dict) else {}
    for candidate in (
        item.get("name"),
        function_dict.get("name"),
        item.get("tool_name"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return _sanitize_trace_text(candidate.strip(), limit=160)
    if item_type == "web_search_call":
        return "web_search"
    if item_type == "image_generation_call":
        return "image_generation"
    if item_type == "computer_call":
        return "computer"
    return None


def _trace_tool_call_arguments(item: dict[str, Any]) -> Any:
    function = item.get("function")
    function_dict = function if isinstance(function, dict) else {}
    for key in ("arguments", "input", "parameters"):
        if item.get(key) is not None:
            value = item.get(key)
            break
    else:
        value = function_dict.get("arguments")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                return json.loads(stripped)
            except Exception:
                return stripped
        return ""
    return value

def _trace_web_search_action(item: dict[str, Any]) -> dict[str, Any]:
    action = item.get("action")
    if not isinstance(action, dict):
        return {}
    summary: dict[str, Any] = {}
    action_type = action.get("type")
    if isinstance(action_type, str) and action_type.strip():
        summary["type"] = action_type.strip()
    for key in ("query", "url"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            summary[key] = _sanitize_trace_text(value.strip(), limit=240)
    return summary


def _trace_tool_call_item(item: Any) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    function = item.get("function")
    if item_type == "function" and isinstance(function, dict):
        is_chat_tool_call = any(item.get(key) is not None for key in ("id", "call_id"))
        if not is_chat_tool_call:
            return None
    if not isinstance(item_type, str) or item_type not in _TRACE_TOOL_CALL_TYPES:
        if not (
            isinstance(function, dict)
            and function.get("name")
            and any(item.get(key) is not None for key in ("id", "call_id"))
        ):
            return None
        item_type = "function"
    item_type = str(item_type)
    call: dict[str, Any] = {
        "type": item_type,
        "name": _trace_tool_call_name(item, item_type),
    }
    for key in ("id", "call_id", "tool_call_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            call[key] = _sanitize_trace_text(value.strip(), limit=160)
    status = item.get("status")
    if isinstance(status, str) and status.strip():
        call["status"] = status.strip()
    namespace = item.get("namespace")
    if isinstance(namespace, str) and namespace.strip():
        call["namespace"] = namespace.strip()
    arguments = _trace_tool_call_arguments(item)
    if arguments not in (None, "", {}, []):
        call["arguments_preview"] = _trace_limited_value(arguments, limit=260)
    web_action = _trace_web_search_action(item)
    if web_action:
        call["action"] = web_action
    return call


def _trace_tool_call_summary(
    response: Any,
    request_kwargs: Optional[dict] = None,
    *,
    max_calls: int = 20,
) -> dict[str, Any]:
    payload = _streaming_module._jsonable(response)
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    truncated = False

    def add(item: Any) -> None:
        nonlocal truncated
        call = _trace_tool_call_item(item)
        if call is None:
            return
        key = json.dumps(call, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            return
        seen.add(key)
        if len(calls) >= max_calls:
            truncated = True
            return
        calls.append(call)

    def visit_message(message: Any, depth: int) -> None:
        if not isinstance(message, dict) or depth > 8:
            return
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for item in tool_calls:
                add(item)
        function_call = message.get("function_call")
        if isinstance(function_call, dict):
            add({"type": "function_call", **function_call})

    def visit(item: Any, depth: int = 0) -> None:
        if item is None or depth > 8:
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if not isinstance(item, dict):
            return

        add(item)
        if isinstance(item.get("item"), dict):
            add(item.get("item"))
            visit(item.get("item"), depth + 1)
        output = item.get("output")
        if isinstance(output, list):
            visit(output, depth + 1)
        response_payload = item.get("response")
        if isinstance(response_payload, dict):
            visit(response_payload, depth + 1)
        choices = item.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                visit_message(choice.get("message"), depth + 1)
                visit_message(choice.get("delta"), depth + 1)
        visit_message(item.get("message"), depth + 1)
        if isinstance(item.get("tool_calls"), list):
            for child in item["tool_calls"]:
                add(child)
        if isinstance(item.get("function_call"), dict):
            add({"type": "function_call", **item["function_call"]})

    visit(payload)
    if request_kwargs is not None:
        for call in _responses_web_search_bridge_module._litellm_web_search_function_calls(response):
            add(call)

    types: list[str] = []
    names: list[str] = []
    for call in calls:
        call_type = call.get("type")
        if isinstance(call_type, str) and call_type not in types:
            types.append(call_type)
        name = call.get("name")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return {
        "count": len(calls),
        "types": types,
        "names": names,
        "calls": calls,
        "truncated": truncated,
    }


def _trace_response_summary(
    response: Any,
    request_kwargs: Optional[dict] = None,
) -> dict[str, Any]:
    summary = {
        "tool_calls": _trace_tool_call_summary(response, request_kwargs),
    }
    if request_kwargs is not None:
        actions = _responses_web_search_bridge_module._litellm_web_search_actions_for_request(response, request_kwargs)
        if actions:
            summary["web_search_actions"] = actions
    return summary


def _route_trace(event: str, **fields: Any) -> None:
    if not _route_trace_enabled():
        return
    try:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00",
            "Z",
        )
        payload = {"timestamp": timestamp, "event": event, **fields}
        _ROUTE_TRACE_LOGGER.warning(
            "litellm_route_trace %s",
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        )
    except Exception:
        pass
