from __future__ import annotations

from . import responses_execution as _responses_execution_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import trace as _trace_module


from .base import (
    Any,
    AsyncIterator,
    Dict,
    List,
    Optional,
    _IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY,
    _IMAGE_GENERATION_TOOL_FALLBACK_DEFAULT_MAX_ATTEMPTS,
    _IMAGE_GENERATION_TOOL_FALLBACK_MAX_ATTEMPTS_ENV,
    _BROWSER_COMPATIBLE_HEADERS,
    _BROWSER_COMPATIBLE_HEADER_HOSTS,
    _BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY,
    _CHAT_COMPAT_REASONING_EFFORT,
    _CODEX_TOOL_RUNTIME_RECOVERY_METADATA_KEY,
    _FALLBACK_BROWSER_USER_AGENT,
    _INLINE_IMAGE_MANY_MAX_EDGE,
    _INLINE_IMAGE_MANY_TARGET_BYTES,
    _INLINE_IMAGE_SINGLE_BUDGET_BYTES,
    _INLINE_IMAGE_SINGLE_MAX_EDGE,
    _INLINE_IMAGE_SINGLE_TARGET_BYTES,
    _INLINE_IMAGE_TOTAL_BUDGET_BYTES,
    _MAX_COMPAT_REASONING_EFFORT,
    _OMIT_RESPONSE_VALUE,
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
    _RESPONSES_IMAGE_INPUT_SUPPORT_KEY,
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
    base64,
    binascii,
    copy,
    inspect,
    io,
    json,
    os,
    re,
    urlparse,
)



def _value_has_image_input(value: Any) -> bool:
    if isinstance(value, dict):
        item_type = value.get("type")
        if isinstance(item_type, str) and item_type in {"input_image", "image_url"}:
            return True
        if isinstance(value.get("image_url"), (str, dict)):
            return True
        return any(_value_has_image_input(child) for child in value.values())
    if isinstance(value, list):
        return any(_value_has_image_input(child) for child in value)
    return False


def _request_has_image_input(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    return _value_has_image_input(request_kwargs.get("input")) or _value_has_image_input(
        request_kwargs.get("messages")
    )


def _image_reference_string(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        nested = value.get("url")
        if isinstance(nested, str) and nested.strip():
            return nested
    return None


def _collect_request_image_references(value: Any, references: set[str]) -> None:
    if isinstance(value, dict):
        item_type = value.get("type")
        if isinstance(item_type, str) and item_type in {"input_image", "image_url"}:
            reference = _image_reference_string(
                value.get("image_url") or value.get("url") or value.get("file_id")
            )
            if reference:
                references.add(reference)
        image_url = _image_reference_string(value.get("image_url"))
        if image_url:
            references.add(image_url)
        file_id = _image_reference_string(value.get("file_id"))
        if file_id:
            references.add(file_id)
        for child in value.values():
            _collect_request_image_references(child, references)
        return
    if isinstance(value, list):
        for child in value:
            _collect_request_image_references(child, references)


def _request_image_references(request_kwargs: Optional[dict]) -> set[str]:
    request_kwargs = request_kwargs or {}
    references: set[str] = set()
    _collect_request_image_references(request_kwargs.get("input"), references)
    _collect_request_image_references(request_kwargs.get("messages"), references)
    return references


def _dict_is_echoed_request_image(value: dict, references: set[str]) -> bool:
    item_type = value.get("type")
    image_url = _image_reference_string(
        value.get("image_url") or value.get("url") or value.get("file_id")
    )
    if not image_url or image_url not in references:
        return False
    if item_type in {"input_image", "image_url"}:
        return True
    return item_type is None and set(value).issubset(
        {"image_url", "url", "file_id", "detail"}
    )


def _strip_echoed_request_images(value: Any, references: set[str]) -> tuple[Any, bool]:
    if not references:
        return value, False
    if isinstance(value, list):
        changed = False
        updated_items: list[Any] = []
        for item in value:
            updated_item, item_changed = _strip_echoed_request_images(item, references)
            changed = changed or item_changed
            if updated_item is _OMIT_RESPONSE_VALUE:
                changed = True
                continue
            updated_items.append(updated_item)
        return (updated_items if changed else value), changed
    if isinstance(value, dict):
        if _dict_is_echoed_request_image(value, references):
            return _OMIT_RESPONSE_VALUE, True
        changed = False
        updated_dict: dict[Any, Any] = {}
        for key, item in value.items():
            updated_item, item_changed = _strip_echoed_request_images(item, references)
            changed = changed or item_changed
            if updated_item is _OMIT_RESPONSE_VALUE:
                changed = True
                continue
            updated_dict[key] = updated_item
        return (updated_dict if changed else value), changed
    if hasattr(value, "model_dump"):
        json_value = _streaming_module._jsonable(value)
        if json_value is not None:
            return _strip_echoed_request_images(json_value, references)
    return value, False


def _sanitize_response_echoed_request_images(response: Any, request_kwargs: Optional[dict]) -> Any:
    references = _request_image_references(request_kwargs)
    if not references:
        return response
    sanitized, changed = _strip_echoed_request_images(response, references)
    if not changed:
        return response
    if sanitized is _OMIT_RESPONSE_VALUE:
        return {}
    _trace_module._route_trace(
        "response_echoed_request_image_stripped",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        stripped_image_count=len(references),
    )
    return sanitized


def _sanitize_response_echoed_request_images_for_delivery(
    response: Any,
    request_kwargs: Optional[dict],
) -> Any:
    if not _request_image_references(request_kwargs):
        return response
    if _response_is_async_iterable(response):
        async def _sanitize_stream() -> AsyncIterator[Any]:
            async for chunk in response:
                yield _sanitize_response_echoed_request_images(chunk, request_kwargs)

        return _sanitize_stream()
    return _sanitize_response_echoed_request_images(response, request_kwargs)


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
)

_CODEX_TOOL_OUTPUT_COMPACT_TOTAL_CHARS = 200_000
_CODEX_TOOL_OUTPUT_COMPACT_ITEM_CHARS = 2_000
_CODEX_COMPACTION_HISTORY_TEXT_CHARS = 500_000
_CODEX_COMPACTION_MESSAGE_ITEM_CHARS = 32_000
_CODEX_COMPACTION_TOOL_CALL_ITEM_CHARS = 8_000
_CODEX_COMPACTION_MIN_HISTORY_ITEM_CHARS = 128
_CODEX_COMPACTION_DEVELOPER_MESSAGE_CHARS = 16_000
_RESPONSES_CONTEXT_TRUNCATION_FALLBACK_HISTORY_TEXT_CHARS = 400_000


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


def _split_image_data_url(value: Any) -> Optional[tuple[str, str]]:
    if not isinstance(value, str) or not value.startswith("data:image/"):
        return None
    marker = ";base64,"
    marker_index = value.find(marker)
    if marker_index == -1:
        return None
    return value[: marker_index + len(marker)], value[marker_index + len(marker) :]


def _image_data_url_size(value: Any) -> int:
    parsed = _split_image_data_url(value)
    if parsed is None:
        return 0
    encoded = parsed[1]
    padding = 2 if encoded.endswith("==") else 1 if encoded.endswith("=") else 0
    return max(0, (len(encoded) * 3) // 4 - padding)


def _collect_image_data_url_sizes(value: Any, sizes: List[int]) -> None:
    size = _image_data_url_size(value)
    if size:
        sizes.append(size)
        return
    if isinstance(value, dict):
        for child in value.values():
            _collect_image_data_url_sizes(child, sizes)
    elif isinstance(value, list):
        for child in value:
            _collect_image_data_url_sizes(child, sizes)


def _resize_data_url(value: str, *, target_bytes: int, max_edge: int) -> str:
    parsed = _split_image_data_url(value)
    if parsed is None:
        return value
    prefix, encoded = parsed
    if _image_data_url_size(value) <= target_bytes:
        return value
    try:
        from PIL import Image

        raw = base64.b64decode(encoded, validate=False)
        with Image.open(io.BytesIO(raw)) as image:
            work = image.convert("RGB")
            quality = 86
            edge = max_edge
            while True:
                resized = work.copy()
                if max(resized.size) > edge:
                    resized.thumbnail((edge, edge))
                buffer = io.BytesIO()
                resized.save(buffer, format="JPEG", quality=quality, optimize=True)
                data = buffer.getvalue()
                if len(data) <= target_bytes or (edge <= 768 and quality <= 76):
                    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
                if quality > 76:
                    quality -= 4
                else:
                    edge = int(edge * 0.85)
    except (binascii.Error, OSError, ValueError, ImportError):
        return value


def _bound_image_data_urls(value: Any, *, target_bytes: int, max_edge: int) -> tuple[Any, bool]:
    if isinstance(value, str):
        resized = _resize_data_url(value, target_bytes=target_bytes, max_edge=max_edge)
        return resized, resized != value
    if isinstance(value, list):
        changed = False
        updated_items: List[Any] = []
        for item in value:
            updated_item, item_changed = _bound_image_data_urls(
                item,
                target_bytes=target_bytes,
                max_edge=max_edge,
            )
            updated_items.append(updated_item)
            changed = changed or item_changed
        return (updated_items if changed else value), changed
    if isinstance(value, dict):
        changed = False
        updated_dict: Dict[Any, Any] = {}
        for key, item in value.items():
            updated_item, item_changed = _bound_image_data_urls(
                item,
                target_bytes=target_bytes,
                max_edge=max_edge,
            )
            updated_dict[key] = updated_item
            changed = changed or item_changed
        return (updated_dict if changed else value), changed
    return value, False


def _with_bounded_image_inputs(request_kwargs: dict) -> Optional[dict]:
    sizes: List[int] = []
    for key in ("input", "messages"):
        _collect_image_data_url_sizes(request_kwargs.get(key), sizes)
    if not sizes:
        return None

    total_size = sum(sizes)
    largest_size = max(sizes)
    if (
        total_size <= _INLINE_IMAGE_TOTAL_BUDGET_BYTES
        and largest_size <= _INLINE_IMAGE_SINGLE_BUDGET_BYTES
    ):
        return None

    many_images = len(sizes) > 1 or total_size > _INLINE_IMAGE_TOTAL_BUDGET_BYTES
    target_bytes = (
        _INLINE_IMAGE_MANY_TARGET_BYTES if many_images else _INLINE_IMAGE_SINGLE_TARGET_BYTES
    )
    max_edge = _INLINE_IMAGE_MANY_MAX_EDGE if many_images else _INLINE_IMAGE_SINGLE_MAX_EDGE

    modified_kwargs = copy.copy(request_kwargs)
    changed = False
    for key in ("input", "messages"):
        updated_value, value_changed = _bound_image_data_urls(
            request_kwargs.get(key),
            target_bytes=target_bytes,
            max_edge=max_edge,
        )
        if value_changed:
            modified_kwargs[key] = updated_value
            changed = True
    return modified_kwargs if changed else None


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
        metadata = _request_metadata_dict(request_kwargs, metadata_key)
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
        metadata = _request_metadata_dict(request_kwargs, metadata_key)
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
    metadata = _request_metadata_dict(modified_kwargs, "litellm_metadata") or {}
    modified_kwargs["litellm_metadata"] = metadata.copy()
    modified_kwargs["litellm_metadata"][_BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY] = True
    return _with_browser_compatible_headers(modified_kwargs) or modified_kwargs


def _deployment_order(deployment: Any) -> Optional[int]:
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
        if order is None:
            continue
        if isinstance(order, int):
            return order
        if isinstance(order, str):
            if not order.strip():
                saw_defaultable_order = True
                continue
            try:
                return int(order)
            except ValueError:
                saw_invalid_order = True
                continue
    if saw_invalid_order:
        return None
    return 1 if saw_defaultable_order else None


def _request_target_order(request_kwargs: Optional[dict]) -> Optional[int]:
    request_kwargs = request_kwargs or {}
    target_order = request_kwargs.get("_target_order")
    if isinstance(target_order, int):
        return target_order
    if isinstance(target_order, str):
        try:
            return int(target_order)
        except ValueError:
            return None
    return None


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
                if _routing_module._is_priority_deployment_failover_error(exc):
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
        if _routing_module._is_priority_deployment_failover_error(exc):
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


def _request_metadata_dict(request_kwargs: Optional[dict], key: str) -> Optional[dict]:
    request_kwargs = request_kwargs or {}
    value = request_kwargs.get(key)
    return value if isinstance(value, dict) else None


def _request_model_info(request_kwargs: Optional[dict]) -> dict:
    request_kwargs = request_kwargs or {}
    model_info = request_kwargs.get("model_info")
    if isinstance(model_info, dict):
        return model_info
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_metadata_dict(request_kwargs, key)
        if not metadata:
            continue
        nested_model_info = metadata.get("model_info")
        if isinstance(nested_model_info, dict):
            return nested_model_info
    litellm_params = request_kwargs.get("litellm_params")
    if isinstance(litellm_params, dict):
        for key in ("litellm_metadata", "metadata"):
            metadata = litellm_params.get(key)
            if not isinstance(metadata, dict):
                continue
            nested_model_info = metadata.get("model_info")
            if isinstance(nested_model_info, dict):
                return nested_model_info
    return {}


def _request_allows_upstream_metadata(request_kwargs: Optional[dict]) -> bool:
    model_info = _request_model_info(request_kwargs)
    return any(model_info.get(flag) is True for flag in _UPSTREAM_METADATA_FORWARD_FLAGS)


def _with_internal_litellm_metadata(request_kwargs: dict) -> Optional[dict]:
    if "metadata" not in request_kwargs:
        return None

    if _request_allows_upstream_metadata(request_kwargs):
        metadata = _request_metadata_dict(request_kwargs, "metadata")
        if metadata is None:
            return None
        modified_kwargs = request_kwargs.copy()
        litellm_metadata = _request_metadata_dict(modified_kwargs, "litellm_metadata") or {}
        merged_litellm_metadata = litellm_metadata.copy()
        merged_litellm_metadata.update(metadata)
        modified_kwargs["litellm_metadata"] = merged_litellm_metadata
        return modified_kwargs

    modified_kwargs = request_kwargs.copy()
    metadata = _request_metadata_dict(request_kwargs, "metadata")
    if metadata is not None:
        litellm_metadata = _request_metadata_dict(modified_kwargs, "litellm_metadata") or {}
        merged_litellm_metadata = litellm_metadata.copy()
        merged_litellm_metadata.update(metadata)
        modified_kwargs["litellm_metadata"] = merged_litellm_metadata
    modified_kwargs.pop("metadata", None)
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


def _positive_int_value(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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
    if isinstance(input_items, list) and any(
        isinstance(item, dict) and item.get("type") == "compaction_trigger"
        for item in input_items
    ):
        return True

    return any(
        _codex_turn_metadata_is_compaction(value)
        for value in _codex_turn_metadata_values(request_kwargs)
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
        metadata = _request_metadata_dict(modified_kwargs, metadata_key)
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
        metadata = _request_metadata_dict(request_kwargs, metadata_key)
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
    emulated_truncation = _with_responses_context_auto_truncation_emulated(
        exception,
        retry_kwargs,
    )
    truncation_stats = None
    if emulated_truncation is not None:
        retry_kwargs, truncation_stats = emulated_truncation
    litellm_metadata = (
        _request_metadata_dict(retry_kwargs, "litellm_metadata") or {}
    )
    retry_metadata = litellm_metadata.copy()
    retry_metadata[_RESPONSES_CONTEXT_TRUNCATION_FALLBACK_METADATA_KEY] = True
    retry_kwargs["litellm_metadata"] = retry_metadata
    _trace_module._route_trace(
        "responses_context_truncation_fallback_start",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(
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
        emulated_auto_truncation=truncation_stats,
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
        metadata = _request_metadata_dict(request_kwargs, metadata_key)
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


def _codex_tool_output_with_hint(output: Any, hint: str) -> Optional[Any]:
    if hint in _codex_tool_output_text(output):
        return None
    if isinstance(output, str):
        return f"{output.rstrip()}\n\n{hint}"
    if not isinstance(output, list):
        return None
    updated_output = copy.deepcopy(output)
    for index in range(len(updated_output) - 1, -1, -1):
        part = updated_output[index]
        if not isinstance(part, dict):
            continue
        for key in ("text", "input_text"):
            text = part.get(key)
            if isinstance(text, str):
                part[key] = f"{text.rstrip()}\n\n{hint}"
                return updated_output
    return None


def _codex_tool_choice_name(tool_choice: Any) -> Optional[str]:
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice not in {"auto", "required", "none"} else None
    if not isinstance(tool_choice, dict):
        return None
    function = tool_choice.get("function")
    function_dict = function if isinstance(function, dict) else {}
    name = function_dict.get("name") or tool_choice.get("name")
    return name if isinstance(name, str) and name.strip() else None


def _with_codex_tool_runtime_recovery_hints(
    request_kwargs: dict,
) -> Optional[dict]:
    """Clarify local tool-runtime failures without fabricating tool loss.

    A completed/expired ``wait`` handle says nothing about whether a fresh
    ``exec`` call is available. Likewise, Codex exposes ``request_user_input``
    in Default mode even though the client deterministically rejects it. Keep
    the original structured history, annotate only those exact runtime
    failures, and remove only the tool that the client has proven unavailable.
    """
    if not _request_has_responses_shape(request_kwargs):
        return None
    if not _request_has_codex_client_evidence(request_kwargs):
        return None
    input_value = request_kwargs.get("input")
    if not isinstance(input_value, list):
        return None
    declared_tools = _codex_declared_tools(request_kwargs)
    declared_names = {
        name
        for name in (_codex_tool_definition_name(tool) for tool in declared_tools)
        if name is not None
    }
    if "exec" not in declared_names:
        return None

    recent_items = input_value[-40:]
    call_names: dict[str, str] = {}
    for item in recent_items:
        if not isinstance(item, dict) or item.get("type") not in {
            "function_call",
            "custom_tool_call",
        }:
            continue
        call_id = item.get("call_id") or item.get("id")
        name = _codex_tool_definition_name(item)
        if isinstance(call_id, str) and name is not None:
            call_names[call_id] = name

    updated_input = list(input_value)
    stale_wait_outputs_hinted = 0
    unavailable_question_outputs_hinted = 0
    recent_start = len(input_value) - len(recent_items)
    for offset, item in enumerate(recent_items):
        if not isinstance(item, dict) or item.get("type") not in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            continue
        call_id = item.get("call_id") or item.get("id")
        call_name = call_names.get(call_id) if isinstance(call_id, str) else None
        output = item.get("output")
        output_text = _codex_tool_output_text(output)
        hint = None
        if call_name == "wait" and re.search(
            r"\bexec cell [^\r\n]{1,80} not found\b",
            output_text,
            flags=re.IGNORECASE,
        ):
            hint = (
                "The referenced exec cell has expired or completed. Start a fresh exec call; "
                "the exec tool itself remains available."
            )
            stale_wait_outputs_hinted += 1
        elif (
            call_name == "request_user_input"
            and "request_user_input is unavailable in Default mode" in output_text
        ):
            hint = (
                "This failure applies only to request_user_input in Default mode. "
                "The other declared tools remain available; the custom exec tool remains "
                "available for shell, file, and test work. Continue with those tools."
            )
            unavailable_question_outputs_hinted += 1
        if hint is None:
            continue
        hinted_output = _codex_tool_output_with_hint(output, hint)
        if hinted_output is None:
            if hint in output_text:
                if call_name == "wait":
                    stale_wait_outputs_hinted -= 1
                else:
                    unavailable_question_outputs_hinted -= 1
            continue
        updated_item = item.copy()
        updated_item["output"] = hinted_output
        updated_input[recent_start + offset] = updated_item

    if not stale_wait_outputs_hinted and not unavailable_question_outputs_hinted:
        return None

    removed_question_tools = 0
    if (
        unavailable_question_outputs_hinted
        and _codex_tool_choice_name(request_kwargs.get("tool_choice"))
        != "request_user_input"
    ):
        top_level_tools = request_kwargs.get("tools")
        if isinstance(top_level_tools, list):
            filtered_tools = [
                tool
                for tool in top_level_tools
                if _codex_tool_definition_name(tool) != "request_user_input"
            ]
            removed_question_tools += len(top_level_tools) - len(filtered_tools)
        else:
            filtered_tools = None

        for index, item in enumerate(updated_input):
            if not isinstance(item, dict) or item.get("type") != "additional_tools":
                continue
            item_tools = item.get("tools")
            if not isinstance(item_tools, list):
                continue
            filtered_item_tools = [
                tool
                for tool in item_tools
                if _codex_tool_definition_name(tool) != "request_user_input"
            ]
            removed_question_tools += len(item_tools) - len(filtered_item_tools)
            if len(filtered_item_tools) != len(item_tools):
                updated_item = item.copy()
                updated_item["tools"] = filtered_item_tools
                updated_input[index] = updated_item
    else:
        filtered_tools = None

    modified_kwargs = request_kwargs.copy()
    modified_kwargs["input"] = updated_input
    if filtered_tools is not None:
        modified_kwargs["tools"] = filtered_tools
    metadata = _request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    updated_metadata = metadata.copy()
    updated_metadata[_CODEX_TOOL_RUNTIME_RECOVERY_METADATA_KEY] = {
        "stale_wait_outputs_hinted": stale_wait_outputs_hinted,
        "unavailable_request_user_input_outputs_hinted": unavailable_question_outputs_hinted,
        "removed_request_user_input_tools": removed_question_tools,
    }
    modified_kwargs["litellm_metadata"] = updated_metadata
    return modified_kwargs


def _compact_history_text(
    text: str,
    *,
    max_chars: int,
    label: str,
) -> str:
    if len(text) <= max_chars:
        return text
    marker = (
        f"\n\n[LiteLLM Menu compacted historical {label}: "
        f"original_chars={len(text)}, kept_chars={max_chars}. "
        "Middle omitted before the compaction request to keep its input within context.]\n\n"
    )
    if len(marker) >= max_chars:
        return marker[:max_chars]
    remaining = max_chars - len(marker)
    head_chars = remaining // 2
    tail_chars = remaining - head_chars
    return text[:head_chars] + marker + text[-tail_chars:]


_CODEX_TOOL_OUTPUT_ITEM_TYPES = {
    "function_call_output",
    "custom_tool_call_output",
    "computer_call_output",
}

_CODEX_COMPACTION_STRUCTURAL_STRING_KEYS = {
    "call_id",
    "id",
    "item_id",
    "name",
    "role",
    "status",
    "type",
}


_RESPONSES_CONTEXT_FALLBACK_TOOL_CALL_ITEM_TYPES = {
    "computer_call",
    "custom_tool_call",
    "function_call",
}


def _responses_context_fallback_item_call_id(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    call_id = item.get("call_id")
    if isinstance(call_id, str) and call_id.strip():
        return call_id.strip()
    return None


def _responses_context_fallback_protected_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("type") == "additional_tools":
        return True
    role = str(item.get("role") or "").strip().lower()
    return role in {"developer", "system"}


def _responses_context_fallback_units(
    input_items: List[Any],
) -> List[List[int]]:
    """Group matching tool calls and outputs so truncation cannot orphan one."""
    call_indices: dict[str, List[int]] = {}
    output_indices: dict[str, List[int]] = {}
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        call_id = _responses_context_fallback_item_call_id(item)
        if call_id is None:
            continue
        item_type = item.get("type")
        if item_type in _RESPONSES_CONTEXT_FALLBACK_TOOL_CALL_ITEM_TYPES:
            call_indices.setdefault(call_id, []).append(index)
        elif item_type in _CODEX_TOOL_OUTPUT_ITEM_TYPES:
            output_indices.setdefault(call_id, []).append(index)

    grouped_indices: dict[int, List[int]] = {}
    consumed: set[int] = set()
    for call_id, calls in call_indices.items():
        matching_outputs = output_indices.get(call_id, [])
        if not matching_outputs:
            continue
        group = sorted(set(calls + matching_outputs))
        leader = group[0]
        grouped_indices[leader] = group
        consumed.update(group)

    units: List[List[int]] = []
    for index in range(len(input_items)):
        if index in grouped_indices:
            units.append(grouped_indices[index])
            continue
        if index in consumed:
            continue
        units.append([index])
    return units


def _responses_context_error_text_budget(
    exception: Exception,
    *,
    original_text_chars: int,
) -> int:
    """Choose a conservative retry budget, using upstream token counts when present."""
    default_budget = min(
        original_text_chars,
        _RESPONSES_CONTEXT_TRUNCATION_FALLBACK_HISTORY_TEXT_CHARS,
    )
    text = _routing_module._exception_text(exception)
    maximum_match = re.search(
        r"max(?:imum)?\s+context(?:\s+(?:length|window))?"
        r"(?:\s+is|\s+of|\s*:)?\s*([\d,]+)\s*tokens?",
        text,
    )
    input_match = re.search(
        r"(?:input\s+(?:contains|has)|prompt\s+(?:contains|has)|requested)"
        r"[^\d]{0,48}([\d,]+)\s*tokens?",
        text,
    )
    if maximum_match is None or input_match is None:
        return default_budget
    try:
        maximum_tokens = int(maximum_match.group(1).replace(",", ""))
        input_tokens = int(input_match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return default_budget
    if maximum_tokens <= 0 or input_tokens <= 0:
        return default_budget
    scaled_budget = int(
        original_text_chars * min(1.0, maximum_tokens / input_tokens) * 0.85
    )
    return max(16_000, min(original_text_chars, scaled_budget))


def _with_responses_context_auto_truncation_emulated(
    exception: Exception,
    request_kwargs: dict,
) -> Optional[tuple[dict, dict[str, Any]]]:
    """Emulate Responses ``truncation=auto`` for compatible upstreams.

    Some OpenAI-compatible endpoints accept the parameter but do not implement
    its required oldest-first item eviction.  This helper runs only after that
    endpoint has returned a context-window error.  It preserves protected
    instructions and a newest contiguous history suffix, and never separates a
    matching tool call from its output.
    """
    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list) or len(input_items) < 2:
        return None

    original_text_chars = _compaction_text_length(input_items)
    text_budget = _responses_context_error_text_budget(
        exception,
        original_text_chars=original_text_chars,
    )
    if original_text_chars <= text_budget:
        return None

    units = _responses_context_fallback_units(input_items)
    protected_units = {
        unit_index
        for unit_index, unit in enumerate(units)
        if any(
            _responses_context_fallback_protected_item(input_items[index])
            for index in unit
        )
    }
    newest_unit = next(
        (
            unit_index
            for unit_index, unit in enumerate(units)
            if len(input_items) - 1 in unit
        ),
        len(units) - 1,
    )
    selected_units = set(protected_units)
    selected_units.add(newest_unit)
    kept_text_chars = sum(
        _compaction_text_length(input_items[index])
        for unit_index in selected_units
        for index in units[unit_index]
    )

    for unit_index in reversed(range(len(units))):
        if unit_index in selected_units:
            continue
        unit = units[unit_index]
        unit_text_chars = sum(
            _compaction_text_length(input_items[index]) for index in unit
        )
        if kept_text_chars + unit_text_chars > text_budget:
            break
        selected_units.add(unit_index)
        kept_text_chars += unit_text_chars

    kept_indices = {
        index
        for unit_index in selected_units
        for index in units[unit_index]
    }
    if len(kept_indices) == len(input_items):
        return None

    modified_kwargs = request_kwargs.copy()
    modified_kwargs["input"] = [
        item for index, item in enumerate(input_items) if index in kept_indices
    ]
    stats = {
        "strategy": "protected-plus-newest-contiguous-suffix",
        "original_items": len(input_items),
        "kept_items": len(modified_kwargs["input"]),
        "dropped_items": len(input_items) - len(modified_kwargs["input"]),
        "original_text_chars": original_text_chars,
        "kept_text_chars": _compaction_text_length(modified_kwargs["input"]),
        "text_budget": text_budget,
        "protected_items": sum(
            len(units[unit_index]) for unit_index in protected_units
        ),
    }
    return modified_kwargs, stats


def _compaction_text_length(value: Any, *, depth: int = 0) -> int:
    if depth > 16:
        return 0
    if isinstance(value, str):
        if value.startswith("data:image/"):
            return 0
        return len(value)
    if isinstance(value, list):
        return sum(
            _compaction_text_length(item, depth=depth + 1)
            for item in value
        )
    if isinstance(value, dict):
        if value.get("type") in {"input_image", "image_url"}:
            return 0
        return sum(
            _compaction_text_length(item, depth=depth + 1)
            for key, item in value.items()
            if key not in _CODEX_COMPACTION_STRUCTURAL_STRING_KEYS
        )
    return 0


def _tool_output_text_length(value: Any, *, depth: int = 0) -> int:
    return _compaction_text_length(value, depth=depth)


def _tool_output_string_allocations(lengths: List[int], max_chars: int) -> List[int]:
    if not lengths:
        return []
    total = sum(lengths)
    if total <= max_chars:
        return list(lengths)

    allocations = [0] * len(lengths)
    remaining_budget = max(0, max_chars)
    remaining = sorted(range(len(lengths)), key=lambda index: lengths[index])
    while remaining:
        even_share, remainder = divmod(remaining_budget, len(remaining))
        smallest = remaining[0]
        if lengths[smallest] <= even_share:
            allocations[smallest] = lengths[smallest]
            remaining_budget -= lengths[smallest]
            remaining.pop(0)
            continue
        for position, index in enumerate(remaining):
            allocations[index] = even_share + (1 if position < remainder else 0)
        break
    return allocations


def _compact_history_value(
    value: Any,
    *,
    max_chars: int,
    label: str,
) -> Any:
    lengths: List[int] = []

    def collect(item: Any, depth: int = 0) -> None:
        if depth > 16:
            return
        if isinstance(item, str):
            if not item.startswith("data:image/"):
                lengths.append(len(item))
            return
        if isinstance(item, list):
            for nested in item:
                collect(nested, depth + 1)
            return
        if isinstance(item, dict):
            if item.get("type") in {"input_image", "image_url"}:
                return
            for key, nested in item.items():
                if key not in _CODEX_COMPACTION_STRUCTURAL_STRING_KEYS:
                    collect(nested, depth + 1)

    collect(value)
    allocations = iter(_tool_output_string_allocations(lengths, max_chars))

    def compact(item: Any, depth: int = 0) -> Any:
        if depth > 16:
            return copy.deepcopy(item)
        if isinstance(item, str):
            if item.startswith("data:image/"):
                return item
            allocation = next(allocations, len(item))
            return _compact_history_text(
                item,
                max_chars=allocation,
                label=label,
            )
        if isinstance(item, list):
            return [compact(nested, depth + 1) for nested in item]
        if isinstance(item, dict):
            if item.get("type") in {"input_image", "image_url"}:
                return copy.deepcopy(item)
            return {
                key: (
                    copy.deepcopy(nested)
                    if key in _CODEX_COMPACTION_STRUCTURAL_STRING_KEYS
                    else compact(nested, depth + 1)
                )
                for key, nested in item.items()
            }
        return copy.deepcopy(item)

    return compact(value)


def _recent_weighted_allocations(
    maximums: List[int],
    *,
    total_budget: int,
    minimums: Optional[List[int]] = None,
    protected_indices: Optional[List[int]] = None,
) -> List[int]:
    allocations = [0] * len(maximums)
    remaining = max(0, total_budget)
    minimums = minimums or [0] * len(maximums)

    for index in protected_indices or []:
        amount = min(maximums[index], minimums[index], remaining)
        allocations[index] += amount
        remaining -= amount

    for index in reversed(range(len(maximums))):
        amount = min(
            maximums[index] - allocations[index],
            max(0, minimums[index] - allocations[index]),
            remaining,
        )
        allocations[index] += amount
        remaining -= amount

    for index in reversed(range(len(maximums))):
        amount = min(maximums[index] - allocations[index], remaining)
        allocations[index] += amount
        remaining -= amount
        if remaining <= 0:
            break
    return allocations


def _compaction_history_fields(
    input_items: List[Any],
) -> List[tuple[int, str, str, int, int, int, bool]]:
    fields: List[tuple[int, str, str, int, int, int, bool]] = []
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        role = str(item.get("role") or "").strip().lower()
        if item_type in _CODEX_TOOL_OUTPUT_ITEM_TYPES:
            key = "output"
            label = "tool output"
            maximum = _compaction_text_length(item.get(key))
            minimum = min(maximum, _CODEX_COMPACTION_MIN_HISTORY_ITEM_CHARS)
            protected = False
        elif item_type in {"function_call", "custom_tool_call", "computer_call"}:
            key = "input" if "input" in item else "arguments"
            label = "tool call input"
            length = _compaction_text_length(item.get(key))
            maximum = min(length, _CODEX_COMPACTION_TOOL_CALL_ITEM_CHARS)
            minimum = min(maximum, _CODEX_COMPACTION_MIN_HISTORY_ITEM_CHARS)
            protected = False
        elif item_type == "message" or role in {
            "assistant",
            "developer",
            "system",
            "user",
        }:
            key = "content"
            label = "message text"
            length = _compaction_text_length(item.get(key))
            maximum = min(length, _CODEX_COMPACTION_MESSAGE_ITEM_CHARS)
            protected = role in {"developer", "system"}
            minimum = min(
                maximum,
                _CODEX_COMPACTION_DEVELOPER_MESSAGE_CHARS
                if protected
                else _CODEX_COMPACTION_MIN_HISTORY_ITEM_CHARS,
            )
        else:
            continue
        length = _compaction_text_length(item.get(key))
        if length:
            fields.append(
                (index, key, label, length, maximum, minimum, protected)
            )
    return fields


def _with_codex_compaction_input_bounded(
    request_kwargs: dict,
) -> Optional[dict]:
    if request_kwargs.get("use_chat_completions_api") is True:
        return None
    if not _request_has_responses_shape(request_kwargs):
        return None
    if not _request_is_codex_compaction(request_kwargs):
        return None

    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list):
        return None

    original_fields = _compaction_history_fields(input_items)
    original_history_text_chars = sum(field[3] for field in original_fields)
    updated_items = list(input_items)
    output_entries: List[tuple[int, int]] = []
    for index, item in enumerate(input_items):
        if (
            not isinstance(item, dict)
            or item.get("type") not in _CODEX_TOOL_OUTPUT_ITEM_TYPES
        ):
            continue
        output = item.get("output")
        output_length = _tool_output_text_length(output)
        if output_length:
            output_entries.append((index, output_length))

    total_output_chars = sum(length for _index, length in output_entries)
    truncated_count = 0
    if total_output_chars > _CODEX_TOOL_OUTPUT_COMPACT_TOTAL_CHARS:
        output_maximums = [
            min(length, _CODEX_TOOL_OUTPUT_COMPACT_ITEM_CHARS)
            for _index, length in output_entries
        ]
        output_minimums = [
            min(maximum, _CODEX_COMPACTION_MIN_HISTORY_ITEM_CHARS)
            for maximum in output_maximums
        ]
        output_allocations = _recent_weighted_allocations(
            output_maximums,
            total_budget=_CODEX_TOOL_OUTPUT_COMPACT_TOTAL_CHARS,
            minimums=output_minimums,
        )
        for (item_index, output_length), allocation in zip(
            output_entries,
            output_allocations,
        ):
            if output_length <= allocation:
                continue
            item = updated_items[item_index]
            assert isinstance(item, dict)
            updated_item = item.copy()
            updated_item["output"] = _compact_history_value(
                item.get("output"),
                max_chars=allocation,
                label="tool output",
            )
            updated_items[item_index] = updated_item
            truncated_count += 1

    fields = _compaction_history_fields(updated_items)
    history_text_chars_after_output_bound = sum(field[3] for field in fields)
    if history_text_chars_after_output_bound > _CODEX_COMPACTION_HISTORY_TEXT_CHARS:
        maximums = [field[4] for field in fields]
        minimums = [field[5] for field in fields]
        protected_indices = [
            index for index, field in enumerate(fields) if field[6]
        ]
        allocations = _recent_weighted_allocations(
            maximums,
            total_budget=_CODEX_COMPACTION_HISTORY_TEXT_CHARS,
            minimums=minimums,
            protected_indices=protected_indices,
        )
        for field, allocation in zip(fields, allocations):
            item_index, key, label, length, _maximum, _minimum, _protected = field
            if length <= allocation:
                continue
            item = updated_items[item_index]
            assert isinstance(item, dict)
            updated_item = item.copy()
            updated_item[key] = _compact_history_value(
                item.get(key),
                max_chars=allocation,
                label=label,
            )
            updated_items[item_index] = updated_item
            truncated_count += 1

    if truncated_count == 0:
        return None

    modified_kwargs = request_kwargs.copy()
    modified_kwargs["input"] = updated_items
    bounded_fields = _compaction_history_fields(updated_items)
    bounded_history_chars = sum(field[3] for field in bounded_fields)
    kept_output_chars = sum(
        _tool_output_text_length(item.get("output"))
        for item in updated_items
        if isinstance(item, dict)
        and item.get("type") in _CODEX_TOOL_OUTPUT_ITEM_TYPES
    )
    _trace_module._route_trace(
        "codex_compaction_input_bounded",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
        input_items=len(input_items),
        tool_call_outputs=len(output_entries),
        truncated_history_fields=truncated_count,
        original_output_chars=total_output_chars,
        compacted_output_chars=kept_output_chars,
        original_history_text_chars=original_history_text_chars,
        history_text_chars_after_output_bound=history_text_chars_after_output_bound,
        bounded_history_text_chars=bounded_history_chars,
        per_item_limit=_CODEX_TOOL_OUTPUT_COMPACT_ITEM_CHARS,
        tool_output_total_limit=_CODEX_TOOL_OUTPUT_COMPACT_TOTAL_CHARS,
        history_text_total_limit=_CODEX_COMPACTION_HISTORY_TEXT_CHARS,
    )
    return modified_kwargs


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
        metadata = _request_metadata_dict(request_kwargs, key)
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
    litellm_metadata = _request_metadata_dict(retry_kwargs, "litellm_metadata") or {}
    retry_metadata = litellm_metadata.copy()
    retry_metadata[_XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY] = True
    retry_kwargs["litellm_metadata"] = retry_metadata
    _trace_module._route_trace(
        "xhigh_reasoning_compat_retry_start",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
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


def _request_forces_image_generation_tool(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    tool_choice = request_kwargs.get("tool_choice")
    if tool_choice == "image_generation":
        return True
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "image_generation":
        return True
    return False


def _image_generation_tool_fallback_max_attempts() -> int:
    value = os.getenv(_IMAGE_GENERATION_TOOL_FALLBACK_MAX_ATTEMPTS_ENV, "").strip()
    if not value:
        return _IMAGE_GENERATION_TOOL_FALLBACK_DEFAULT_MAX_ATTEMPTS
    try:
        parsed = int(value)
    except ValueError:
        return _IMAGE_GENERATION_TOOL_FALLBACK_DEFAULT_MAX_ATTEMPTS
    return max(0, parsed)


def _request_image_generation_tool_fallback_attempts(request_kwargs: Optional[dict]) -> int:
    max_attempts = 0
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_metadata_dict(request_kwargs, key)
        if metadata is None:
            continue
        value = metadata.get(_IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY)
        if isinstance(value, int):
            max_attempts = max(max_attempts, value)
        elif isinstance(value, str) and value.strip().isdigit():
            max_attempts = max(max_attempts, int(value.strip()))
    return max_attempts


def _request_can_attempt_image_generation_tool_fallback(request_kwargs: Optional[dict]) -> bool:
    return (
        _request_image_generation_tool_fallback_attempts(request_kwargs)
        < _image_generation_tool_fallback_max_attempts()
    )


def _with_incremented_image_generation_tool_fallback_attempts(request_kwargs: dict) -> int:
    attempts = _request_image_generation_tool_fallback_attempts(request_kwargs) + 1
    litellm_metadata = _request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    updated_metadata = litellm_metadata.copy()
    updated_metadata[_IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY] = attempts
    request_kwargs["litellm_metadata"] = updated_metadata
    return attempts


def _image_generation_tool_runtime_fallback_exception() -> Exception:
    exception = RuntimeError("image_generation runtime fallback")
    try:
        exception.image_generation_tool_runtime_fallback = True  # type: ignore[attr-defined]
    except Exception:
        pass
    return exception


def _request_already_attempted_streaming_fallback(request_kwargs: Optional[dict]) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_metadata_dict(request_kwargs, key)
        if metadata is not None and metadata.get(_STREAM_FALLBACK_METADATA_KEY) is True:
            return True
    return False


def _request_already_attempted_streaming_error_fallback(request_kwargs: Optional[dict]) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_metadata_dict(request_kwargs, key)
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
    if request_kwargs.get("use_chat_completions_api") is True:
        return True
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_metadata_dict(request_kwargs, key)
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
        metadata = _request_metadata_dict(request_kwargs, key)
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


def _deployment_supports_responses_image_generation_tool(deployment: Any) -> bool:
    if not isinstance(deployment, dict):
        return False
    model_info = deployment.get("model_info")
    if not isinstance(model_info, dict):
        return False
    return model_info.get("supports_responses_image_generation_tool") is True


def _deployment_supports_vision(deployment: Any) -> bool:
    if not isinstance(deployment, dict):
        return False
    model_info = deployment.get("model_info")
    return isinstance(model_info, dict) and model_info.get("supports_vision") is True


def _deployment_allows_responses_image_input(deployment: Any) -> bool:
    if not isinstance(deployment, dict):
        return True
    model_info = deployment.get("model_info")
    if not isinstance(model_info, dict):
        return True
    return model_info.get(_RESPONSES_IMAGE_INPUT_SUPPORT_KEY) is not False


def _request_model_for_error(request_data: Optional[dict]) -> str:
    model = (request_data or {}).get("model")
    return model if isinstance(model, str) else ""


def _is_non_empty_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return value is not None


def _payload_has_tool_activity(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(_payload_has_tool_activity(item) for item in value)
    if isinstance(value, dict):
        item_type = value.get("type")
        if isinstance(item_type, str) and (
            item_type.endswith("_call")
            or item_type in {"function_call", "tool_call", "custom_tool_call"}
        ):
            return True
        tool_calls = value.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return True
        function_call = value.get("function_call")
        if _is_non_empty_value(function_call):
            return True
        if (
            isinstance(value.get("name"), str)
            and value.get("name").strip()
            and ("arguments" in value or "call_id" in value)
        ):
            return True
        return any(_payload_has_tool_activity(item) for item in value.values())
    if hasattr(value, "model_dump"):
        try:
            return _payload_has_tool_activity(value.model_dump())
        except Exception:
            return False
    return False


def _payload_has_visible_text(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_payload_has_visible_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("output_text", "text", "content", "delta"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return True
            if isinstance(text, list) and _payload_has_visible_text(text):
                return True
        for key in ("output", "choices", "message", "messages", "item", "response", "data"):
            item = value.get(key)
            if isinstance(item, (dict, list)) and _payload_has_visible_text(item):
                return True
        return False
    if hasattr(value, "model_dump"):
        try:
            return _payload_has_visible_text(value.model_dump())
        except Exception:
            return False
    return False


def _response_has_usable_output(response: Any) -> bool:
    return _payload_has_visible_text(response) or _payload_has_tool_activity(response)


def _response_is_effectively_empty(response: Any) -> bool:
    return not _response_has_usable_output(response)


def _response_types(response: Any) -> List[str]:
    found: List[str] = []

    def walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if isinstance(value, dict):
            item_type = value.get("type")
            if isinstance(item_type, str):
                found.append(item_type)
            for item in value.values():
                walk(item)
            return
        if hasattr(value, "model_dump"):
            try:
                walk(value.model_dump())
            except Exception:
                return

    walk(response)
    return found


def _response_text(response: Any) -> str:
    chunks: List[str] = []

    def walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if isinstance(value, dict):
            for key in ("output_text", "text", "content", "delta"):
                text = value.get(key)
                if isinstance(text, str):
                    chunks.append(text)
            for item in value.values():
                walk(item)
            return
        if hasattr(value, "model_dump"):
            try:
                walk(value.model_dump())
            except Exception:
                return

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        chunks.append(output_text)
    walk(response)
    return "\n".join(chunk for chunk in chunks if chunk)


def _response_has_image_generation_result(response: Any) -> bool:
    return "image_generation_call" in _response_types(response)


def _response_has_image_generation_activity(response: Any) -> bool:
    return any("image_generation_call" in item_type for item_type in _response_types(response))

def _response_is_async_iterable(response: Any) -> bool:
    return callable(getattr(response, "__aiter__", None))


def _response_is_image_generation_unavailable_refusal(response: Any) -> bool:
    if _response_has_image_generation_result(response):
        return False
    text = _response_text(response).lower()
    if not text:
        return False
    normalized_text = (
        text.replace("`", "")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
    )
    compact_text = "".join(normalized_text.replace("'", "").split())
    mentions_image_generation = (
        "image_generation" in text
        or "image generation" in text
        or "image_gen" in text
        or "imagegen" in text
        or "image_generation" in compact_text
        or "imagegeneration" in compact_text
        or "image_gen" in compact_text
        or "imagegen" in compact_text
    )
    unavailable = mentions_image_generation and (
        "not available" in text
        or "isn't available" in text
        or "is not available" in text
        or "not directly available" in text
        or "not directly exposed" in text
        or "notavailable" in compact_text
        or "isntavailable" in compact_text
        or "isnotavailable" in compact_text
        or "don't have access" in text
        or "don’t have access" in text
        or "t have access" in text
        or "do not have access" in text
        or "no access" in text
        or "donthaveaccess" in compact_text
        or "nothaveaccess" in compact_text
        or "noaccess" in compact_text
        or "can't complete" in text
        or "cannot complete" in text
        or "cantcomplete" in compact_text
        or "cannotcomplete" in compact_text
        or ("no " in text and "tool" in text)
        or ("没有" in text and ("工具" in text or "可用" in text or "调用" in text))
        or ("没有" in compact_text and ("工具" in compact_text or "可用" in compact_text or "调用" in compact_text))
        or ("无可用" in text and "工具" in text)
        or ("无可用" in compact_text and "工具" in compact_text)
        or ("不可用" in text and "工具" in text)
        or ("不可用" in compact_text and "工具" in compact_text)
        or ("无法生成" in text and "工具" in text)
        or ("无法生成" in compact_text and "工具" in compact_text)
        or "imagegen_tool_unavailable" in compact_text
        or "image_generation_tool_unavailable" in compact_text
        or "builtin_imagegen_tool_unavailable" in compact_text
        or ("imagegen_result" in compact_text and "status=fail" in compact_text)
        or ("imagegen_result" in compact_text and "status:fail" in compact_text)
        or ("imagegen" in compact_text and "tool_unavailable" in compact_text)
        or ("imagegen" in compact_text and "toolunavailable" in compact_text)
    )
    return unavailable


def _response_should_trigger_image_generation_fallback(response: Any) -> bool:
    return _response_is_effectively_empty(response) or _response_is_image_generation_unavailable_refusal(response)
