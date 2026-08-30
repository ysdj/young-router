from __future__ import annotations

from . import request_context as _request_context_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import trace as _trace_module
from . import image_inputs as _image_inputs_module


from .base import (
    Any,
    Dict,
    List,
    Optional,
    _BROWSER_COMPATIBLE_HEADERS,
    _BROWSER_COMPATIBLE_HEADER_HOSTS,
    _BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY,
    _CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER,
    _CODEX_VIEW_IMAGE_REFERENCE_MARKER,
    _CHAT_COMPAT_REASONING_EFFORT,
    _FALLBACK_BROWSER_USER_AGENT,
    _MAX_COMPAT_REASONING_EFFORT,
    _PI_WEB_ACCESS_TOOL_NAMES,
    _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES,
    _HOSTED_TOOL_UNSUPPORTED_MESSAGE_KEY,
    _HOSTED_WEB_SEARCH_UNSUPPORTED_BRIDGE_KEY,
    _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
    _RESPONSES_CHAT_BRIDGE_EMPTY_RETRY_METADATA_KEY,
    _RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY,
    _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
    _RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY,
    _RESPONSES_CONTEXT_TRUNCATION_FALLBACK_METADATA_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY,
    _RouteOrder,
    _STREAM_ERROR_FALLBACK_METADATA_KEY,
    _STREAM_FALLBACK_METADATA_KEY,
    _UPSTREAM_METADATA_FORWARD_FLAGS,
    _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
    _WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY,
    _XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY,
    _XHIGH_REASONING_EFFORT,
    asyncio,
    copy,
    inspect,
    json,
    re,
    urlparse,
)





















def _request_is_responses_api(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    call_type = request_kwargs.get("call_type")
    if isinstance(call_type, str) and call_type.lower() in {"responses", "aresponses"}:
        return True

    original_generic_function = request_kwargs.get("original_generic_function")
    for attr in ("__name__", "__qualname__"):
        name = getattr(original_generic_function, attr, None)
        if isinstance(name, str) and name.lower() in {"responses", "aresponses"}:
            return True

    proxy_request_values: List[Any] = []
    containers: List[Any] = [request_kwargs]
    for key in ("litellm_params", "litellm_metadata", "metadata"):
        container = request_kwargs.get(key)
        if isinstance(container, dict):
            containers.append(container)
    for container in containers:
        if not isinstance(container, dict):
            continue
        proxy_request = container.get("proxy_server_request")
        if isinstance(proxy_request, dict):
            proxy_request_values.extend(
                proxy_request.get(key) for key in ("url", "path", "route", "endpoint")
            )
        else:
            proxy_request_values.extend(
                getattr(proxy_request, key, None)
                for key in ("url", "path", "route", "endpoint")
            )

    for value in proxy_request_values:
        if isinstance(value, str) and "/v1/responses" in value:
            return True
    return False


_RESPONSES_NATIVE_EXTRA_BODY_KEYS = (
    "client_metadata",
)


_CODEX_COMPACTION_UPSTREAM_HEADER_NAMES = (
    "Accept",
    "Originator",
    "Session-Id",
    "Thread-Id",
    "User-Agent",
    "X-Client-Request-Id",
    "X-Codex-Beta-Features",
    "X-Codex-Turn-Metadata",
    "X-Codex-Window-Id",
    "X-OpenAI-Internal-Codex-Responses-Lite",
)


def _agent_message_encrypted_part_is_plain_text(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    return any(character.isspace() or ord(character) > 127 for character in value)


def _with_plaintext_agent_message_content_restored(
    request_kwargs: dict,
) -> Optional[dict]:
    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list):
        return None

    updated_items: List[Any] = []
    changed = False
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            updated_items.append(item)
            continue
        content = item.get("content")
        if not isinstance(content, list):
            updated_items.append(item)
            continue

        updated_content: List[Any] = []
        item_changed = False
        for part in content:
            encrypted_content = (
                part.get("encrypted_content")
                if isinstance(part, dict) and part.get("type") == "encrypted_content"
                else None
            )
            if not _agent_message_encrypted_part_is_plain_text(encrypted_content):
                updated_content.append(part)
                continue
            updated_content.append(
                {
                    "type": "input_text",
                    "text": encrypted_content,
                }
            )
            item_changed = True

        if not item_changed:
            updated_items.append(item)
            continue
        updated_item = item.copy()
        updated_item["content"] = updated_content
        updated_items.append(updated_item)
        changed = True

    if not changed:
        return None
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["input"] = updated_items
    return modified_kwargs

def _request_has_responses_shape(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    return _request_is_responses_api(request_kwargs) or "input" in request_kwargs


def _with_responses_native_extra_body(request_kwargs: dict) -> Optional[dict]:
    if request_kwargs.get("use_chat_completions_api") is True:
        return None
    if not _request_has_responses_shape(request_kwargs):
        return None

    passthrough_values = {
        key: request_kwargs[key]
        for key in _RESPONSES_NATIVE_EXTRA_BODY_KEYS
        if key in request_kwargs and request_kwargs.get(key) is not None
    }
    if not passthrough_values:
        return None

    existing_extra_body = request_kwargs.get("extra_body")
    merged_extra_body = (
        existing_extra_body.copy() if isinstance(existing_extra_body, dict) else {}
    )
    changed = False
    for key, value in passthrough_values.items():
        if merged_extra_body.get(key) == value:
            continue
        merged_extra_body[key] = copy.deepcopy(value)
        changed = True

    if not changed:
        return None

    modified_kwargs = request_kwargs.copy()
    modified_kwargs["extra_body"] = merged_extra_body
    return modified_kwargs


def _codex_compaction_metadata_header_value(
    request_kwargs: Optional[dict],
    header_name: str,
) -> Optional[str]:
    request_kwargs = request_kwargs or {}
    client_metadata = request_kwargs.get("client_metadata")
    if not isinstance(client_metadata, dict):
        return None

    header_key = header_name.lower()
    if header_key == "session-id":
        value = client_metadata.get("session_id") or client_metadata.get("thread_id")
    elif header_key == "thread-id":
        value = client_metadata.get("thread_id")
    elif header_key == "x-client-request-id":
        value = client_metadata.get("thread_id") or client_metadata.get("session_id")
    elif header_key == "x-codex-turn-metadata":
        value = client_metadata.get("x-codex-turn-metadata")
    elif header_key == "x-codex-window-id":
        value = client_metadata.get("x-codex-window-id")
    else:
        value = None

    if isinstance(value, str) and value.strip():
        return value
    return None


def _codex_compaction_passthrough_headers(
    request_kwargs: Optional[dict],
    *,
    source_request_kwargs: Optional[dict] = None,
) -> Dict[str, str]:
    request_kwargs = request_kwargs or {}
    if request_kwargs.get("use_chat_completions_api") is True:
        return {}
    if not _request_has_responses_shape(request_kwargs):
        return {}
    if not _request_is_codex_compaction(request_kwargs):
        return {}

    source_request_kwargs = source_request_kwargs or {}
    header_sources = []
    if source_request_kwargs is not request_kwargs:
        header_sources.extend(_incoming_request_headers(source_request_kwargs))
    header_sources.extend(_incoming_request_headers(request_kwargs))

    metadata_sources = [request_kwargs]
    if source_request_kwargs and source_request_kwargs is not request_kwargs:
        metadata_sources.append(source_request_kwargs)

    passthrough_headers: Dict[str, str] = {}
    for header_name in _CODEX_COMPACTION_UPSTREAM_HEADER_NAMES:
        value = None
        for headers in header_sources:
            value = _header_value(headers, header_name)
            if value:
                break
        if value is None:
            for metadata_source in metadata_sources:
                value = _codex_compaction_metadata_header_value(metadata_source, header_name)
                if value:
                    break
        if value is not None:
            passthrough_headers[header_name] = value

    source_stream = (
        source_request_kwargs.get("stream") if isinstance(source_request_kwargs, dict) else None
    )
    if "Accept" not in passthrough_headers and (
        request_kwargs.get("stream") is True or source_stream is True
    ):
        passthrough_headers["Accept"] = "text/event-stream"
    passthrough_headers["Accept-Encoding"] = "identity"
    if "X-Codex-Beta-Features" not in passthrough_headers:
        passthrough_headers["X-Codex-Beta-Features"] = "remote_compaction_v2"

    return passthrough_headers


def _with_codex_compaction_headers_from_source(
    request_kwargs: dict,
    source_request_kwargs: Optional[dict] = None,
) -> Optional[dict]:
    passthrough_headers = _codex_compaction_passthrough_headers(
        request_kwargs,
        source_request_kwargs=source_request_kwargs,
    )
    if not passthrough_headers:
        return None

    existing_headers = request_kwargs.get("extra_headers")
    merged_headers: Dict[str, str] = (
        existing_headers.copy() if isinstance(existing_headers, dict) else {}
    )
    changed = False
    for header_name, value in passthrough_headers.items():
        existing_key = _header_key(merged_headers, header_name)
        if existing_key is None:
            merged_headers[header_name] = value
            changed = True
        elif merged_headers[existing_key] != value:
            merged_headers[existing_key] = value
            changed = True

    if not changed:
        return None

    modified_kwargs = request_kwargs.copy()
    modified_kwargs["extra_headers"] = merged_headers
    return modified_kwargs


def _with_codex_compaction_headers(request_kwargs: dict) -> Optional[dict]:
    return _with_codex_compaction_headers_from_source(request_kwargs)














def _request_api_base(request_kwargs: Optional[dict]) -> str:
    request_kwargs = request_kwargs or {}
    api_base = request_kwargs.get("api_base")
    if isinstance(api_base, str):
        return api_base
    litellm_params = request_kwargs.get("litellm_params")
    if isinstance(litellm_params, dict):
        api_base = litellm_params.get("api_base")
        if isinstance(api_base, str):
            return api_base
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs, metadata_key
        )
        if metadata is None:
            continue
        metadata_api_base = metadata.get("api_base")
        if isinstance(metadata_api_base, str):
            return metadata_api_base
    if isinstance(litellm_params, dict):
        for metadata_key in ("litellm_metadata", "metadata"):
            metadata = litellm_params.get(metadata_key)
            if not isinstance(metadata, dict):
                continue
            metadata_api_base = metadata.get("api_base")
            if isinstance(metadata_api_base, str):
                return metadata_api_base
    return ""


def _api_base_host(api_base: str) -> str:
    if not api_base:
        return ""
    parsed = urlparse(api_base if "://" in api_base else f"https://{api_base}")
    return (parsed.hostname or "").lower()


def _api_base_needs_browser_compatible_headers(api_base: str) -> bool:
    host = _api_base_host(api_base)
    return any(
        host == allowed or host.endswith(f".{allowed}")
        for allowed in _BROWSER_COMPATIBLE_HEADER_HOSTS
    )


def _request_forces_browser_compatible_headers(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    for container in (request_kwargs,):
        if not isinstance(container, dict):
            continue
        if container.get(_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY) is True:
            return True
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs, metadata_key
        )
        if (
            isinstance(metadata, dict)
            and metadata.get(_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY) is True
        ):
            return True
    return False


def _with_browser_compatible_headers_retry(request_kwargs: dict) -> Optional[dict]:
    if _request_forces_browser_compatible_headers(request_kwargs):
        return None
    modified_kwargs = request_kwargs.copy()
    modified_kwargs[_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY] = True
    metadata = _request_context_module._request_metadata_dict(modified_kwargs, "litellm_metadata") or {}
    modified_kwargs["litellm_metadata"] = metadata.copy()
    modified_kwargs["litellm_metadata"][_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY] = True
    return _with_browser_compatible_headers(modified_kwargs) or modified_kwargs


def _deployment_order(deployment: Any) -> Optional[_RouteOrder]:
    if not isinstance(deployment, dict):
        return None
    saw_defaultable_order = False
    saw_invalid_order = False
    for section_name in ("litellm_params", "model_info"):
        section = deployment.get(section_name)
        if not isinstance(section, dict):
            continue
        if "order" not in section or section.get("order") is None:
            saw_defaultable_order = True
            continue
        order = section.get("order")
        if isinstance(order, str) and not order.strip():
            saw_defaultable_order = True
            continue
        normalized = _routing_module._coerce_order(order)
        if normalized is not None:
            return normalized
        saw_invalid_order = True
    if saw_invalid_order:
        return None
    return 1 if saw_defaultable_order else None


def _request_target_order(request_kwargs: Optional[dict]) -> Optional[_RouteOrder]:
    request_kwargs = request_kwargs or {}
    return _routing_module._coerce_order(request_kwargs.get("_target_order"))


def _deployment_id(deployment: Any) -> Optional[str]:
    if not isinstance(deployment, dict):
        return None
    model_info = deployment.get("model_info")
    if not isinstance(model_info, dict):
        return None
    deployment_id = model_info.get("id")
    return deployment_id if isinstance(deployment_id, str) else None


def _request_excluded_deployment_ids(request_kwargs: Optional[dict]) -> set[str]:
    request_kwargs = request_kwargs or {}
    excluded = request_kwargs.get("_excluded_deployment_ids")
    if not isinstance(excluded, list):
        return set()
    return {item for item in excluded if isinstance(item, str)}


def _request_verified_fallback_deployment_ids(
    request_kwargs: Optional[dict],
) -> set[str]:
    request_kwargs = request_kwargs or {}
    deployment_ids = request_kwargs.get(_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY)
    if not isinstance(deployment_ids, list):
        return set()
    return {
        deployment_id
        for deployment_id in deployment_ids
        if isinstance(deployment_id, str) and deployment_id.strip()
    }


def _with_retry_target_constraints(
    deployments: List[dict],
    request_kwargs: Optional[dict],
) -> List[dict]:
    constrained = deployments
    target_order = _request_target_order(request_kwargs)
    if target_order is not None:
        constrained = [
            deployment
            for deployment in constrained
            if _deployment_order(deployment) == target_order
        ]

    excluded_ids = _request_excluded_deployment_ids(request_kwargs)
    if excluded_ids:
        constrained = [
            deployment
            for deployment in constrained
            if _deployment_id(deployment) not in excluded_ids
        ]

    verified_ids = _request_verified_fallback_deployment_ids(request_kwargs)
    if verified_ids:
        constrained = [
            deployment
            for deployment in constrained
            if _deployment_id(deployment) in verified_ids
        ]

    return constrained


async def _await_streaming_fallback_candidate_response(
    response: Any,
    request_kwargs: dict,
    outer_request_kwargs: Optional[dict] = None,
) -> Any:
    is_fallback_candidate = _request_is_fallback_attempt(
        request_kwargs
    ) or _request_is_fallback_attempt(outer_request_kwargs)
    if (
        request_kwargs.get("stream") is not True
        or not is_fallback_candidate
    ):
        if inspect.isawaitable(response):
            timeout_seconds = (
                _routing_module._stream_start_timeout_seconds_for_request(request_kwargs)
                if request_kwargs.get("stream") is True
                else 0.0
            )
            try:
                if timeout_seconds > 0:
                    return await asyncio.wait_for(response, timeout=timeout_seconds)
                return await response
            except Exception as exc:
                if isinstance(exc, asyncio.TimeoutError):
                    exc = _streaming_module._stream_start_timeout_exception(
                        request_kwargs,
                        start_seconds=timeout_seconds,
                        saw_chunk=False,
                        buffered_chunks=0,
                    )
                if _routing_module._is_request_scoped_priority_deployment_failover_error(
                    exc,
                    request_kwargs,
                ):
                    _routing_module._mark_exception_for_deployment_failover(exc, request_kwargs)
                raise exc
        return response

    timeout_seconds = _routing_module._stream_start_timeout_seconds_for_request(request_kwargs)
    try:
        if inspect.isawaitable(response):
            if timeout_seconds > 0:
                return await asyncio.wait_for(response, timeout=timeout_seconds)
            return await response
        return response
    except Exception as exc:
        if isinstance(exc, asyncio.TimeoutError):
            exc = _streaming_module._stream_start_timeout_exception(
                request_kwargs,
                start_seconds=timeout_seconds,
                saw_chunk=False,
                buffered_chunks=0,
            )
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            exc,
            request_kwargs,
        ):
            _routing_module._mark_exception_for_deployment_failover(exc, request_kwargs)
        raise exc


def _header_value(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if isinstance(value, str) and value.strip():
        return value

    lower_name = name.lower()
    if isinstance(headers, dict):
        for key, item in headers.items():
            if str(key).lower() == lower_name and isinstance(item, str) and item.strip():
                return item
        return None

    if isinstance(headers, list):
        for item in headers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            key, value = item
            if str(key).lower() == lower_name and isinstance(value, str) and value.strip():
                return value
    return None


def _incoming_request_headers(request_kwargs: Optional[dict]) -> List[Any]:
    request_kwargs = request_kwargs or {}
    headers: List[Any] = []
    header_sources: List[Any] = [request_kwargs]
    for container_key in ("litellm_params", "litellm_metadata", "metadata"):
        container = request_kwargs.get(container_key)
        if isinstance(container, dict):
            header_sources.append(container)
            nested_metadata = container.get("metadata")
            if isinstance(nested_metadata, dict):
                header_sources.append(nested_metadata)

    for source in header_sources:
        if not isinstance(source, dict):
            continue
        proxy_request = source.get("proxy_server_request")
        if isinstance(proxy_request, dict):
            headers.append(proxy_request.get("headers"))
        else:
            headers.append(getattr(proxy_request, "headers", None))

        for key in ("headers", "request_headers"):
            headers.append(source.get(key))
    return headers


def _incoming_request_user_agent(request_kwargs: Optional[dict]) -> Optional[str]:
    for headers in _incoming_request_headers(request_kwargs):
        user_agent = _header_value(headers, "User-Agent")
        if user_agent:
            return user_agent
    return None


def _header_key(headers: Dict[str, str], name: str) -> Optional[str]:
    lower_name = name.lower()
    for key in headers:
        if str(key).lower() == lower_name:
            return key
    return None


def _with_incoming_user_agent_header(request_kwargs: dict) -> Optional[dict]:
    incoming_user_agent = _incoming_request_user_agent(request_kwargs)
    if not incoming_user_agent:
        return None

    existing_headers = request_kwargs.get("extra_headers")
    merged_headers: Dict[str, str] = (
        existing_headers.copy() if isinstance(existing_headers, dict) else {}
    )
    user_agent_key = _header_key(merged_headers, "User-Agent")
    if user_agent_key is None:
        merged_headers["User-Agent"] = incoming_user_agent
    elif merged_headers[user_agent_key] == incoming_user_agent:
        return None
    else:
        merged_headers[user_agent_key] = incoming_user_agent

    modified_kwargs = request_kwargs.copy()
    modified_kwargs["extra_headers"] = merged_headers
    return modified_kwargs

def _is_browser_compatible_user_agent(value: Optional[str]) -> bool:
    return isinstance(value, str) and "mozilla/" in value.lower()


def _is_replaceable_default_user_agent(value: Optional[str]) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized.startswith(
        (
            "python-urllib/",
            "python-requests/",
            "curl/",
            "go-http-client/",
        )
    )


def _with_browser_compatible_headers(request_kwargs: dict) -> Optional[dict]:
    force_headers = _request_forces_browser_compatible_headers(request_kwargs)
    if not (force_headers or _api_base_needs_browser_compatible_headers(_request_api_base(request_kwargs))):
        return None

    existing_headers = request_kwargs.get("extra_headers")
    merged_headers: Dict[str, str] = (
        existing_headers.copy() if isinstance(existing_headers, dict) else {}
    )
    changed = False

    incoming_user_agent = _incoming_request_user_agent(request_kwargs)
    if force_headers and not _is_browser_compatible_user_agent(incoming_user_agent):
        incoming_user_agent = None
    browser_user_agent = incoming_user_agent or _FALLBACK_BROWSER_USER_AGENT
    user_agent_key = _header_key(merged_headers, "User-Agent")
    if user_agent_key is None:
        merged_headers["User-Agent"] = browser_user_agent
        changed = True
    elif force_headers and not _is_browser_compatible_user_agent(merged_headers[user_agent_key]):
        merged_headers[user_agent_key] = browser_user_agent
        changed = True
    elif _is_replaceable_default_user_agent(merged_headers[user_agent_key]):
        merged_headers[user_agent_key] = browser_user_agent
        changed = True

    for key, value in _BROWSER_COMPATIBLE_HEADERS.items():
        if _header_key(merged_headers, key) is not None:
            continue
        merged_headers[key] = value
        changed = True

    if not changed and existing_headers is request_kwargs.get("extra_headers"):
        return None

    modified_kwargs = request_kwargs.copy()
    modified_kwargs["extra_headers"] = merged_headers
    return modified_kwargs






def _request_allows_upstream_metadata(request_kwargs: Optional[dict]) -> bool:
    model_info = _request_context_module._request_model_info(request_kwargs)
    return any(model_info.get(flag) is True for flag in _UPSTREAM_METADATA_FORWARD_FLAGS)


def _with_internal_litellm_metadata(request_kwargs: dict) -> Optional[dict]:
    if "metadata" not in request_kwargs:
        return None

    if _request_allows_upstream_metadata(request_kwargs):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, "metadata")
        if metadata is None:
            return None
        modified_kwargs = request_kwargs.copy()
        litellm_metadata = _request_context_module._request_metadata_dict(modified_kwargs, "litellm_metadata") or {}
        merged_litellm_metadata = litellm_metadata.copy()
        merged_litellm_metadata.update(metadata)
        modified_kwargs["litellm_metadata"] = merged_litellm_metadata
        return modified_kwargs

    modified_kwargs = request_kwargs.copy()
    metadata = _request_context_module._request_metadata_dict(request_kwargs, "metadata")
    if metadata is not None:
        litellm_metadata = _request_context_module._request_metadata_dict(modified_kwargs, "litellm_metadata") or {}
        merged_litellm_metadata = litellm_metadata.copy()
        merged_litellm_metadata.update(metadata)
        modified_kwargs["litellm_metadata"] = merged_litellm_metadata
    modified_kwargs.pop("metadata", None)
    return modified_kwargs


def _with_mcp_auto_approval(request_kwargs: dict) -> Optional[dict]:
    """Disable interactive approval for Responses API MCP tool calls when enabled."""
    if not _routing_module._env_bool(_MCP_AUTO_APPROVE_ENV, False):
        return None
    if request_kwargs.get("use_chat_completions_api") is True:
        return None
    if not _request_has_responses_shape(request_kwargs):
        return None

    tools = request_kwargs.get("tools")
    if not isinstance(tools, list):
        return None

    updated_tools: list[Any] = []
    changed = False
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            updated_tools.append(tool)
            continue
        if tool.get("require_approval") == "never":
            updated_tools.append(tool)
            continue
        updated_tool = tool.copy()
        updated_tool["require_approval"] = "never"
        updated_tools.append(updated_tool)
        changed = True

    if not changed:
        return None
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["tools"] = updated_tools
    return modified_kwargs


def _with_empty_tool_controls_removed(request_kwargs: dict) -> Optional[dict]:
    if _request_is_codex_compaction(request_kwargs):
        return None

    tools = request_kwargs.get("tools")
    if (
        (isinstance(tools, list) and tools)
        or _request_has_leading_responses_additional_tools(request_kwargs)
    ):
        return None

    modified_kwargs = request_kwargs.copy()
    changed = False
    if isinstance(tools, list) and not tools:
        modified_kwargs.pop("tools", None)
        changed = True
    for key in ("tool_choice", "parallel_tool_calls"):
        if key in modified_kwargs:
            modified_kwargs.pop(key, None)
            changed = True
    return modified_kwargs if changed else None


def _request_has_leading_responses_additional_tools(
    request_kwargs: Optional[dict],
) -> bool:
    """Keep Responses tool controls until leading Codex tools are promoted.

    Codex may carry its client tools in one or more leading
    ``input: [{"type": "additional_tools", ...}]`` items while the
    top-level ``tools`` array is empty.  Those tools are promoted later by the
    Responses compatibility layer.  Treating the top-level array as empty
    before that promotion drops a valid custom ``tool_choice`` and its
    ``parallel_tool_calls`` setting.
    """
    if not isinstance(request_kwargs, dict):
        return False
    input_value = request_kwargs.get("input")
    if not isinstance(input_value, list):
        return False
    for item in input_value:
        if not isinstance(item, dict) or item.get("type") != "additional_tools":
            break
        if isinstance(item.get("tools"), list) and item["tools"]:
            return True
    return False




def _codex_turn_metadata_is_compaction(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return False
    if not isinstance(value, dict):
        return False
    request_kind = value.get("request_kind")
    return (
        isinstance(request_kind, str)
        and request_kind.strip().lower() == "compaction"
    )


def _codex_turn_metadata_has_request_kind(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return False
    if not isinstance(value, dict):
        return False
    request_kind = value.get("request_kind")
    return isinstance(request_kind, str) and bool(request_kind.strip())


def _codex_turn_metadata_values(
    request_kwargs: Optional[dict],
) -> List[Any]:
    if not isinstance(request_kwargs, dict):
        return []

    metadata_sources: List[Any] = [request_kwargs]
    for key in (
        "client_metadata",
        "litellm_metadata",
        "metadata",
        "extra_body",
        "litellm_params",
    ):
        value = request_kwargs.get(key)
        if isinstance(value, dict):
            metadata_sources.append(value)
            nested_client_metadata = value.get("client_metadata")
            if isinstance(nested_client_metadata, dict):
                metadata_sources.append(nested_client_metadata)
            nested_metadata = value.get("metadata")
            if isinstance(nested_metadata, dict):
                metadata_sources.append(nested_metadata)

    values: List[Any] = list(metadata_sources)
    for metadata in metadata_sources:
        if not isinstance(metadata, dict):
            continue
        for key in ("x-codex-turn-metadata", "X-Codex-Turn-Metadata"):
            if key in metadata:
                values.append(metadata.get(key))
    for headers in _incoming_request_headers(request_kwargs):
        value = _header_value(headers, "X-Codex-Turn-Metadata")
        if value is not None:
            values.append(value)
    return values


def _request_has_structured_codex_compaction(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False

    input_items = request_kwargs.get("input")
    return isinstance(input_items, list) and any(
        isinstance(item, dict) and item.get("type") == "compaction_trigger"
        for item in input_items
    )


def _request_has_explicit_codex_turn_kind(
    request_kwargs: Optional[dict],
) -> bool:
    return any(
        _codex_turn_metadata_has_request_kind(value)
        for value in _codex_turn_metadata_values(request_kwargs)
    )


def _request_is_codex_compaction(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if not _request_has_responses_shape(request_kwargs):
        return False
    if _request_has_structured_codex_compaction(request_kwargs):
        return True
    if any(
        _codex_turn_metadata_is_compaction(value)
        for value in _codex_turn_metadata_values(request_kwargs)
    ):
        return True
    if _request_has_explicit_codex_turn_kind(request_kwargs):
        return False
    if not _request_has_codex_client_evidence(request_kwargs):
        return False
    preview = _trace_module._trace_request_preview(request_kwargs)
    latest_user = str(preview.get("latest_user") or "").strip().lower()
    if not latest_user:
        return False
    return any(
        marker in latest_user
        for marker in (
            "context checkpoint compaction",
            "compact handoff summary",
            "create a handoff summary for another llm",
            "create a compact handoff summary for resuming this codex session",
        )
    )


def _with_codex_compaction_controls(request_kwargs: dict) -> Optional[dict]:
    if not _request_is_codex_compaction(request_kwargs):
        return None

    modified_kwargs = request_kwargs.copy()
    changed = False
    if modified_kwargs.pop("use_chat_completions_api", None) is not None:
        changed = True

    bridge_metadata_keys = {
        _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
        _RESPONSES_CHAT_BRIDGE_EMPTY_RETRY_METADATA_KEY,
        _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
        _RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY,
        _RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY,
        "responses_chat_bridge_preemptive_reason",
        "responses_chat_bridge_tool_sanitized",
        _RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
        _RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY,
        "responses_function_tool_bridge_preemptive_reason",
        "responses_function_tool_bridge_tool_sanitized",
        _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
        _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
        _WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY,
        _HOSTED_WEB_SEARCH_UNSUPPORTED_BRIDGE_KEY,
        _HOSTED_TOOL_UNSUPPORTED_MESSAGE_KEY,
    }
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(modified_kwargs, metadata_key)
        if not metadata:
            continue
        cleaned_metadata = metadata.copy()
        for key in bridge_metadata_keys:
            if key in cleaned_metadata:
                cleaned_metadata.pop(key, None)
                changed = True
        if cleaned_metadata != metadata:
            modified_kwargs[metadata_key] = cleaned_metadata

    return modified_kwargs if changed else None


def _request_already_attempted_responses_context_truncation_fallback(
    request_kwargs: Optional[dict],
) -> bool:
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, metadata_key)
        if (
            metadata is not None
            and metadata.get(
                _RESPONSES_CONTEXT_TRUNCATION_FALLBACK_METADATA_KEY
            )
            is True
        ):
            return True
    return False


def _explicit_responses_truncation(
    request_kwargs: Optional[dict],
) -> Any:
    if not isinstance(request_kwargs, dict):
        return None
    if request_kwargs.get("truncation") is not None:
        return request_kwargs.get("truncation")
    for container_key in ("extra_body", "litellm_params"):
        container = request_kwargs.get(container_key)
        if isinstance(container, dict) and container.get("truncation") is not None:
            return container.get("truncation")
    return None


def _request_disables_responses_truncation_fallback(
    request_kwargs: Optional[dict],
) -> bool:
    explicit_truncation = _explicit_responses_truncation(request_kwargs)
    if explicit_truncation is None:
        return False
    return str(explicit_truncation).strip().lower() != "auto"


def _responses_context_truncation_fallback_kwargs(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> Optional[dict]:
    """Retry one native Responses turn with the API's own truncation strategy.

    This is deliberately separate from Codex remote compaction.  A structured
    compaction request must be made valid before its first upstream call; an
    ordinary turn may use the Responses API's documented ``truncation=auto``
    compatibility fallback after the upstream establishes that the input is
    too large.  The caller invokes the selected deployment function directly,
    so this helper never asks the Router to choose another deployment.
    """
    if not isinstance(request_kwargs, dict):
        return None
    if not _request_has_responses_shape(request_kwargs):
        return None
    if request_kwargs.get("use_chat_completions_api") is True:
        return None
    if _request_is_codex_compaction(request_kwargs):
        return None
    if _request_disables_responses_truncation_fallback(request_kwargs):
        return None
    if _request_already_attempted_responses_context_truncation_fallback(
        request_kwargs
    ):
        return None
    if not _routing_module._is_context_size_error(exception):
        return None

    retry_kwargs = request_kwargs.copy()
    retry_kwargs["truncation"] = "auto"
    litellm_metadata = (
        _request_context_module._request_metadata_dict(retry_kwargs, "litellm_metadata") or {}
    )
    retry_metadata = litellm_metadata.copy()
    retry_metadata[_RESPONSES_CONTEXT_TRUNCATION_FALLBACK_METADATA_KEY] = True
    retry_kwargs["litellm_metadata"] = retry_metadata
    _trace_module._route_trace(
        "responses_context_truncation_fallback_start",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_request_context_module._request_model_group(
            request_kwargs
        ),
        deployment_id=_routing_module._deployment_id_from_request(
            request_kwargs
        ),
        route_key=_routing_module._deployment_route_key_from_request(
            request_kwargs
        ),
        request=_trace_module._trace_request_summary(request_kwargs),
        retry_request=_trace_module._trace_request_summary(retry_kwargs),
        exception=_routing_module._trace_exception(exception),
    )
    return retry_kwargs


def _request_can_attempt_responses_context_truncation_fallback(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if not _request_has_responses_shape(request_kwargs):
        return False
    if request_kwargs.get("use_chat_completions_api") is True:
        return False
    if _request_is_codex_compaction(request_kwargs):
        return False
    if _request_disables_responses_truncation_fallback(request_kwargs):
        return False
    return not _request_already_attempted_responses_context_truncation_fallback(
        request_kwargs
    )


def _request_has_codex_client_evidence(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False

    for headers in _incoming_request_headers(request_kwargs):
        for header_name in (
            "X-Codex-Turn-Metadata",
            "X-Codex-Window-Id",
            "X-Codex-Beta-Features",
            "X-Codex-Installation-Id",
        ):
            if _header_value(headers, header_name):
                return True
        for header_name in ("Originator", "User-Agent"):
            value = _header_value(headers, header_name)
            if isinstance(value, str) and "codex" in value.lower():
                return True

    for metadata_key in ("client_metadata", "litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, metadata_key)
        if not metadata:
            continue
        for key, value in metadata.items():
            key_text = str(key).lower()
            if key_text.startswith("x-codex-") and isinstance(value, str) and value.strip():
                return True
        for key in (
            "x-codex-turn-metadata",
            "x-codex-window-id",
            "x-codex-installation-id",
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return True
    return False


def _codex_tool_definition_name(tool: Any) -> Optional[str]:
    if not isinstance(tool, dict):
        return None
    function = tool.get("function")
    function_dict = function if isinstance(function, dict) else {}
    name = function_dict.get("name") or tool.get("name")
    return name if isinstance(name, str) and name.strip() else None


def _codex_declared_tools(request_kwargs: Optional[dict]) -> list[dict]:
    if not isinstance(request_kwargs, dict):
        return []
    tools = request_kwargs.get("tools")
    declared = [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []
    input_value = request_kwargs.get("input")
    if isinstance(input_value, list):
        for item in input_value:
            if not isinstance(item, dict) or item.get("type") != "additional_tools":
                break
            item_tools = item.get("tools")
            if isinstance(item_tools, list):
                declared.extend(
                    tool for tool in item_tools if isinstance(tool, dict)
                )
    return declared


_CODEX_OPENROUTER_NATIVE_WEB_SEARCH_METADATA_KEY = (
    "openrouter_native_web_search_injected"
)


def _request_is_openrouter_route(request_kwargs: Optional[dict]) -> bool:
    """Return whether the selected upstream is OpenRouter.

    Provider metadata is authoritative after deployment selection. The host
    and explicit custom-provider fields cover generic callbacks that LiteLLM
    rebuilds without the original model_info object.
    """

    if not isinstance(request_kwargs, dict):
        return False
    model_info = _request_context_module._request_model_info(request_kwargs)
    litellm_params = request_kwargs.get("litellm_params")
    if not isinstance(litellm_params, dict):
        litellm_params = {}

    def is_openrouter_name(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        normalized = value.strip().lower().rstrip("/")
        return normalized in {"openrouter", "openrouter.ai"}

    for value in (
        model_info.get("provider"),
        request_kwargs.get("custom_llm_provider"),
        litellm_params.get("custom_llm_provider"),
    ):
        if is_openrouter_name(value):
            return True

    host = _api_base_host(_request_api_base(request_kwargs))
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _codex_tool_is_web_search_declaration(tool: Any) -> bool:
    if not isinstance(tool, dict):
        return False
    if tool.get("type") in (
        {"web_search", "web_search_preview"} | _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES
    ):
        return True
    return _codex_tool_definition_name(tool) in {"web_search", "fetch_content"}


def _codex_tool_is_provider_native_web_search(tool: Any) -> bool:
    return (
        isinstance(tool, dict)
        and tool.get("type") in _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES
    )


def _codex_tool_is_hosted_web_search(tool: Any) -> bool:
    return (
        isinstance(tool, dict)
        and tool.get("type") in {"web_search", "web_search_preview"}
    )


def _codex_openrouter_native_search_request(
    request_kwargs: dict,
    _declared_tools: List[dict],
) -> tuple[dict, bool]:
    """Convert Codex hosted search declarations to OpenRouter's native type."""

    modified = request_kwargs.copy()
    top_level_tools = request_kwargs.get("tools")
    if not isinstance(top_level_tools, list):
        existing_extra_body = request_kwargs.get("extra_body")
        if isinstance(existing_extra_body, dict) and isinstance(
            existing_extra_body.get("tools"), list
        ):
            top_level_tools = existing_extra_body["tools"]
    updated_tools = (
        copy.deepcopy(top_level_tools)
        if isinstance(top_level_tools, list)
        else []
    )
    hosted_replaced = False
    for index, tool in enumerate(updated_tools):
        if _codex_tool_is_hosted_web_search(tool):
            updated_tools[index] = {"type": "openrouter:web_search"}
            hosted_replaced = True

    input_value = request_kwargs.get("input")
    updated_input = copy.deepcopy(input_value) if isinstance(input_value, list) else None
    if isinstance(updated_input, list):
        normalized_input: list[Any] = []
        leading_additional_tools = True
        for item in updated_input:
            if (
                leading_additional_tools
                and isinstance(item, dict)
                and item.get("type") == "additional_tools"
            ):
                item_tools = item.get("tools")
                if not isinstance(item_tools, list):
                    normalized_input.append(item)
                    continue
                remaining_tools: list[Any] = []
                promoted_native = False
                for tool in item_tools:
                    if _codex_tool_is_hosted_web_search(tool):
                        hosted_replaced = True
                        promoted_native = True
                        continue
                    if _codex_tool_is_provider_native_web_search(tool):
                        promoted_native = True
                        continue
                    remaining_tools.append(tool)
                if promoted_native:
                    if remaining_tools:
                        item["tools"] = remaining_tools
                        normalized_input.append(item)
                    # Promote the declaration to the top-level tools array.
                    # Leaving it in ``additional_tools`` would either send an
                    # invalid OpenRouter shape or duplicate it when Codex
                    # client tools are lifted later in the request pipeline.
                    continue
                normalized_input.append(item)
                continue
            leading_additional_tools = False
            normalized_input.append(item)
        if normalized_input != input_value:
            modified["input"] = normalized_input

    # This helper is called only when native OpenRouter search is the selected
    # path, so make the exact provider declaration top-level even when the
    # caller originally placed it inside a Codex ``additional_tools`` item.
    if not any(
        _codex_tool_is_provider_native_web_search(tool)
        for tool in updated_tools
    ):
        updated_tools.append({"type": "openrouter:web_search"})
    modified["tools"] = updated_tools
    return modified, hosted_replaced


def _codex_openrouter_search_capability_probe_request(
    request_kwargs: dict,
    declared_tools: Optional[List[dict]] = None,
) -> dict:
    """Return a probe-shaped copy for route-local native-search state.

    The negative capability cache is keyed by the tool family that failed. A
    later plain Codex turn has no search declaration yet, so use the same
    provider-native declaration that this hook would send when consulting the
    cache. This does not expose the local bridge or mutate the caller request.
    """

    declared_tools = (
        _codex_declared_tools(request_kwargs)
        if declared_tools is None
        else declared_tools
    )
    if any(_codex_tool_is_provider_native_web_search(tool) for tool in declared_tools):
        return request_kwargs
    if any(
        _codex_tool_definition_name(tool) in {"web_search", "fetch_content"}
        for tool in declared_tools
    ):
        return request_kwargs
    probe, _hosted_replaced = _codex_openrouter_native_search_request(
        request_kwargs,
        declared_tools,
    )
    return probe


def _with_codex_openrouter_native_web_search_tool(
    request_kwargs: dict,
) -> Optional[dict]:
    """Expose OpenRouter server-side search on Codex OpenRouter turns.

    A route with an explicit negative capability, or one remembered as
    rejected by the short probe cache, is left to the pi-web-access adapter.
    Unknown capability deliberately receives a native probe first.
    """

    if (
        not _request_has_responses_shape(request_kwargs)
        or _request_is_codex_compaction(request_kwargs)
        or not _request_has_codex_client_evidence(request_kwargs)
        or not _request_is_openrouter_route(request_kwargs)
    ):
        return None

    from . import responses_surfaces as _responses_surfaces_module

    declared_tools = _codex_declared_tools(request_kwargs)
    capability_request = _codex_openrouter_search_capability_probe_request(
        request_kwargs, declared_tools
    )
    support = _responses_surfaces_module._request_native_responses_web_search_support_decision(
        capability_request
    )
    if support is False:
        return None

    if any(
        _codex_tool_definition_name(tool) in {"web_search", "fetch_content"}
        for tool in declared_tools
    ):
        return None
    top_level_tools = request_kwargs.get("tools")
    has_top_level_native = isinstance(top_level_tools, list) and any(
        _codex_tool_is_provider_native_web_search(tool)
        for tool in top_level_tools
    )
    nested_tools = _codex_declared_tools({"input": request_kwargs.get("input")})
    has_nested_search_declaration = any(
        _codex_tool_is_web_search_declaration(tool)
        for tool in nested_tools
    )
    has_hosted_search_declaration = any(
        _codex_tool_is_hosted_web_search(tool) for tool in declared_tools
    )
    if (
        has_top_level_native
        and not has_nested_search_declaration
        and not has_hosted_search_declaration
    ):
        return None

    modified_kwargs, hosted_replaced = _codex_openrouter_native_search_request(
        request_kwargs,
        declared_tools,
    )
    existing_extra_body = request_kwargs.get("extra_body")
    if isinstance(existing_extra_body, dict) and "tools" in existing_extra_body:
        extra_body = existing_extra_body.copy()
        extra_body["tools"] = copy.deepcopy(modified_kwargs["tools"])
        modified_kwargs["extra_body"] = extra_body
    hosted_options_dropped = False
    if hosted_replaced:
        if modified_kwargs.pop("web_search_options", None) is not None:
            hosted_options_dropped = True
        tool_choice = modified_kwargs.get("tool_choice")
        if (
            isinstance(tool_choice, dict)
            and _codex_tool_is_hosted_web_search(tool_choice)
        ) or (
            isinstance(tool_choice, str)
            and tool_choice in {"web_search", "web_search_preview"}
        ):
            modified_kwargs["tool_choice"] = {"type": "openrouter:web_search"}
    metadata = (
        _request_context_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        )
        or {}
    )
    updated_metadata = metadata.copy()
    if hosted_replaced and hosted_options_dropped:
        updated_metadata["openrouter_hosted_web_search_options_dropped"] = True
    updated_metadata[_CODEX_OPENROUTER_NATIVE_WEB_SEARCH_METADATA_KEY] = True
    modified_kwargs["litellm_metadata"] = updated_metadata
    return modified_kwargs


def _with_codex_external_web_search_bridge_tool(
    request_kwargs: dict,
) -> Optional[dict]:
    """Expose pi-web-access functions to Codex model turns.

    Codex's standalone ``/alpha/search`` endpoint is not represented in the
    Responses model tool list.  Expose the bundled pi-web-access functions as
    ordinary Responses tools so the model can choose them from their schemas.
    This hook deliberately does not alter ``instructions`` or require a tool
    call; execution remains driven by the model's normal function-call output.
    """
    if not _request_has_responses_shape(request_kwargs):
        return None
    if _request_is_codex_compaction(request_kwargs):
        return None
    if not _request_has_codex_client_evidence(request_kwargs):
        return None

    openrouter_route = _request_is_openrouter_route(request_kwargs)
    # Keep the direct worker contract attached to turns that already carry a
    # client tool registry. Once an OpenRouter route has a remembered or
    # explicit native-search rejection, it also needs the local pair even when
    # the original request had no other callable tools.
    declared_tools = _codex_declared_tools(request_kwargs)
    if not declared_tools and not openrouter_route:
        return None

    from . import responses_surfaces as _responses_surfaces_module

    # Native search must get the first opportunity. An unknown capability on
    # a Responses route is deliberately left untouched; the local pair is
    # reserved for an explicit unsupported capability or a cached deterministic
    # rejection so the upstream still gets to produce native search events.
    capability_request = (
        _codex_openrouter_search_capability_probe_request(
            request_kwargs, declared_tools
        )
        if openrouter_route
        else request_kwargs
    )
    native_search_rejected = (
        _responses_surfaces_module._request_native_responses_web_search_support_decision(
            capability_request
        )
        is False
    )
    if (
        _responses_surfaces_module._request_supports_native_responses_web_search(
            capability_request
        )
        or _responses_surfaces_module._request_should_try_unknown_native_responses_web_search(
            capability_request
        )
    ):
        return None

    # These are ordinary client-side function tools backed by the local
    # pi-web-access worker, not a claim that the selected upstream exposes
    # hosted web search. If the selected route explicitly cannot accept
    # Responses function tools, do not advertise functions that it cannot
    # receive.
    supports_function_tools = (
        _responses_surfaces_module._request_supports_responses_function_tools(
            request_kwargs
        )
    )
    explicit_function_tool_rejection = (
        _request_context_module._request_model_info(request_kwargs).get(
            "supports_responses_function_tools"
        )
        is False
    )
    if not supports_function_tools and not (
        openrouter_route
        and native_search_rejected
        and not explicit_function_tool_rejection
    ):
        return None

    tools = request_kwargs.get("tools")
    for tool in declared_tools:
        if not isinstance(tool, dict):
            continue
        if _codex_tool_is_web_search_declaration(tool):
            return None

    # Keep the canonical pi-web-access schemas in the Responses tool module;
    # the local import avoids a module cycle during startup.
    from . import responses_tools as _responses_tools_module

    direct_tools = _responses_tools_module._pi_web_access_tool_definitions()
    if not direct_tools:
        return None
    updated_tools = list(tools) if isinstance(tools, list) else []
    updated_tools.extend(direct_tools)
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["tools"] = updated_tools
    return modified_kwargs


def _codex_tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return ""
    chunks: list[str] = []
    for part in output:
        if not isinstance(part, dict):
            continue
        text = part.get("text") or part.get("input_text")
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)


def _value_has_encrypted_content(value: Any) -> bool:
    if isinstance(value, dict):
        encrypted_content = value.get("encrypted_content")
        if isinstance(encrypted_content, str) and encrypted_content:
            return True
        return any(_value_has_encrypted_content(child) for child in value.values())
    if isinstance(value, list):
        return any(_value_has_encrypted_content(child) for child in value)
    return False


def _codex_text_tool_output_parts(output: Any) -> Optional[list[str]]:
    if not isinstance(output, list) or not output:
        return None
    chunks: list[str] = []
    for part in output:
        if (
            not isinstance(part, dict)
            or part.get("type") != "input_text"
            or not set(part).issubset({"type", "text"})
            or not isinstance(part.get("text"), str)
        ):
            return None
        chunks.append(part["text"])
    return chunks


_CODEX_VIEW_IMAGE_PATH_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"')
_CODEX_VIEW_IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
)
_CODEX_VIEW_IMAGE_REFERENCE_PATH_LINE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")


def _codex_view_image_paths_from_call(item: Any) -> list[str]:
    if (
        not isinstance(item, dict)
        or item.get("type") != "custom_tool_call"
        or item.get("name") != "exec"
        or not isinstance(item.get("input"), str)
    ):
        return []
    source = item["input"]
    if "view_image" not in source:
        return []
    paths: list[str] = []
    for literal in _CODEX_VIEW_IMAGE_PATH_LITERAL.findall(source):
        try:
            value = json.loads(literal)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, str):
            continue
        normalized = value.replace("\\\\", "\\")
        is_absolute = normalized.startswith("/") or bool(
            re.match(r"^[A-Za-z]:[\\\\/]", normalized)
        )
        if (
            is_absolute
            and normalized.lower().endswith(_CODEX_VIEW_IMAGE_EXTENSIONS)
            and normalized not in paths
        ):
            paths.append(normalized)
    return paths


def _codex_view_image_call_requests_original(item: Any) -> bool:
    if (
        not isinstance(item, dict)
        or item.get("type") != "custom_tool_call"
        or item.get("name") != "exec"
        or not isinstance(item.get("input"), str)
    ):
        return False
    return bool(
        re.search(
            r"(?:detail|['\"]detail['\"])[\s:]+['\"]original['\"]",
            item["input"],
            flags=re.IGNORECASE,
        )
    )


def _codex_view_image_output_parts(output: Any) -> list[dict]:
    if not isinstance(output, list):
        return []
    return [
        part
        for part in output
        if isinstance(part, dict)
        and part.get("type") == "input_image"
        and isinstance(part.get("image_url"), str)
        and part["image_url"].startswith("data:image/")
    ]


def _codex_view_image_referenced_paths(value: Any) -> set[str]:
    """Read path references already emitted in mutable tool-output text."""

    if isinstance(value, list):
        paths: set[str] = set()
        for item in value:
            paths.update(_codex_view_image_referenced_paths(item))
        return paths
    if not isinstance(value, dict):
        return set()
    if value.get("type") == "input_text" and isinstance(value.get("text"), str):
        text = value["text"]
        if _CODEX_VIEW_IMAGE_REFERENCE_MARKER not in text:
            return set()
        paths: set[str] = set()
        for line in text.splitlines():
            match = _CODEX_VIEW_IMAGE_REFERENCE_PATH_LINE.match(line)
            if not match:
                continue
            path = match.group(1).strip()
            if (
                (path.startswith("/") or re.match(r"^[A-Za-z]:[\\\\/]", path))
                and path.lower().endswith(_CODEX_VIEW_IMAGE_EXTENSIONS)
            ):
                paths.add(path)
        return paths
    paths: set[str] = set()
    for item in value.values():
        paths.update(_codex_view_image_referenced_paths(item))
    return paths


def _with_codex_view_image_output_paths(request_kwargs: dict) -> Optional[dict]:
    """Pair mutable ``view_image`` results with paths for on-demand reinspection."""

    if (
        not _request_has_responses_shape(request_kwargs)
        or not _request_has_codex_client_evidence(request_kwargs)
    ):
        return None
    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list):
        return None

    last_encrypted_index = max(
        (
            index
            for index, item in enumerate(input_items)
            if _value_has_encrypted_content(item)
        ),
        default=-1,
    )
    call_paths: dict[str, list[str]] = {}
    call_original_paths: dict[str, list[str]] = {}
    referenced_paths: set[str] = set()
    updated_items = list(input_items)
    changed = False
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "custom_tool_call":
            call_id = item.get("call_id") or item.get("id")
            paths = _codex_view_image_paths_from_call(item)
            if isinstance(call_id, str) and paths:
                call_paths[call_id] = paths
                if _codex_view_image_call_requests_original(item):
                    call_original_paths[call_id] = paths
                else:
                    call_original_paths[call_id] = [
                        path for path in paths if path in referenced_paths
                    ]
            continue
        if index <= last_encrypted_index or item.get("type") != "custom_tool_call_output":
            referenced_paths.update(_codex_view_image_referenced_paths(item))
            continue
        output = item.get("output")
        if not isinstance(output, list) or any(
            isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and _CODEX_VIEW_IMAGE_REFERENCE_MARKER in part["text"]
            for part in output
        ):
            referenced_paths.update(_codex_view_image_referenced_paths(item))
            continue
        call_id = item.get("call_id") or item.get("id")
        paths = call_paths.get(call_id) if isinstance(call_id, str) else None
        original_paths = (
            call_original_paths.get(call_id) if isinstance(call_id, str) else None
        ) or []
        image_parts = _codex_view_image_output_parts(output)
        if not paths or len(paths) != len(image_parts):
            referenced_paths.update(_codex_view_image_referenced_paths(item))
            continue
        references = "\n".join(
            f"{number}. {path}" for number, path in enumerate(paths, start=1)
        )
        if original_paths:
            reference_text = (
                f"{_CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER}\n"
                "Original-resolution image requested for this explicit re-open; "
                "the inline image below is intentionally not reduced.\n"
                f"{_CODEX_VIEW_IMAGE_REFERENCE_MARKER}\n"
                f"{references}"
            )
        else:
            reference_text = (
                f"{_CODEX_VIEW_IMAGE_REFERENCE_MARKER}\n"
                "Inline images below are reduced previews. For full detail, call "
                "view_image again on the matching local path:\n"
                f"{references}"
            )
        reference_part = {
            "type": "input_text",
            "text": reference_text,
        }
        updated_item = item.copy()
        updated_item["output"] = [reference_part, *output]
        updated_items[index] = updated_item
        changed = True
        referenced_paths.update(_codex_view_image_referenced_paths(updated_item))

    if not changed:
        return None
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["input"] = updated_items
    return modified_kwargs


def _with_codex_function_call_output_text(request_kwargs: dict) -> Optional[dict]:
    """Flatten text-only Codex function results in the mutable replay suffix."""

    if (
        not _request_has_responses_shape(request_kwargs)
        or not _request_has_codex_client_evidence(request_kwargs)
    ):
        return None
    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list):
        return None

    last_encrypted_index = max(
        (
            index
            for index, item in enumerate(input_items)
            if _value_has_encrypted_content(item)
        ),
        default=-1,
    )
    updated_items = list(input_items)
    changed = False
    for index in range(last_encrypted_index + 1, len(input_items)):
        item = input_items[index]
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        chunks = _codex_text_tool_output_parts(item.get("output"))
        if chunks is None:
            continue
        updated_item = item.copy()
        updated_item["output"] = "\n".join(chunks)
        updated_items[index] = updated_item
        changed = True

    if not changed:
        return None
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["input"] = updated_items
    return modified_kwargs


def _codex_repaired_function_arguments(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.startswith("{}"):
        return None
    suffix = value[2:].lstrip()
    if not suffix:
        return None
    try:
        parsed = json.loads(suffix)
    except (TypeError, ValueError):
        return None
    return suffix if isinstance(parsed, dict) else None


def _with_codex_function_call_arguments_repaired(
    request_kwargs: dict,
) -> Optional[dict]:
    """Repair the empty-object placeholder emitted before streamed deltas."""

    if (
        not _request_has_responses_shape(request_kwargs)
        or not _request_has_codex_client_evidence(request_kwargs)
    ):
        return None
    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list):
        return None

    last_encrypted_index = max(
        (
            index
            for index, item in enumerate(input_items)
            if _value_has_encrypted_content(item)
        ),
        default=-1,
    )
    updated_items = list(input_items)
    changed = False
    for index in range(last_encrypted_index + 1, len(input_items)):
        item = input_items[index]
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        repaired_arguments = _codex_repaired_function_arguments(item.get("arguments"))
        if repaired_arguments is None:
            continue
        updated_item = item.copy()
        updated_item["arguments"] = repaired_arguments
        updated_items[index] = updated_item
        changed = True

    if not changed:
        return None
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["input"] = updated_items
    return modified_kwargs


def _codex_tool_choice_name(tool_choice: Any) -> Optional[str]:
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice not in {"auto", "required", "none"} else None
    if not isinstance(tool_choice, dict):
        return None
    function = tool_choice.get("function")
    function_dict = function if isinstance(function, dict) else {}
    name = function_dict.get("name") or tool_choice.get("name")
    return name if isinstance(name, str) and name.strip() else None


_CODEX_DESCENDANT_CLEANUP_ENV = "LITELLM_MENU_CODEX_DESCENDANT_CLEANUP"
_MCP_AUTO_APPROVE_ENV = "LITELLM_MENU_MCP_AUTO_APPROVE"
_CODEX_DESCENDANT_CLEANUP_MARKER = "<litellm_menu_codex_descendant_cleanup>"
_CODEX_DESCENDANT_CLEANUP_METADATA_KEY = "codex_descendant_cleanup"
_CODEX_DESCENDANT_LIFECYCLE_TOOLS = {
    "followup_task",
    "interrupt_agent",
    "spawn_agent",
}
_CODEX_DESCENDANT_CLEANUP_INSTRUCTION = (
    f"{_CODEX_DESCENDANT_CLEANUP_MARKER}\n"
    "Every assistant response without a real tool call terminates the current "
    "Codex turn, even when its text calls itself commentary, a progress update, "
    "or promises future work. Never emit such a response while work remains. "
    "Completion means the full user-requested outcome, not merely answering the "
    "latest sentence. A correction, expression of dissatisfaction, evidence "
    "challenge, status question, or follow-up during unfinished work adds context "
    "or requirements unless the user clearly replaces the task. If your answer "
    "would reveal that a promised action, implementation, screenshot, test, "
    "verification, deployment, or other required result was not actually completed, "
    "continue doing the work; an admission, apology, explanation, or failed "
    "verification is not completion. Treat every future-tense commitment you make "
    "in commentary as outstanding until later evidence establishes it, or until you "
    "report a concrete blocker after exhausting safe in-scope alternatives. "
    "Before any tool-free response, account for every live descendant with "
    "list_agents, including descendants spawned by another agent in your subtree. "
    "Use canonical agent paths: a subagent may manage only paths beneath its own path, "
    "never a sibling or ancestor; the root agent owns the entire tree. If a descendant "
    "still owns code, file, test, or other work required for the requested outcome, "
    "wait for it and incorporate its result before finalizing. If required work is "
    "stalled in an unusable descendant, take ownership or reassign that work, interrupt "
    "the unusable descendant, and finish the work before finalizing; never drop required "
    "work merely to clear the descendant. If the deliverable no "
    "longer depends on a live descendant, interrupt it. Clean up unneeded descendants "
    "deepest-first before their parents, then call list_agents again and do not "
    "finalize while an unneeded descendant is still running. A spawn_agent, "
    "followup_task, or interrupt_agent call invalidates every earlier list_agents "
    "snapshot, so list the full subtree again afterwards. If a required descendant "
    "is still active, make a real wait_agent or other work tool call in the same "
    "response; a progress-only response would terminate the turn. A clean descendant "
    "snapshot only accounts for descendants; it never proves the root task complete. "
    "After obtaining one, independently compare the evidence against the full requested "
    "outcome and every outstanding commentary commitment. If root work remains, call "
    "a real work tool in the same response. Visible answer text alone is not evidence "
    "that implementation work is complete.\n"
    "</litellm_menu_codex_descendant_cleanup>"
)


def _codex_message_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text") or part.get("input_text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _codex_request_is_root_agent(request_kwargs: dict) -> bool:
    input_value = request_kwargs.get("input")
    if not isinstance(input_value, list):
        return False
    root_marker = "You are `/root`, the primary agent"
    generic_agent_marker = (
        "You are an agent in a team of agents collaborating to complete a task."
    )
    is_root: Optional[bool] = None
    for item in input_value:
        if (
            not isinstance(item, dict)
            or str(item.get("role") or "").lower() not in {"developer", "system"}
        ):
            continue
        text = _codex_message_text(item)
        markers = (
            (text.rfind(root_marker), True),
            (text.rfind("You are `/root/"), False),
            (text.rfind(generic_agent_marker), False),
        )
        position, marker_is_root = max(markers, key=lambda marker: marker[0])
        if position >= 0:
            is_root = marker_is_root
    return is_root is True


def _codex_call_arguments(item: dict) -> Optional[dict]:
    arguments = item.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return None
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _codex_list_agents_call_covers_root(item: dict) -> bool:
    arguments = _codex_call_arguments(item)
    if arguments is None:
        return False
    path_prefix = arguments.get("path_prefix")
    return path_prefix is None or path_prefix == "/root"


def _codex_descendant_status_is_active(status: Any) -> bool:
    terminal = {"cancelled", "completed", "errored", "failed", "interrupted"}
    if isinstance(status, str):
        return status.strip().lower() not in terminal
    if isinstance(status, dict):
        return not any(str(key).strip().lower() in terminal for key in status)
    return True


def _codex_list_agents_output_has_active_descendants(output: Any) -> Optional[bool]:
    text = _codex_tool_output_text(output).strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    agents = payload.get("agents") if isinstance(payload, dict) else None
    if not isinstance(agents, list):
        return None
    for agent in agents:
        if not isinstance(agent, dict):
            return None
        name = agent.get("agent_name")
        if not isinstance(name, str):
            return None
        if name == "/root":
            continue
        if name.startswith("/root/") and _codex_descendant_status_is_active(
            agent.get("agent_status")
        ):
            return True
    return False


def _codex_descendant_cleanup_runtime_state(request_kwargs: dict) -> Optional[str]:
    """Return the root turn state that can make a tool-free response unsafe.

    Function calls emitted in one assistant tool batch are concurrent.  Their
    serialized order in ``input`` is not an execution-order guarantee, so a
    ``list_agents`` call from the same batch as a lifecycle mutation cannot
    release the barrier in either ordering.
    """
    if not _codex_request_is_root_agent(request_kwargs):
        return None
    input_value = request_kwargs.get("input")
    if not isinstance(input_value, list):
        return None

    calls: dict[str, tuple[str, bool, int]] = {}
    batch = 0
    batch_open = False
    lifecycle_batches: set[int] = set()
    snapshot_active: Optional[bool] = None
    snapshot_valid = False
    snapshot_invalidated = False
    for item in input_value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"function_call", "custom_tool_call"}:
            if not batch_open:
                batch += 1
                batch_open = True
            call_id = item.get("call_id") or item.get("id")
            name = _codex_tool_definition_name(item)
            if isinstance(call_id, str) and name is not None:
                covers_root = name == "list_agents" and _codex_list_agents_call_covers_root(item)
                calls[call_id] = (name, covers_root, batch)
            if name in _CODEX_DESCENDANT_LIFECYCLE_TOOLS:
                lifecycle_batches.add(batch)
                snapshot_invalidated = True
            continue
        if item_type not in {"function_call_output", "custom_tool_call_output"}:
            continue
        batch_open = False
        call_id = item.get("call_id") or item.get("id")
        call = calls.get(call_id) if isinstance(call_id, str) else None
        if (
            call is None
            or call[0] != "list_agents"
            or call[1] is not True
            or call[2] in lifecycle_batches
        ):
            continue
        active = _codex_list_agents_output_has_active_descendants(item.get("output"))
        if active is None:
            continue
        snapshot_active = active
        snapshot_valid = True
        snapshot_invalidated = False

    if snapshot_invalidated:
        return "snapshot_invalidated"
    if snapshot_valid and snapshot_active:
        return "active_descendants"
    if not snapshot_valid:
        return "snapshot_missing"
    return None


def _codex_descendant_cleanup_has_history(request_kwargs: dict) -> bool:
    """Return whether this replay contains a descendant-management call.

    A root Codex request can expose the collaboration namespace without ever
    spawning a child.  In that ordinary case there is no cleanup barrier to
    enforce yet; forcing ``list_agents`` on the first request makes otherwise
    valid providers reject the request before the model can answer.  Once a
    lifecycle call or a prior root snapshot appears in the replay, the barrier
    is meaningful and may be required again after the next turn.
    """
    input_value = request_kwargs.get("input")
    if not isinstance(input_value, list):
        return False
    tracked_names = _CODEX_DESCENDANT_LIFECYCLE_TOOLS | {"list_agents"}
    for item in input_value:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"function_call", "custom_tool_call"}:
            continue
        if _codex_tool_definition_name(item) in tracked_names:
            return True
    return False


def _codex_declared_tool_names(request_kwargs: Optional[dict]) -> set[str]:
    names: set[str] = set()

    def visit(tool: Any) -> None:
        name = _codex_tool_definition_name(tool)
        if name is not None:
            names.add(name)
        if not isinstance(tool, dict):
            return
        child_tools = tool.get("tools")
        if isinstance(child_tools, list):
            for child_tool in child_tools:
                visit(child_tool)

    for tool in _codex_declared_tools(request_kwargs):
        visit(tool)
    return names


_CODEX_TOOL_REGISTRY_MARKER = "<litellm_menu_codex_tool_registry>"
_CODEX_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CODEX_TOOL_PATH_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)


def _codex_declared_tool_registry(
    request_kwargs: Optional[dict],
) -> tuple[list[str], list[str]]:
    """Return direct Codex tool keys and their declared namespace paths."""
    direct_names: list[str] = []
    qualified_names: list[str] = []

    def append(target: list[str], name: Any) -> None:
        if (
            isinstance(name, str)
            and (
                _CODEX_TOOL_NAME_PATTERN.fullmatch(name)
                or _CODEX_TOOL_PATH_PATTERN.fullmatch(name)
            )
            and name not in target
        ):
            target.append(name)

    def visit(tool: Any, namespace: str = "") -> None:
        if not isinstance(tool, dict):
            return
        name = _codex_tool_definition_name(tool)
        tool_type = tool.get("type")
        if tool_type == "namespace":
            next_namespace = name if isinstance(name, str) else ""
            if namespace and next_namespace:
                next_namespace = f"{namespace}.{next_namespace}"
            children = tool.get("tools")
            if isinstance(children, list) and children:
                for child in children:
                    visit(child, next_namespace)
                return
        if not isinstance(name, str):
            return
        # These are Responses function tools backed by the proxy's bundled
        # pi-web-access runtime, not keys on the host-side ``tools`` object.
        # Keeping them out of this registry prevents the model from trying
        # ``tools.web_search``/``tools.fetch_content`` through ``exec``.
        if name in _PI_WEB_ACCESS_TOOL_NAMES:
            return
        append(direct_names, name)
        qualified = f"{namespace}.{name}" if namespace else name
        append(qualified_names, qualified)

    for tool in _codex_declared_tools(request_kwargs):
        visit(tool)
    return direct_names, qualified_names


def _with_codex_tool_registry_instruction(
    request_kwargs: dict,
) -> Optional[dict]:
    """Tell Codex-backed models which callable tool keys exist in this request."""
    if not _request_has_responses_shape(request_kwargs):
        return None
    if _request_is_codex_compaction(request_kwargs):
        return None
    if not _request_has_codex_client_evidence(request_kwargs):
        return None
    direct_names, qualified_names = _codex_declared_tool_registry(request_kwargs)
    if not direct_names:
        return None
    instructions = request_kwargs.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        return None
    instructions = instructions or ""
    if _CODEX_TOOL_REGISTRY_MARKER in instructions:
        return None

    direct_text = ", ".join(f"tools.{name}" for name in direct_names)
    qualified_text = ", ".join(qualified_names)
    web_search_is_declared = any(
        name.rsplit(".", 1)[-1] == "web__run"
        for name in (*direct_names, *qualified_names)
    )
    unavailable_web_search_hint = (
        " In particular, `tools.web__run` is unavailable in this request."
        if not web_search_is_declared
        else ""
    )
    note = (
        f"{_CODEX_TOOL_REGISTRY_MARKER}\n"
        "This request's complete callable tool registry is fixed by the tools "
        f"declared below. The available direct keys are: {direct_text}. "
        f"Their declared namespace paths are: {qualified_text}. "
        "Only these keys exist on the `tools` object. Never call or invent an "
        f"unlisted name.{unavailable_web_search_hint} Do not retry an "
        "unlisted tool. If the required capability is absent, "
        "say that it is unavailable or continue without it.\n"
        f"</litellm_menu_codex_tool_registry>"
    )
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["instructions"] = (
        f"{instructions.rstrip()}\n\n{note}" if instructions.strip() else note
    )
    return modified_kwargs


def _with_codex_descendant_cleanup_instruction(
    request_kwargs: dict,
) -> Optional[dict]:
    """Enforce a root-agent completion barrier around nested Codex work."""
    if not _routing_module._env_bool(_CODEX_DESCENDANT_CLEANUP_ENV, True):
        return None
    if not _request_has_responses_shape(request_kwargs):
        return None
    if not _request_has_codex_client_evidence(request_kwargs):
        return None
    if _request_is_codex_compaction(request_kwargs):
        return None
    if not {"list_agents", "interrupt_agent"}.issubset(
        _codex_declared_tool_names(request_kwargs)
    ):
        return None

    instructions = request_kwargs.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        return None
    instructions = instructions or ""
    modified_kwargs = request_kwargs.copy()
    changed = False
    if _CODEX_DESCENDANT_CLEANUP_MARKER not in instructions:
        modified_kwargs["instructions"] = (
            f"{instructions.rstrip()}\n\n{_CODEX_DESCENDANT_CLEANUP_INSTRUCTION}"
            if instructions.strip()
            else _CODEX_DESCENDANT_CLEANUP_INSTRUCTION
        )
        changed = True

    runtime_state = _codex_descendant_cleanup_runtime_state(request_kwargs)
    if runtime_state is not None:
        metadata = (
            _request_context_module._request_metadata_dict(
                request_kwargs,
                "litellm_metadata",
            )
            or {}
        )
        next_metadata = metadata.copy()
        state_metadata = {"state": runtime_state, "tool_call_required": True}
        if metadata.get(_CODEX_DESCENDANT_CLEANUP_METADATA_KEY) != state_metadata:
            next_metadata[_CODEX_DESCENDANT_CLEANUP_METADATA_KEY] = state_metadata
            modified_kwargs["litellm_metadata"] = next_metadata
            changed = True

        # A protocol fallback reaches this hook again after the surface
        # adapter has deliberately relaxed a named choice that the upstream
        # rejected.  Keep the cleanup instruction and its state metadata, but
        # do not re-inject the rejected named choice on every lower-level
        # pre-call hook.  The marker is request-scoped and is set only after
        # the concrete compatibility error, so ordinary calls retain the
        # strict cleanup barrier below.
        barrier_required = runtime_state in {"active_descendants", "snapshot_invalidated"}
        if runtime_state == "snapshot_missing":
            barrier_required = _codex_descendant_cleanup_has_history(request_kwargs)
        if (
            barrier_required
            and not _routing_module._protocol_fallback_relax_tool_choice(request_kwargs)
            and _codex_tool_choice_name(request_kwargs.get("tool_choice")) is None
        ):
            # Snapshot recovery has one valid action. Generic ``required`` lets
            # the model pick an unrelated tool and repeat the same turn.
            required_tool_choice: Any = "required"
            if runtime_state in {"snapshot_missing", "snapshot_invalidated"}:
                required_tool_choice = {"type": "function", "name": "list_agents"}
            if request_kwargs.get("tool_choice") != required_tool_choice:
                modified_kwargs["tool_choice"] = required_tool_choice
                changed = True

    return modified_kwargs if changed else None


def _is_xhigh_reasoning_effort(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.strip().lower() == _XHIGH_REASONING_EFFORT
    )


def _xhigh_reasoning_compat_target_effort(exception: Exception) -> str:
    text = _routing_module._exception_text(exception)
    if (
        re.search(r"(?<![a-z0-9_])max(?![a-z0-9_])", text)
        and all(
            re.search(rf"(?<![a-z0-9_]){level}(?![a-z0-9_])", text)
            for level in ("low", "medium", "high")
        )
    ):
        return _MAX_COMPAT_REASONING_EFFORT
    return _CHAT_COMPAT_REASONING_EFFORT


def _map_reasoning_effort_for_chat(
    value: Any,
    *,
    in_reasoning: bool = False,
    target_effort: str = _CHAT_COMPAT_REASONING_EFFORT,
) -> tuple[Any, bool]:
    if _is_xhigh_reasoning_effort(value):
        return target_effort, True

    if not isinstance(value, dict):
        return value, False

    changed = False
    updated: dict[Any, Any] = {}
    for key, item in value.items():
        if key == "reasoning_effort":
            if _is_xhigh_reasoning_effort(item):
                updated[key] = target_effort
                changed = True
                continue
            if isinstance(item, dict):
                mapped_item, item_changed = _map_reasoning_effort_for_chat(
                    item,
                    in_reasoning=True,
                    target_effort=target_effort,
                )
                updated[key] = mapped_item
                changed = changed or item_changed
                continue
        if key == "reasoning" and isinstance(item, dict):
            mapped_item, item_changed = _map_reasoning_effort_for_chat(
                item,
                in_reasoning=True,
                target_effort=target_effort,
            )
            updated[key] = mapped_item
            changed = changed or item_changed
            continue
        if in_reasoning and key == "effort" and _is_xhigh_reasoning_effort(item):
            updated[key] = target_effort
            changed = True
            continue
        if key in {"extra_body", "litellm_params"} and isinstance(item, dict):
            mapped_item, item_changed = _map_reasoning_effort_for_chat(
                item,
                target_effort=target_effort,
            )
            updated[key] = mapped_item
            changed = changed or item_changed
            continue
        updated[key] = item

    return (updated if changed else value), changed


def _request_already_attempted_xhigh_reasoning_compat_retry(
    request_kwargs: Optional[dict],
) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if (
            metadata is not None
            and metadata.get(_XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY) is True
        ):
            return True
    return False


def _xhigh_reasoning_compat_retry_kwargs(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> Optional[dict]:
    if not isinstance(request_kwargs, dict):
        return None
    if _request_already_attempted_xhigh_reasoning_compat_retry(request_kwargs):
        return None
    if not _routing_module._is_xhigh_reasoning_unsupported_error(exception):
        return None

    target_effort = _xhigh_reasoning_compat_target_effort(exception)
    mapped_kwargs, changed = _map_reasoning_effort_for_chat(
        request_kwargs,
        target_effort=target_effort,
    )
    if not changed or not isinstance(mapped_kwargs, dict):
        return None

    retry_kwargs = mapped_kwargs.copy()
    litellm_metadata = _request_context_module._request_metadata_dict(retry_kwargs, "litellm_metadata") or {}
    retry_metadata = litellm_metadata.copy()
    retry_metadata[_XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY] = True
    retry_kwargs["litellm_metadata"] = retry_metadata
    _trace_module._route_trace(
        "xhigh_reasoning_compat_retry_start",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_request_context_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        exception=_routing_module._trace_exception(exception),
        from_effort=_XHIGH_REASONING_EFFORT,
        to_effort=target_effort,
    )
    return retry_kwargs


def _with_stream_request_timeout(request_kwargs: dict) -> Optional[dict]:
    if request_kwargs.get("stream") is not True:
        return None
    if _request_has_explicit_stream_timeout(request_kwargs):
        return None
    timeout_seconds = _routing_module._request_timeout_seconds()
    if timeout_seconds <= 0:
        return None
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["stream_timeout"] = timeout_seconds
    return modified_kwargs














def _request_already_attempted_streaming_fallback(request_kwargs: Optional[dict]) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if metadata is not None and metadata.get(_STREAM_FALLBACK_METADATA_KEY) is True:
            return True
    return False


def _request_already_attempted_streaming_error_fallback(request_kwargs: Optional[dict]) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if metadata is not None and metadata.get(_STREAM_ERROR_FALLBACK_METADATA_KEY) is True:
            return True
    return False


def _request_has_explicit_stream_timeout(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    for key in ("stream_timeout", "timeout", "request_timeout"):
        if request_kwargs.get(key) is not None:
            return True
    litellm_params = request_kwargs.get("litellm_params")
    if isinstance(litellm_params, dict):
        for key in ("stream_timeout", "timeout", "request_timeout"):
            if litellm_params.get(key) is not None:
                return True
    return False


def _request_already_attempted_responses_chat_bridge(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if metadata is not None and metadata.get(_RESPONSES_CHAT_BRIDGE_METADATA_KEY) is True:
            return True
    return False


def _request_is_fallback_attempt(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    if _request_target_order(request_kwargs) is not None:
        return True
    if _request_excluded_deployment_ids(request_kwargs):
        return True
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if metadata is None:
            continue
        for marker in (
            _STREAM_ERROR_FALLBACK_METADATA_KEY,
            _STREAM_FALLBACK_METADATA_KEY,
            _RESPONSES_CHAT_BRIDGE_METADATA_KEY,
            _RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY,
        ):
            if metadata.get(marker) is True:
                return True
    return False
