from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .. import browser_identity as _browser_identity
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
    _PREFIX_IMAGE_PREVIEW_ENABLED_DEFAULT,
    _PREFIX_IMAGE_PREVIEW_ENABLED_ENV,
    _PREFIX_IMAGE_PREVIEW_MIN_BYTES_DEFAULT,
    _PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV,
    _HISTORY_IMAGE_ELISION_DEFAULT_KEEP,
    _HISTORY_IMAGE_ELISION_DEFAULT_MIN_BYTES,
    _HISTORY_IMAGE_ELISION_KEEP_ENV,
    _HISTORY_IMAGE_ELISION_MIN_BYTES_ENV,
    _HISTORY_TOOL_OUTPUT_CHARS_PER_TOKEN,
    _HISTORY_TOOL_OUTPUT_DEFAULT_KEPT_CHARS,
    _HISTORY_TOOL_OUTPUT_DEFAULT_MIN_CHARS,
    _HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV,
    _HISTORY_TOOL_OUTPUT_LATENCY_BUDGET_ENV,
    _HISTORY_TOOL_OUTPUT_MIN_CHARS_ENV,
    _PREFIX_IMAGE_MODE_DEFAULT,
    _PREFIX_IMAGE_MODE_ENV,
    _PREFIX_IMAGE_MODE_VALUES,
    _PREFIX_IMAGE_RECENT_COUNT_DEFAULT,
    _PREFIX_IMAGE_RECENT_COUNT_ENV,
    _PREFIX_IMAGE_ORIGINAL_PATH_DEFAULT,
    _PREFIX_IMAGE_ORIGINAL_PATH_ENV,
    _PREFIX_IMAGE_CACHE_DIR_ENV,
    _CHAT_COMPAT_REASONING_EFFORT,
    _FALLBACK_BROWSER_USER_AGENT,
    _MAX_COMPAT_REASONING_EFFORT,
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
    _SUPPORTS_RESPONSES_WEB_SEARCH_KEY,
    _SUPPORTS_WEB_SEARCH_KEY,
    _RouteOrder,
    _STREAM_ERROR_FALLBACK_METADATA_KEY,
    _STREAM_FALLBACK_METADATA_KEY,
    _UPSTREAM_METADATA_FORWARD_FLAGS,
    _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
    _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
    _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    _XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY,
    _XHIGH_REASONING_EFFORT,
    _value_has_encrypted_content,
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


def _with_dsh_messages_plaintext_content_restored(
    request_kwargs: dict,
) -> Optional[dict]:
    """Recover DSH text that an upstream returned as encrypted placeholders.

    The DeepSeek Harness search provider sends a hidden Messages turn in
    plaintext. Some OpenAI-compatible adapters return `encrypted_content` for
    text blocks even when the value clearly contains readable text. Carrying it
    forward as ciphertext makes the next turn look empty, so restore only the
    values that satisfy the strict plaintext test used for Codex agent messages.
    """

    messages = request_kwargs.get("messages")
    if not isinstance(messages, list):
        return None
    updated_messages: list[Any] = []
    changed = False
    for message in messages:
        if not isinstance(message, dict):
            updated_messages.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            updated_messages.append(message)
            continue
        updated_content: list[Any] = []
        item_changed = False
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "encrypted_content"
                and _agent_message_encrypted_part_is_plain_text(
                    part.get("encrypted_content")
                )
            ):
                updated_content.append(
                    {
                        "type": "text",
                        "text": part["encrypted_content"],
                    }
                )
                item_changed = True
            else:
                updated_content.append(part)
        if not item_changed:
            updated_messages.append(message)
            continue
        updated_message = message.copy()
        updated_message["content"] = updated_content
        updated_messages.append(updated_message)
        changed = True
    if not changed:
        return None
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["messages"] = updated_messages
    return modified_kwargs


def _request_has_responses_shape(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    return _request_is_responses_api(request_kwargs) or "input" in request_kwargs


_REPLAY_PAYLOAD_BYTES_METADATA_KEY = "young_router_replay_payload_bytes"


def _request_replay_payload_bytes(request_kwargs: Optional[dict]) -> Optional[int]:
    """Serialized byte weight of a Responses replay payload (best effort).

    The weight covers the request ``input`` replay prefix plus ``instructions``.
    Callers derive stream-start budgets from it several times per request, and
    serializing a multi-megabyte encrypted prefix on every call would dominate
    the request path, so the value is cached inside an existing proxy metadata
    channel when one is present.  The raw request body is never mutated and no
    new key is ever added to the payload that is forwarded upstream.
    """

    if not isinstance(request_kwargs, dict) or "input" not in request_kwargs:
        return None
    metadata: Optional[dict] = None
    for metadata_key in ("litellm_metadata", "metadata"):
        candidate = _request_context_module._request_metadata_dict(
            request_kwargs,
            metadata_key,
        )
        if isinstance(candidate, dict):
            metadata = candidate
            break
    if metadata is not None:
        cached = metadata.get(_REPLAY_PAYLOAD_BYTES_METADATA_KEY)
        if isinstance(cached, int) and cached >= 0:
            return cached
    payload = {
        key: request_kwargs[key]
        for key in ("input", "instructions")
        if request_kwargs.get(key) is not None
    }
    try:
        weight = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:
        return None
    if metadata is not None:
        try:
            metadata[_REPLAY_PAYLOAD_BYTES_METADATA_KEY] = weight
        except Exception:
            pass
    return weight


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

def _with_owned_user_agent_header(request_kwargs: dict) -> Optional[dict]:
    """Guarantee one outgoing User-Agent that never names this router.

    A downstream client's own User-Agent is forwarded byte-for-byte by
    ``_with_incoming_user_agent_header`` and stays untouched here.  Two cases
    are repaired: a request that carries no User-Agent at all (the router's own
    calls and helper workers build their own requests, and the SDK default
    would name a Python client) and a User-Agent that names this app (only our
    own components can send it, and a relay must never learn it).  Both present
    the shared browser identity instead.
    """

    existing_headers = request_kwargs.get("extra_headers")
    merged_headers: Dict[str, str] = (
        existing_headers.copy() if isinstance(existing_headers, dict) else {}
    )
    user_agent_key = _header_key(merged_headers, "User-Agent")
    if user_agent_key is not None and not _browser_identity.is_self_identifying_user_agent(
        merged_headers[user_agent_key]
    ):
        return None

    merged_headers[user_agent_key or "User-Agent"] = _browser_identity.browser_user_agent()
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["extra_headers"] = merged_headers
    return modified_kwargs


def _with_incoming_user_agent_header(request_kwargs: dict) -> Optional[dict]:
    incoming_user_agent = _incoming_request_user_agent(request_kwargs)
    if not incoming_user_agent or _browser_identity.is_self_identifying_user_agent(incoming_user_agent):
        # Our own components can be a "client" of this proxy; their identity is
        # ours and must not be forwarded upstream.  ``_with_owned_user_agent_header``
        # supplies the browser identity instead.
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
        # A compaction request never takes the truncation fallback, however
        # large its prefix: the upstream compacts exactly what it receives, so
        # an injected ``truncation=auto`` would drop signed replay items and
        # bake that loss into the history the client keeps.  Encrypted history
        # is an immutable replay prefix — never recompress, re-encode, reorder,
        # drop, or path-rewrite a signed item.
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

    # Codex Desktop declares its local tool registry in a leading
    # ``additional_tools`` item.  LiteLLM may omit the identifying headers and
    # client metadata in its reduced context, but the ``functions`` namespace
    # is the native Codex marker.  Generic Responses clients also use
    # ``additional_tools`` for arbitrary tools, so the item alone is not
    # sufficient evidence.
    if _request_has_codex_additional_tools_namespace(request_kwargs):
        return True

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


def _request_has_codex_additional_tools_namespace(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    input_value = request_kwargs.get("input")
    if not isinstance(input_value, list):
        return False
    for item in input_value:
        if not isinstance(item, dict) or item.get("type") != "additional_tools":
            break
        item_tools = item.get("tools")
        if not isinstance(item_tools, list):
            continue
        if any(
            isinstance(tool, dict)
            and tool.get("type") == "namespace"
            and tool.get("name") == "functions"
            for tool in item_tools
        ):
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
    """Every tool this request declares, from all three carriers it uses.

    ``additional_tools`` is read wherever it appears, not only while the input
    still begins with one.  Codex sends its always-on registry as a leading
    item but appends the deferred registry -- the tools reachable only after a
    ``tool_search`` -- behind the conversation it belongs to.  Reading the
    leading run alone therefore made every deferred tool (``view_image``,
    ``exec_command``, ...) look undeclared even though the bridge faithfully
    forwarded it, and each call to one was answered with a terminal failure
    that stopped the turn.  The bridge lifts tools through
    :func:`responses_tools._responses_input_additional_tools`, which already
    reads every item, so the contract that judges the model's answer has to
    read the same set the request actually declared.
    """
    if not isinstance(request_kwargs, dict):
        return []
    tools = request_kwargs.get("tools")
    declared = [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []
    input_value = request_kwargs.get("input")
    if isinstance(input_value, list):
        from . import responses_tools as _responses_tools_module

        declared.extend(
            _responses_tools_module._responses_input_additional_tools(input_value)
        )
        # Codex loads deferred tools before the model can call them. The
        # resulting tool_search_output is part of the request's declared
        # surface just like an additional_tools item. Excluding it turns a
        # valid deferred call into an undeclared-tool terminal error.
        declared.extend(
            _responses_tools_module._responses_input_tool_search_output_tools(
                input_value
            )
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


_CODEX_GPT_RESPONSES_PROVIDER_NAMES = frozenset({"openai", "chatgpt"})


def _codex_selected_provider_name(request_kwargs: Optional[dict]) -> str:
    if not isinstance(request_kwargs, dict):
        return ""
    model_info = _request_context_module._request_model_info(request_kwargs)
    provider = model_info.get("provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip().lower()
    for value in (
        request_kwargs.get("custom_llm_provider"),
        (request_kwargs.get("litellm_params") or {}).get("custom_llm_provider")
        if isinstance(request_kwargs.get("litellm_params"), dict)
        else None,
    ):
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _codex_request_responses_surface(request_kwargs: Optional[dict]) -> str:
    if not isinstance(request_kwargs, dict):
        return ""
    current_surface = _routing_module._request_current_upstream_surface(request_kwargs)
    if current_surface:
        return current_surface
    model_info = _request_context_module._request_model_info(request_kwargs)
    configured_surface = model_info.get("upstream_url_surface")
    if isinstance(configured_surface, str) and configured_surface.strip():
        return configured_surface.strip().lower()
    if request_kwargs.get("use_chat_completions_api") is True:
        return _UPSTREAM_URL_SURFACE_OPENAI_CHAT
    return (
        _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
        if _request_is_responses_api(request_kwargs)
        else ""
    )


_CODEX_GPT_FAMILY_MODEL_RE = re.compile(r"^gpt-[0-9]", re.IGNORECASE)


def _codex_gpt_family_model_name(value: Any) -> bool:
    """Return whether a public model name belongs to the GPT family.

    The digit after the ``gpt-`` prefix keeps this narrow: ``gpt-5.6-sol``
    and ``gpt-4.1`` match, while image/audio families such as
    ``gpt-image-2`` do not.  Keep this aligned with the catalog-side helper
    ``gpt_family_model_name`` in ``young_router.core.model_contexts``.
    """

    if not isinstance(value, str):
        return False
    return bool(_CODEX_GPT_FAMILY_MODEL_RE.match(value.strip()))


def _codex_gpt_hosted_search_capability_probe_request(
    request_kwargs: dict,
) -> dict:
    """Return a probe-shaped copy for route-local native-search state.

    The rejection cache is keyed by the tool family that failed.  A plain
    Codex turn may carry no hosted declaration yet, so consult the cache with
    the hosted ``web_search`` declaration that this hook would forward.  The
    probe does not expose the local bridge or mutate the caller request.
    """

    if any(
        _codex_tool_is_hosted_web_search(tool)
        for tool in _codex_declared_tools(request_kwargs)
    ):
        return request_kwargs
    probe = request_kwargs.copy()
    tools = probe.get("tools")
    if not isinstance(tools, list):
        tools = []
    probe["tools"] = [*tools, {"type": "web_search"}]
    return probe


def _request_is_codex_gpt_responses_route(request_kwargs: Optional[dict]) -> bool:
    """Return whether Codex may use the GPT Responses native search tool.

    A generic ``openai/responses`` adapter is not enough: LiteLLM uses that
    surface for many third-party gateways.  Provider identity, the official
    OpenAI origin, or a GPT-family public model identifies a GPT-compatible
    route.  A GPT-family model with unknown capability defaults to native
    hosted search so the original hosted-search request stays eligible for
    the upstream; only an explicit false capability or a deterministic
    rejection cached for this exact request and route hands the turn to the
    local search bridge.
    """

    if not isinstance(request_kwargs, dict) or not _request_has_responses_shape(request_kwargs):
        return False
    if request_kwargs.get("use_chat_completions_api") is True:
        return False
    surface = _codex_request_responses_surface(request_kwargs)
    if surface and surface != _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        return False

    model_info = _request_context_module._request_model_info(request_kwargs)
    if (
        model_info.get(_SUPPORTS_RESPONSES_WEB_SEARCH_KEY) is False
        or model_info.get(_SUPPORTS_WEB_SEARCH_KEY) is False
    ):
        return False

    if _api_base_host(_request_api_base(request_kwargs)) == "api.openai.com":
        return True
    if _codex_selected_provider_name(request_kwargs) in _CODEX_GPT_RESPONSES_PROVIDER_NAMES:
        return True

    if _codex_gpt_family_model_name(
        _request_context_module._request_model_group(request_kwargs)
    ):
        from . import responses_surfaces as _responses_surfaces_module

        probe = _codex_gpt_hosted_search_capability_probe_request(request_kwargs)
        support = _responses_surfaces_module._request_native_responses_web_search_support_decision(
            probe
        )
        if support is not False:
            return True

    # Route identity is intentionally required otherwise. A standalone
    # capability flag without an identified provider, official endpoint, or
    # GPT-family model is not proof that a third-party gateway implements
    # GPT hosted search.
    return False
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
    rejected by the short probe cache, is left to the local search bridge.
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


_CODEX_VIEW_IMAGE_PATH_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
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


def _codex_view_image_call_source(item: Any) -> Optional[str]:
    """Return the exec script text of a view-image tool call item.

    Codex stores local tool calls as ``custom_tool_call`` in its rollout, but
    replayed wire history carries them as ``function_call`` items with the
    script under ``arguments``.  Both shapes share ``name == "exec"`` and a
    plain-string script field.
    """

    if (
        not isinstance(item, dict)
        or item.get("name") != "exec"
        or item.get("type") not in {"custom_tool_call", "function_call"}
    ):
        return None
    for key in ("input", "arguments"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _codex_view_image_call_source(item: Any) -> Optional[str]:
    """Return the exec script text of a view-image tool call item.

    Codex stores local tool calls as ``custom_tool_call`` in its rollout, but
    replayed wire history carries them as ``function_call`` items with the
    script under ``arguments``.  Both shapes share ``name == "exec"`` and a
    plain-string script field.  A JSON-encoded string (double-encoded script)
    is unwrapped so path literals are matched against the real script text.
    """

    if (
        not isinstance(item, dict)
        or item.get("name") != "exec"
        or item.get("type") not in {"custom_tool_call", "function_call"}
    ):
        return None
    for key in ("input", "arguments"):
        value = item.get(key)
        if not isinstance(value, str) or not value:
            continue
        stripped = value.strip()
        if not stripped:
            continue
        # A JSON-encoded string (double-encoded script) is unwrapped once.
        if stripped.startswith('"'):
            try:
                unwrapped = json.loads(stripped)
            except (TypeError, ValueError):
                unwrapped = None
            if isinstance(unwrapped, str) and unwrapped:
                return unwrapped
        # Wire replay serializes the call as {"input": "<script>", ...}.
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except (TypeError, ValueError):
                obj = None
            if isinstance(obj, dict):
                for inner_key in ("input", "command", "path", "script"):
                    inner = obj.get(inner_key)
                    if isinstance(inner, str) and inner:
                        return inner
        return value
    return None


def _codex_view_image_path_literal_value(literal: str) -> Optional[str]:
    """Decode one quoted path literal (double- or single-quoted)."""

    try:
        value = json.loads(literal)
    except (TypeError, ValueError):
        value = literal[1:-1] if len(literal) >= 2 else None
    return value if isinstance(value, str) else None


def _codex_view_image_native_call_arguments(item: Any) -> Optional[dict]:
    """Return the JSON arguments of a native ``view_image`` function call.

    The client ships two shapes for the same capability.  The older one is a
    script run through ``exec``; the current one is a dedicated ``view_image``
    function call whose ``arguments`` carry ``{"path": ..., "detail": ...}``.
    Only the ``exec`` shape was recognized here, so the current shape's path
    never reached the replay passes: the image it produced looked like one with
    nobody able to reopen it, and an unsigned history dropped it for a note
    telling the model to run the tool again -- which the model did, forever.
    """

    if (
        not isinstance(item, dict)
        or item.get("name") != "view_image"
        or item.get("type") not in {"custom_tool_call", "function_call"}
    ):
        return None
    for key in ("arguments", "input"):
        value = item.get(key)
        if isinstance(value, dict):
            return value
        if not isinstance(value, str) or not value.strip():
            continue
        stripped = value.strip()
        if stripped.startswith('"'):
            try:
                unwrapped = json.loads(stripped)
            except (TypeError, ValueError):
                unwrapped = None
            if isinstance(unwrapped, str):
                stripped = unwrapped.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _codex_view_image_argument_paths(item: Any) -> list[str]:
    """Absolute image paths named by a native ``view_image`` function call."""

    arguments = _codex_view_image_native_call_arguments(item)
    if not arguments:
        return []
    value = arguments.get("path")
    if not isinstance(value, str) or not value:
        return []
    normalized = value.replace("\\\\", "\\")
    if not (
        normalized.startswith("/") or re.match(r"^[A-Za-z]:[\\\\/]", normalized)
    ):
        return []
    if not normalized.lower().endswith(_CODEX_VIEW_IMAGE_EXTENSIONS):
        return []
    return [normalized]


def _codex_view_image_paths_from_call(item: Any) -> list[str]:
    native_paths = _codex_view_image_argument_paths(item)
    if native_paths:
        return native_paths
    source = _codex_view_image_call_source(item)
    if source is None or "view_image" not in source:
        return []
    paths: list[str] = []
    for literal in _CODEX_VIEW_IMAGE_PATH_LITERAL.findall(source):
        value = _codex_view_image_path_literal_value(literal)
        if not value:
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
    arguments = _codex_view_image_native_call_arguments(item)
    if arguments is not None:
        detail = arguments.get("detail")
        return isinstance(detail, str) and detail.strip().lower() == "original"
    source = _codex_view_image_call_source(item)
    if source is None:
        return False
    return bool(
        re.search(
            r"(?:detail|['\"]detail['\"])[\s:]+['\"]original['\"]",
            source,
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
        if item.get("type") in {"custom_tool_call", "function_call"}:
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
        if index <= last_encrypted_index or item.get("type") not in {
            "custom_tool_call_output",
            "function_call_output",
        }:
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
                "the inline image below carries the highest resolution this "
                "route forwards. For the original file, call view_image on the "
                "matching local path:\n"
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


_PREFIX_IMAGE_PREVIEW_CACHE_MAX_ENTRIES = 64
_PREFIX_IMAGE_PREVIEW_CACHE: "OrderedDict[str, str]" = OrderedDict()
_PREFIX_IMAGE_PREVIEW_CACHE_LOCK = threading.Lock()


def _prefix_image_preview_enabled() -> bool:
    value = os.getenv(_PREFIX_IMAGE_PREVIEW_ENABLED_ENV, "").strip().lower()
    if not value:
        return _PREFIX_IMAGE_PREVIEW_ENABLED_DEFAULT
    return value not in {"0", "false", "off", "no", "disabled"}


def _prefix_image_mode() -> str:
    value = os.getenv(_PREFIX_IMAGE_MODE_ENV, "").strip().lower()
    if value in _PREFIX_IMAGE_MODE_VALUES:
        return value
    return _PREFIX_IMAGE_MODE_DEFAULT


def _prefix_image_recent_count() -> int:
    raw = os.getenv(_PREFIX_IMAGE_RECENT_COUNT_ENV, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed
    return _PREFIX_IMAGE_RECENT_COUNT_DEFAULT


def _prefix_image_original_path_enabled() -> bool:
    """Whether original-resolution outputs join the path-recent conversion.

    When enabled (default), view_image outputs explicitly requested at
    ``detail: original`` are replayed like every other historical output: a
    deterministic preview plus the matching local path inside the recent
    window, and a pure local-path reference once they age out.  The original
    bytes are never replayed inline, because one task that re-opens several
    crops at ``detail: original`` would otherwise carry tens of megabytes of
    history on every turn.  Disable to replay explicit original-resolution
    requests unchanged forever.
    """

    value = os.getenv(_PREFIX_IMAGE_ORIGINAL_PATH_ENV, "").strip().lower()
    if not value:
        return _PREFIX_IMAGE_ORIGINAL_PATH_DEFAULT
    return value not in {"0", "false", "off", "no", "disabled"}


def _prefix_image_preview_min_bytes() -> int:
    raw = os.getenv(_PREFIX_IMAGE_PREVIEW_MIN_BYTES_ENV, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return _PREFIX_IMAGE_PREVIEW_MIN_BYTES_DEFAULT


def _prefix_image_preview_resized(image_url: str) -> str:
    """Deterministically shrink one oversized replay image to its preview.

    Signed-prefix history replays on every request, so the resized preview is
    cached by the original data-URL digest; without the cache every worker
    would re-encode megabytes of PNG on each turn.  An image that cannot be
    decoded is cached unchanged so a malformed entry never breaks the request
    and is never retried.
    """

    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
    with _PREFIX_IMAGE_PREVIEW_CACHE_LOCK:
        cached = _PREFIX_IMAGE_PREVIEW_CACHE.get(digest)
        if cached is not None:
            _PREFIX_IMAGE_PREVIEW_CACHE.move_to_end(digest)
            return cached
    try:
        resized = _image_inputs_module._resize_data_url(
            image_url,
            target_bytes=_image_inputs_module._CODEX_VIEW_IMAGE_PREVIEW_MIN_TARGET_BYTES,
            max_edge=_image_inputs_module._INLINE_IMAGE_MANY_MAX_EDGE,
        )
    except Exception as exc:
        # Falling back to the original bytes is the right answer for one
        # undecodable image, but a missing Pillow makes *every* image slide
        # through and looks identical to "nothing needed shrinking".  Record
        # the cause so a deployment that forgot the dependency is visible in
        # the trace instead of only as unexplained slowness.
        resized = image_url
        _trace_module._route_trace(
            "prefix_image_preview_failed",
            reason=type(exc).__name__,
            detail=str(exc)[:200],
            bytes=len(image_url),
        )
    with _PREFIX_IMAGE_PREVIEW_CACHE_LOCK:
        _PREFIX_IMAGE_PREVIEW_CACHE[digest] = resized
        while len(_PREFIX_IMAGE_PREVIEW_CACHE) > _PREFIX_IMAGE_PREVIEW_CACHE_MAX_ENTRIES:
            _PREFIX_IMAGE_PREVIEW_CACHE.popitem(last=False)
    return resized


def _configured_replay_image_cache_root() -> Optional[Path]:
    """Return the operator-configured copy directory, when one is set."""

    configured = os.getenv(_PREFIX_IMAGE_CACHE_DIR_ENV, "").strip()
    return Path(configured).expanduser() if configured else None


def _replay_image_copy_root(source: Optional[Path]) -> Path:
    """Return the directory an advertised replay copy belongs in.

    A copy belongs next to the file it was made from, in that file's own
    directory: the model derives every crop path from the path it reads, so
    keeping the copy there leaves its whole read/crop/write workflow where the
    source lives instead of moving it into a router-created folder. A source
    inside the router's storage (or with no usable directory) maps to OS
    temporary storage, which is where a clipboard paste came from anyway; an
    operator-configured ``YOUNG_ROUTER_IMAGE_CACHE_DIR`` overrides both.
    """

    configured = _configured_replay_image_cache_root()
    if configured is not None:
        return configured
    if source is not None:
        parent = source.parent
        if str(parent) not in {"", "."} and _router_storage_relative_path(parent) is None:
            try:
                if parent.is_dir():
                    return parent
                if not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                    return parent
            except OSError:
                pass
    return Path(tempfile.gettempdir())


def _replay_image_copy_name(raw: bytes, source: Optional[Path], extension: str) -> str:
    """Name one copy after its digest and, when known, its source file."""

    digest = hashlib.sha256(raw).hexdigest()[:32]
    stem = ""
    if source is not None:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._-")[:32]
    return f"{digest}.{stem}{extension}" if stem else f"{digest}{extension}"


def _future_replay_image_copy_path(source: Path) -> str:
    """Return where a file a command is about to write would be kept."""

    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:32]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._-")[:32]
    extension = source.suffix.lower()
    root = _replay_image_copy_root(source)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    name = f"{digest}.{stem}{extension}" if stem else f"{digest}{extension}"
    return str(root / name)


def _temporary_path_roots() -> List[Path]:
    roots: List[Path] = []
    for candidate in (
        tempfile.gettempdir(),
        os.getenv("TMPDIR", "").strip(),
        "/tmp",
        "/var/tmp",
        "/private/tmp",
        "/private/var/tmp",
    ):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _path_lives_in_temporary_storage(path: Path) -> bool:
    """True when the OS owns the file's lifetime, not the user's task."""

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in _temporary_path_roots():
        if resolved == root or root in resolved.parents:
            return True
    # macOS per-user temp trees: /var/folders/<xx>/<yyy>/T/...
    parts = resolved.parts
    return any(
        parts[index] == "var" and parts[index + 1] == "folders"
        for index in range(len(parts) - 1)
    )


def _router_storage_roots() -> List[Path]:
    """Return the directories that belong to the router, not to the client.

    A file inside them exists, but advertising it tells the client model that
    the router exists, where it keeps its configuration and caches, and gives
    it a directory to read, crop, and rewrite while the user watches. The
    attachment folders earlier releases copied into belong here too: a reference
    that points at one of them keeps the model working inside a router-created
    folder instead of the source file's own directory.
    """

    home = Path.home()
    candidates = [
        os.getenv("LITELLM_RUNTIME_ROOT", "").strip(),
        os.getenv("YOUNG_ROUTER_HOME", "").strip(),
        str(home / ".young-router"),
        str(home / "Library" / "ImageAttachments"),
        str(home / ".local" / "share" / "ImageAttachments"),
    ]
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(str(Path(local_app_data).expanduser() / "ImageAttachments"))
    for candidate in (
        os.getenv("LITELLM_RUNTIME_ROOT", "").strip(),
        os.getenv("YOUNG_ROUTER_HOME", "").strip(),
        str(home / ".young-router"),
    ):
        if candidate:
            candidates.append(str(Path(candidate).expanduser() / "image-cache"))
    roots: List[Path] = []
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _router_storage_relative_path(path: Path) -> Optional[Path]:
    """Return a router-owned path's position below its storage root."""

    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    configured_root = _configured_replay_image_cache_root()
    if configured_root is not None:
        try:
            configured_root = configured_root.resolve()
        except OSError:
            pass
        if resolved == configured_root or configured_root in resolved.parents:
            return None
    for root in _router_storage_roots():
        if resolved == root:
            return Path(resolved.name)
        if root in resolved.parents:
            return resolved.relative_to(root)
    return None


# Any absolute path token in a replayed script or tool output: quoted,
# backticked, or bare. A trailing glob character ends a token without
# rewriting it, because a glob is not the file a command will open.
_ROUTER_PATH_TOKEN = re.compile(r"/(?:[^\s'\"`|&;<>()\[\]{},:?*!$\\]+)")


def _restore_missing_replay_paths(input_items: list[Any]) -> int:
    """Rewrite a vanished client image file from the bytes the client resends.

    A clipboard paste or an intermediate crop the client has since deleted is
    still part of the replayed history, and a re-open against it fails with "no
    such file" -- which sends the model back to crop and view the same region
    again. The bytes are still in the same request (the client resends its whole
    history every turn), so the file is restored at the exact path the replay
    names and the call reads what it always read.
    """

    images_by_call: dict[str, list[str]] = {}
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"custom_tool_call_output", "function_call_output"}:
            continue
        call_id = item.get("call_id") or item.get("id")
        output = item.get("output")
        if not isinstance(call_id, str) or not isinstance(output, list):
            continue
        images = [
            part["image_url"]
            for part in output
            if isinstance(part, dict)
            and part.get("type") == "input_image"
            and isinstance(part.get("image_url"), str)
            and part["image_url"].startswith("data:image/")
        ]
        if images:
            images_by_call.setdefault(call_id, []).extend(images)

    restored = 0
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") not in {
            "custom_tool_call",
            "function_call",
        }:
            continue
        call_id = item.get("call_id") or item.get("id")
        images = images_by_call.get(call_id) if isinstance(call_id, str) else None
        if not images:
            continue
        source = item.get("input") if isinstance(item.get("input"), str) else item.get("arguments")
        if not isinstance(source, str) or not source:
            continue
        index = 0
        for match in _ROUTER_PATH_TOKEN.finditer(source):
            candidate = Path(match.group(0))
            if candidate.suffix.lower() not in _CODEX_VIEW_IMAGE_EXTENSIONS:
                continue
            if _router_storage_relative_path(candidate) is not None:
                # A router-owned source is copied and rewritten by the caller;
                # it never consumes the output's image slots.
                continue
            try:
                if candidate.is_file():
                    continue
            except OSError:
                pass
            image_url = images[index] if index < len(images) else None
            index += 1
            if image_url is None:
                continue
            parsed = _image_inputs_module._split_image_data_url(image_url)
            if parsed is None:
                continue
            try:
                raw = base64.b64decode(parsed[1], validate=False)
            except (binascii.Error, ValueError):
                continue
            if not raw:
                continue
            try:
                # The path belongs to a filesystem this proxy may not see.  A
                # missing parent is not restored into existence: on a remote
                # gateway that would invent a directory tree (/Users, /home) the
                # client never had, and the file itself would still be
                # unreachable from the machine that runs view_image.
                if not candidate.parent.is_dir():
                    continue
                tmp_path = candidate.with_name(f".{candidate.name}.tmp.{os.getpid()}.{time.time_ns()}")
                with open(tmp_path, "wb") as handle:
                    handle.write(raw)
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass
                os.replace(tmp_path, candidate)
            except OSError:
                continue
            restored += 1
    return restored


def _neutral_attachment_copy(source_text: str) -> Optional[str]:
    """Return the neutral path that replaces one router-owned image path.

    The model reads its own past commands back, so a router-owned literal keeps
    pulling the model -- and every crop it derives -- into the router's storage.
    The replacement is written where the copy policy puts it (next to its
    source, or OS temporary storage for a router-owned source), and the bytes
    are copied first: a literal the model will re-open must stay true. A literal
    that names a file which does not exist yet (a crop the command is about to
    write) maps the same way, so new files never land in the router's storage.
    """

    candidate = Path(source_text)
    if not candidate.is_absolute():
        return None
    if candidate.suffix.lower() not in _CODEX_VIEW_IMAGE_EXTENSIONS:
        return None
    if _router_storage_relative_path(candidate) is None:
        return None
    try:
        exists = candidate.is_file()
    except OSError:
        exists = False
    if not exists:
        # A path a command is about to write: point the write at the directory
        # this file's own copy would use, so later crops stay with their source.
        return _future_replay_image_copy_path(candidate)
    try:
        raw = candidate.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    target = _replay_image_copy_root(candidate) / _replay_image_copy_name(
        raw, None, candidate.suffix.lower()
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    try:
        if not target.is_file() or target.read_bytes() != raw:
            tmp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}.{time.time_ns()}")
            with open(tmp_path, "wb") as handle:
                handle.write(raw)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, target)
    except OSError:
        return None
    return str(target)


def _with_neutral_attachment_paths(request_kwargs: dict) -> Optional[dict]:
    """Replay the model's own image paths from the neutral attachment directory.

    Advertising neutral paths is not enough while the model's own history still
    names the router's storage: the model reads those commands and notes back,
    re-opens the router's copies, and writes its derived crops next to them,
    which the user then sees in the transcript. Every router-owned image path
    token in a replayed tool call, tool output, or message is replaced by its
    neutral attachment copy (the bytes are copied first), so the whole
    read/crop/write workflow moves out of the router's storage without breaking
    a single path. Only the router's own image files are affected: a path that is
    not an existing image below the router's storage stays untouched.
    """

    if not _request_has_responses_shape(request_kwargs) or not _request_has_codex_client_evidence(
        request_kwargs
    ):
        return None
    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list):
        return None

    updated_items = list(input_items)
    rewritten_paths: set[str] = set()
    restored_paths = _restore_missing_replay_paths(input_items)

    def rewrite_path_tokens(text: str) -> str:
        def replace(match: "re.Match[str]") -> str:
            end = match.end()
            if end < len(text) and text[end] in "*?":
                return match.group(0)
            neutral = _neutral_attachment_copy(match.group(0))
            if neutral is None:
                return match.group(0)
            rewritten_paths.add(match.group(0))
            return neutral

        return _ROUTER_PATH_TOKEN.sub(replace, text)

    changed = False
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        updates: dict[str, Any] = {}
        if isinstance(item_type, str) and item_type in {"custom_tool_call", "function_call"}:
            for key in ("input", "arguments"):
                source = item.get(key)
                if not isinstance(source, str) or not source:
                    continue
                candidate = rewrite_path_tokens(source)
                if candidate != source:
                    updates[key] = candidate
        elif item_type in {"custom_tool_call_output", "function_call_output"}:
            output = item.get("output")
            if isinstance(output, str) and output:
                candidate = rewrite_path_tokens(output)
                if candidate != output:
                    updates["output"] = candidate
            elif isinstance(output, list):
                updated_parts = list(output)
                parts_changed = False
                for part_index, part in enumerate(output):
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if not isinstance(text, str) or not text:
                        continue
                    candidate = rewrite_path_tokens(text)
                    if candidate == text:
                        continue
                    updated_part = part.copy()
                    updated_part["text"] = candidate
                    updated_parts[part_index] = updated_part
                    parts_changed = True
                if parts_changed:
                    updates["output"] = updated_parts
        elif item_type == "message":
            content = item.get("content")
            if isinstance(content, str) and content:
                candidate = rewrite_path_tokens(content)
                if candidate != content:
                    updates["content"] = candidate
            elif isinstance(content, list):
                updated_parts = list(content)
                parts_changed = False
                for part_index, part in enumerate(content):
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if not isinstance(text, str) or not text:
                        continue
                    candidate = rewrite_path_tokens(text)
                    if candidate == text:
                        continue
                    updated_part = part.copy()
                    updated_part["text"] = candidate
                    updated_parts[part_index] = updated_part
                    parts_changed = True
                if parts_changed:
                    updates["content"] = updated_parts
        if not updates:
            continue
        updated_item = item.copy()
        updated_item.update(updates)
        updated_items[index] = updated_item
        changed = True
    if not changed and not restored_paths:
        return None
    if changed:
        modified_kwargs = request_kwargs.copy()
        modified_kwargs["input"] = updated_items
    else:
        modified_kwargs = request_kwargs
    _trace_module._route_trace(
        "router_path_literals_normalized",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        copy_root=str(_configured_replay_image_cache_root() or Path(tempfile.gettempdir())),
        path_count=len(rewritten_paths),
        restored_paths=restored_paths,
    )
    return modified_kwargs


def _trace_prefix_image_reference_paths(
    request_kwargs: Optional[dict],
    *,
    materialized: List[str],
    kept_inline: int,
) -> None:
    """Record that a replay path reference needed a durable copy, or could not
    be honored at all (the inline image stayed)."""

    from . import responses_execution as _responses_execution_module

    _trace_module._route_trace(
        "prefix_image_reference_paths",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        materialized=len(materialized),
        kept_inline=kept_inline,
        copy_root=str(_configured_replay_image_cache_root() or Path(tempfile.gettempdir())),
        names=sorted(Path(path).name for path in materialized)[:8],
    )


def _trace_prefix_image_preview_gate(
    request_kwargs: Optional[dict],
    gate: str,
    extra: Optional[dict] = None,
) -> None:
    """Record which guard stopped prefix-image previewing for one request."""

    from . import responses_execution as _responses_execution_module

    payload: dict[str, Any] = {
        "gate": gate,
    }
    if extra:
        payload.update(extra)
    _trace_module._route_trace(
        "prefix_image_preview_gate",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        **payload,
    )


def _item_pair_identifiers(item: Any) -> list[str]:
    """Return every call/output pairing identifier an item carries.

    The client's wire convention has varied over time: some replays carry
    ``call_id`` on both the call and its output, some carry only ``id`` on
    the call, and some carry both.  Collect every present identifier so the
    pairing passes below can match either side.
    """

    if not isinstance(item, dict):
        return []
    identifiers: list[str] = []
    for key in ("call_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value and value not in identifiers:
            identifiers.append(value)
    return identifiers


def _resolve_output_call_paths(
    input_items: list[Any],
    output_index: int,
    output_item: dict,
    call_paths: dict[str, list[str]],
    original_calls: set[str],
    call_index_by_id: dict[str, int],
    claimed_call_ids: set[str],
) -> tuple[Optional[list[str]], bool]:
    """Resolve the view-image call paired with one image-bearing output.

    Identifier-based pairing comes first (either shared ``call_id`` or shared
    ``id``).  When the replayed wire omits every shared identifier, fall back
    to adjacency: the immediately preceding item, when it is a view-image
    ``exec`` call that no other output has claimed through identifiers, is the
    only plausible pairing.  The fallback never steals a call already claimed
    by an identifier match.
    """

    for identifier in _item_pair_identifiers(output_item):
        if identifier in call_paths:
            claimed_call_ids.add(identifier)
            return call_paths[identifier], identifier in original_calls
    if output_index > 0:
        previous = input_items[output_index - 1]
        if isinstance(previous, dict) and previous.get("type") in {
            "custom_tool_call",
            "function_call",
        }:
            previous_identifiers = _item_pair_identifiers(previous)
            if any(
                identifier in call_paths and identifier not in claimed_call_ids
                for identifier in previous_identifiers
            ):
                identifier = next(
                    identifier
                    for identifier in previous_identifiers
                    if identifier in call_paths and identifier not in claimed_call_ids
                )
                claimed_call_ids.add(identifier)
                return call_paths[identifier], identifier in original_calls
    return None, False


def _history_tool_output_min_chars() -> int:
    """Tool-output size above which a replayed output is dropped."""

    value = os.getenv(_HISTORY_TOOL_OUTPUT_MIN_CHARS_ENV, "").strip()
    if not value:
        return _HISTORY_TOOL_OUTPUT_DEFAULT_MIN_CHARS
    try:
        parsed = int(value)
    except ValueError:
        return _HISTORY_TOOL_OUTPUT_DEFAULT_MIN_CHARS
    return max(0, parsed)


def _history_tool_output_kept_chars() -> int:
    """The fixed ceiling an operator configured, or the fallback when none did."""

    value = os.getenv(_HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV, "").strip()
    if not value:
        return _HISTORY_TOOL_OUTPUT_DEFAULT_KEPT_CHARS
    try:
        parsed = int(value)
    except ValueError:
        return _HISTORY_TOOL_OUTPUT_DEFAULT_KEPT_CHARS
    return max(0, parsed)


def _history_tool_output_ceiling_chars_for_window(
    *,
    context_window: int,
    max_context_window: Optional[int],
    effective_percent: int,
    max_output_tokens: Optional[int],
) -> int:
    """How many characters of replayed tool records a window can carry.

    The reserve is what the client asked to keep for its own answer, and it is
    never allowed to claim more than half the window: a client that names a
    ``max_output_tokens`` larger than the route can serve has not made the
    route's input smaller, and reading it that way left every such request with
    no ceiling at all.
    """

    window = max(0, int(context_window or 0))
    maximum = max(0, int(max_context_window or window))
    usable = min(window, maximum) if window and maximum else max(window, maximum)
    percent = min(100, max(1, int(effective_percent or 100)))
    headroom = usable * percent // 100
    reserve = max(0, int(max_output_tokens or 0))
    reserve = min(reserve, headroom // 2)
    return max(0, headroom - reserve) * _HISTORY_TOOL_OUTPUT_CHARS_PER_TOKEN


def _history_tool_output_latency_budget_chars() -> Optional[int]:
    """Replay the route can be sent before the wait outgrows the evidence.

    ``None`` means the window alone decides, ``0`` is the same answer stated
    explicitly, and a negative or unparsable value is ignored rather than
    silently emptying the replay.
    """

    value = os.getenv(_HISTORY_TOOL_OUTPUT_LATENCY_BUDGET_ENV, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


def _history_tool_output_ceiling_chars(request_kwargs: Optional[dict] = None) -> int:
    """The replayed tool-call tail this request may carry before it is trimmed.

    A route's own window is the only number that answers this: the same history
    is comfortable on a 1M-token model and refused by a 128k one, and a fixed
    character count picked for either turns into a memory budget for the other.
    Measured on this app's sessions, the fixed 20,000 in place before fired in
    eleven of thirteen live histories and dropped their *entire* set of records
    over the threshold, which is why the model re-read the same files for turn
    after turn.

    ``YOUNG_ROUTER_HISTORY_TOOL_OUTPUT_KEPT_CHARS`` still wins when it is set,
    and a request that names no route this app can size falls back to the
    constant.  ``YOUNG_ROUTER_HISTORY_TOOL_OUTPUT_LATENCY_BUDGET_CHARS`` narrows
    the result to what the route can be sent before the round trip stops being
    worth the evidence.
    """

    configured = os.getenv(_HISTORY_TOOL_OUTPUT_KEPT_CHARS_ENV, "").strip()
    if configured:
        return _history_tool_output_kept_chars()
    budget = _history_tool_output_latency_budget_chars()
    window_ceiling = _history_tool_output_window_ceiling_chars(request_kwargs)
    # The window says how much the route can hold; the budget says how much is
    # worth sending it.  Whichever is smaller is the ceiling, because a request
    # that fits but takes a minute to answer has cost the turn as much as one
    # that did not fit.  Only a positive budget narrows anything: ``None`` and
    # ``0`` leave the window alone.
    if budget:
        return min(window_ceiling, budget)
    return window_ceiling


def _history_tool_output_window_ceiling_chars(
    request_kwargs: Optional[dict] = None,
) -> int:
    """The ceiling the route's own window alone allows."""

    if not isinstance(request_kwargs, dict):
        return _HISTORY_TOOL_OUTPUT_DEFAULT_KEPT_CHARS
    model = request_kwargs.get("model")
    if not isinstance(model, str) or not model.strip():
        return _HISTORY_TOOL_OUTPUT_DEFAULT_KEPT_CHARS
    reserve = request_kwargs.get("max_output_tokens")
    try:
        from . import reasoning as _reasoning_module

        record = _reasoning_module._reasoning_registry().record_for(model.strip())
    except Exception:
        return _HISTORY_TOOL_OUTPUT_DEFAULT_KEPT_CHARS
    return _history_tool_output_ceiling_chars_for_window(
        context_window=record.context_window,
        max_context_window=record.max_context_window,
        effective_percent=record.effective_context_window_percent,
        max_output_tokens=reserve if isinstance(reserve, int) else None,
    )


def _history_tool_call_record_kind(item_type: Any) -> Optional[str]:
    """Return the lane a tool record belongs to, or ``None``.

    An exec call and the exec output that consumed it are one record to the
    upstream, so a replayed window that kept the call and dropped the output
    with it would hand the gateway exactly the incomplete tool record it
    rejects.  Both items of a lane are measured, kept and elided together.
    """

    if item_type in ("custom_tool_call", "custom_tool_call_output"):
        return "custom"
    if item_type in ("function_call", "function_call_output"):
        return "function"
    return None


def _tool_record_weight(item: Any) -> int:
    """Characters an item spends on the replayed tool-record budget."""

    if not isinstance(item, dict):
        return 0
    item_type = item.get("type")
    if item_type in ("custom_tool_call_output", "function_call_output"):
        return len(json.dumps(item.get("output"), ensure_ascii=False))
    if item_type == "function_call":
        return len(json.dumps(item.get("arguments"), ensure_ascii=False))
    if item_type == "custom_tool_call":
        script = item.get("input")
        return len(script) if isinstance(script, str) else 0
    return 0


def _elided_tool_call_note(size_chars: int) -> str:
    return (
        f"[{size_chars} characters elided: this tool output is outside the "
        "replayed history window. Run the tool again if its contents are "
        "needed.]"
    )


def _tool_call_lane_by_index(input_items: list) -> dict[int, str]:
    """Map every tool-record index to the lane it belongs to.

    A call and its output are one record to the upstream, and a request carries
    the same record in one of two lane shapes.  Both items of a lane are
    measured, judged and rewritten together, which is why the whole lane is what
    this pass counts rather than each output on its own.
    """

    lane_by_index: dict[int, str] = {}
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        lane = _history_tool_call_record_kind(item.get("type"))
        if lane is not None:
            lane_by_index[index] = lane
    return lane_by_index


def _frozen_tool_call_lane_ids(input_items: list) -> set[str]:
    """Lanes whose every item sits before the request's signed boundary.

    Signed content freezes the bytes before it: a lane that ends at or before
    the first signed item is re-sent by the client exactly as it first went out,
    so a record rewritten here would come back rewritten on the request after
    this one and could never be repaired.  A lane with any item past the
    boundary is mutable, because that is where this pass is allowed to write at
    all.
    """

    lane_by_index = _tool_call_lane_by_index(input_items)
    if not lane_by_index:
        return set()
    boundary: Optional[int] = None
    for index, item in enumerate(input_items):
        if isinstance(item, dict) and _value_has_encrypted_content(item):
            boundary = index
            break
    if boundary is None:
        return set()
    frozen: set[str] = set()
    for index, lane in lane_by_index.items():
        if index >= boundary:
            frozen.discard(lane)
            continue
        frozen.add(lane)
    return frozen


def _without_replayed_reasoning_summary_text(input_items: list) -> list:
    """Read a replayed ``reasoning`` item the way its upstream reads it.

    LiteLLM's pre-call context check turns every Responses input item into chat
    messages and counts them.  A ``reasoning`` item becomes a message whose
    ``content`` is its ``summary`` text, so the check charges the whole chain of
    thought the client replayed.  Neither side of a real request carries those
    words: Codex replays reasoning as an opaque id, and the upstream that
    produced them is the only party that can decode them.  On one measured
    thread the summaries were 545k of the 1.14M tokens the check charged for a
    body the same route had just answered at 400k -- 1,004,863 against a
    1,000,000 window, i.e. a refusal decided entirely by this reading.

    Only the summary text is dropped, and only for the count: the request
    itself is untouched.  A ``reasoning`` item's ``summary`` is the human
    rendering of thinking that the wire carries as an opaque signature or not
    at all -- the counter prices that prose as chat content, and no upstream
    ever receives it as such.  An item that carries visible ``content`` keeps
    it, because that is reasoning text a client can genuinely put on the wire.
    The direction this can err in is the safe one -- a body this pass still
    refuses really is larger than the window, and a body it accepts is then
    judged by the upstream's own accounting, which is what the client's window
    was sized for.
    """

    counted: list = []
    if not isinstance(input_items, list):
        return input_items
    for item in input_items:
        if (
            isinstance(item, dict)
            and item.get("type") == "reasoning"
            and item.get("content") in (None, "", [])
            and "summary" in item
        ):
            stripped = {key: value for key, value in item.items() if key != "summary"}
            counted.append(stripped)
            continue
        counted.append(item)
    return counted


def _with_elided_oversized_tool_outputs(request_kwargs: dict) -> Optional[dict]:
    """Bound the replayed tool-call window in the mutable replay suffix.

    A screenshot, a page snapshot or a file dump is consumed once and then
    replayed on every later turn.  Measured on one session, four image outputs
    and eleven snapshots held 72% of a 2.1M-character tool-output total that
    had grown the request past the gateway's ceiling while the client still
    reported 80% of its own window.  The item keeps its type, its identity and
    its position, so the model still sees that the tool ran and can ask for the
    content again; only the payload is dropped.

    What the window bounds is the replayed tool-call *tail*, in characters, per
    lane: a window counted in outputs kept the last eight outputs whatever they
    weighed, so a history of many medium outputs replayed whole on every turn
    until the upstream refused it -- a 141-output Codex thread kept eight of
    them, forty-odd records once both lanes of the request are counted.  The
    budget is the route's own usable input window
    (``_history_tool_output_ceiling_chars``), never a fixed character count:
    the same tail is comfortable on a 1M-token route and refused by a 128k one.

    Two rules keep this pass from becoming a memory budget, which is what a
    window below the drop threshold did when it emptied every read the model
    had made: the newest record of a lane is always kept whole, and a lane whose
    emptied records still leave it over the ceiling keeps them all, because that
    trim cannot make the request fit and only spends the evidence the model was
    working from.

    This pass runs after the image passes, and it may only rewrite what they
    left behind: an image output already replaced by previews and path
    references is small, and one the client re-sends from its own frozen
    transcript cannot be rewritten at all.
    """

    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list):
        return None
    min_chars = _history_tool_output_min_chars()
    if min_chars <= 0:
        return None
    kept_chars = _history_tool_output_ceiling_chars(request_kwargs)
    if kept_chars <= 0:
        return None
    lane_by_index = _tool_call_lane_by_index(input_items)
    if not lane_by_index:
        return None
    frozen_lanes = _frozen_tool_call_lane_ids(input_items)
    lane_weights: dict[str, int] = {}
    for index, lane in lane_by_index.items():
        lane_weights[lane] = lane_weights.get(lane, 0) + _tool_record_weight(
            input_items[index]
        )
    if not lane_weights:
        return None
    elided_by_index: dict[int, dict] = {}
    over_ceiling_lanes: list[str] = []
    for lane, weight in lane_weights.items():
        if weight <= kept_chars:
            continue
        if lane in frozen_lanes:
            continue
        lane_indices = sorted(
            index for index, value in lane_by_index.items() if value == lane
        )
        # The newest record is the one being answered, so it is kept whole even
        # when it alone exceeds the window: the window is a floor under what the
        # model works from, not a guarantee.  Everything older than it is
        # emptied once the lane leaves the window, record by record.
        newest_index = lane_indices[-1] if lane_indices else -1
        lane_elisions: dict[int, dict] = {}
        for index in reversed(lane_indices):
            item = input_items[index]
            if not isinstance(item, dict):
                continue
            if index == newest_index:
                continue
            if not item.get("type", "").endswith("_output"):
                continue
            item_weight = _tool_record_weight(item)
            if item_weight <= min_chars:
                continue
            updated = dict(item)
            updated["output"] = [
                {"type": "input_text", "text": _elided_tool_call_note(item_weight)}
            ]
            # Keep the existing whole-record floor for small records, but do
            # not preserve one old record that is itself larger than the
            # latency budget merely because removing it crosses the ceiling.
            if weight - item_weight <= kept_chars and item_weight <= kept_chars:
                break
            lane_elisions[index] = updated
            weight -= item_weight
            if weight <= kept_chars:
                break
        # A lane over the ceiling is trimmed as far as its own eligible records
        # take it, and the newest record always stays whole.  Reaching the
        # ceiling is not required for the trim to be worth it: one 150KB
        # snapshot dropped from a 240KB tail is the case this pass exists for,
        # even though the lane's small records still leave it over a small
        # ceiling.
        if weight > kept_chars:
            over_ceiling_lanes.append(lane)
        elided_by_index.update(lane_elisions)
    if not elided_by_index:
        return None
    # The call of an emptied record keeps its own arguments.  Rewriting them
    # into a placeholder that carried the note looked like the way to keep the
    # pair consistent, but a replayed call is the model's own few-shot example
    # of how to call that tool: one ``view_image`` whose arguments became
    # ``{"elided": "[N characters elided: ...]"}`` taught the model to emit
    # exactly that shape, and Codex's router answered every following turn with
    # ``failed to parse function arguments: missing field `path``` -- 233 times
    # in one thread, because the placeholder replays on every turn.  The output
    # note already tells the model the payload is gone; the call stays as it
    # was issued, so nothing in the replay teaches an invalid call.
    rewritten = [
        elided_by_index.get(index, item) for index, item in enumerate(input_items)
    ]
    elided_outputs = len(elided_by_index)
    elided_chars = sum(
        max(
            0,
            _tool_record_weight(input_items[index])
            - _tool_record_weight(updated),
        )
        for index, updated in elided_by_index.items()
    )
    result = dict(request_kwargs)
    result["input"] = rewritten
    _trace_module._route_trace(
        "responses_tool_output_elided",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        request=_trace_module._trace_request_summary(request_kwargs),
        ceiling_chars=kept_chars,
        over_ceiling_lanes=over_ceiling_lanes,
        elided_tool_outputs=elided_outputs,
        elided_tool_output_chars=elided_chars,
    )
    return result


def _history_image_elision_min_bytes() -> int:
    """Inline image bytes above which a replayed history image is dropped."""

    value = os.getenv(_HISTORY_IMAGE_ELISION_MIN_BYTES_ENV, "").strip()
    if not value:
        return _HISTORY_IMAGE_ELISION_DEFAULT_MIN_BYTES
    try:
        parsed = int(value)
    except ValueError:
        return _HISTORY_IMAGE_ELISION_DEFAULT_MIN_BYTES
    return max(0, parsed)


def _history_image_elision_keep_recent() -> int:
    """How many of the newest image outputs are always kept whole."""

    value = os.getenv(_HISTORY_IMAGE_ELISION_KEEP_ENV, "").strip()
    if not value:
        return _HISTORY_IMAGE_ELISION_DEFAULT_KEEP
    try:
        parsed = int(value)
    except ValueError:
        return _HISTORY_IMAGE_ELISION_DEFAULT_KEEP
    return max(0, parsed)


def _with_elided_oversized_history_images(
    request_kwargs: dict,
    input_items: list,
) -> Optional[dict]:
    """Drop oversized inline images from a history that cannot be reopened.

    Only reached when the history carries no signed prefix.  The newest image
    outputs are left alone -- the model is working from one of them -- and an
    image whose bytes are already small is not worth a rewrite.  Each dropped
    image becomes a text note naming its size, so the model can still see that
    an image was produced and can reproduce it with another tool call.
    """

    min_bytes = _history_image_elision_min_bytes()
    if min_bytes <= 0:
        return None
    # The newest image outputs are the ones the model may still be reasoning
    # from, so a fixed count of them is kept whole.  Everything else is judged
    # by weight: an oversized inline image in an unsigned history is replayed
    # in full on every turn, and the client discounts bytes the gateway counts.
    keep_recent = _history_image_elision_keep_recent()
    # An image whose producing call names a path can still be reopened, so the
    # path-reference pass below owns it and eliding it here would throw away a
    # recoverable original.  Only an image with no path behind it -- a ``js``
    # screenshot or any exec-shaped tool dump -- is beyond recovery.
    path_backed = _outputs_with_reopenable_paths(input_items)
    image_positions = [
        index
        for index, item in enumerate(input_items)
        if isinstance(item, dict)
        and item.get("type") in {"custom_tool_call_output", "function_call_output"}
        and index not in path_backed
        and isinstance(item.get("output"), list)
        and any(
            isinstance(part, dict) and part.get("type") == "input_image"
            for part in item["output"]
        )
    ]
    keep_positions = set(image_positions[max(0, len(image_positions) - keep_recent):])
    elided_bytes = 0
    elided_images = 0
    rewritten: list[Any] = []
    for index, item in enumerate(input_items):
        if (
            index not in keep_positions
            and index not in path_backed
            and isinstance(item, dict)
            and item.get("type") in {"custom_tool_call_output", "function_call_output"}
            and isinstance(item.get("output"), list)
            and any(
                isinstance(part, dict) and part.get("type") == "input_image"
                for part in item["output"]
            )
        ):
            new_parts: list[Any] = []
            elided_here = 0
            for part in item["output"]:
                if not isinstance(part, dict) or part.get("type") != "input_image":
                    new_parts.append(part)
                    continue
                url = part.get("image_url")
                if isinstance(url, str) and len(url) >= min_bytes:
                    elided_bytes += len(url)
                    elided_images += 1
                    elided_here += 1
                    new_parts.append(
                        {
                            "type": "input_text",
                            "text": (
                                f"[image elided: {len(url)} bytes of inline "
                                "image data from this tool output are no longer "
                                "current. Run the tool again if the image is "
                                "needed.]"
                            ),
                        }
                    )
                else:
                    new_parts.append(part)
            if elided_here:
                updated = dict(item)
                updated["output"] = new_parts
                rewritten.append(updated)
                continue
        rewritten.append(item)
    if not elided_images:
        return None
    result = dict(request_kwargs)
    result["input"] = rewritten
    _trace_prefix_image_preview_gate(request_kwargs, "history-images-elided")
    return result


def _outputs_with_reopenable_paths(input_items: list) -> set[int]:
    """Indices of image outputs whose producing call names a local path.

    Such an output is replayable by reference, so it belongs to the
    path-preview pass rather than to the elision above.
    """

    path_call_ids: set[str] = set()
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"custom_tool_call", "function_call"}:
            continue
        if not _codex_view_image_paths_from_call(item):
            continue
        path_call_ids.update(_item_pair_identifiers(item))
    if not path_call_ids:
        return set()
    indices: set[int] = set()
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"custom_tool_call_output", "function_call_output"}:
            continue
        if path_call_ids.intersection(_item_pair_identifiers(item)):
            indices.add(index)
    return indices


def _with_prefix_image_previews(request_kwargs: dict) -> Optional[dict]:
    """Serve signed-prefix ``view_image`` outputs as previews plus paths.

    A ``view_image`` tool output keeps its original inline bytes in the
    client's local history, and once a later response item carries
    ``encrypted_content`` the frozen-prefix policy forwards those bytes
    verbatim forever -- so every request re-uploads multi-megabyte originals
    even though the model already reasoned over the compressed preview on the
    output's first replay.  The upstream demonstrably accepts sibling-byte
    variation across requests (the existing entry window already alternates
    compressed and original bytes for the same item), so for oversized paired
    images in the frozen prefix this pass swaps in the same deterministic
    preview the entry window produced and prepends the matching local paths.
    The model keeps its visual memory at preview fidelity and can re-open any
    original at full resolution with one more ``view_image`` call; outputs the
    call explicitly requested at original resolution are left untouched.  When
    a one-to-one path-to-image pairing cannot be established (an exec output
    carries more images than the call's path literals), the images are still
    previewed but no path-reference text is added; the paths remain visible in
    the matching call item's script text.

    A path reference is a promise that the original can be re-opened, so a
    candidate whose local file is gone (or only lives in OS temporary storage
    the client will clean up) is backed by a durable copy in the replay-image
    cache instead of the client's path.  When even that copy cannot be written
    the inline image is kept: replay history must never trade the model's only
    view of an image for a path it cannot open.

    The replay never carries a multi-megabyte original: a crop re-opened at
    ``detail: original`` is routinely 2-12 MB, so a task that keeps several of
    them in its frozen prefix makes every turn larger than the upstream gateway
    accepts instead of just larger than the model needs.  An explicit
    original-resolution output is therefore replayed as the same deterministic
    preview as every other oversized history image, and only the matching local
    path (always part of the same preview reference text) is left to reach the
    original bytes.

    ``YOUNG_ROUTER_PREFIX_IMAGE_MODE`` selects the treatment: ``preview``
    keeps previews for every oversized paired output, ``path-recent`` keeps
    previews only for the most recent image outputs (count from
    ``YOUNG_ROUTER_PREFIX_IMAGE_RECENT_COUNT``, default 6) and replaces older
    ones with pure path references, and ``off`` disables the pass entirely.

    A reference only ever names a path the client's own tool call wrote.  This
    proxy does not know whether it can see that filesystem -- a desktop proxy
    shares it with the client, a remote gateway does not -- and must not guess:
    substituting a copy it materialized would name a path that a remote client's
    ``view_image`` cannot open, while declaring the client's path dead because
    this host cannot see it would delete the one path that works.  The model
    still keeps a readable preview either way and can re-open the original with
    one more ``view_image`` call.
    """

    if not _prefix_image_preview_enabled():
        _trace_prefix_image_preview_gate(request_kwargs, "disabled")
        return None
    mode = _prefix_image_mode()
    if mode == "off":
        _trace_prefix_image_preview_gate(request_kwargs, "mode-off")
        return None
    if _request_has_structured_codex_compaction(request_kwargs):
        # A compaction request must replay its signed history byte-exact:
        # the upstream compacts exactly what it receives, and a preview or
        # path swap here would permanently bake the reduced image into the
        # compacted history the client keeps.
        _trace_prefix_image_preview_gate(request_kwargs, "compaction-request")
        return None
    if not _request_has_responses_shape(request_kwargs):
        _trace_prefix_image_preview_gate(request_kwargs, "not-responses-shape")
        return None
    if not _request_has_codex_client_evidence(request_kwargs):
        _trace_prefix_image_preview_gate(request_kwargs, "no-codex-evidence")
        return None
    input_items = request_kwargs.get("input")
    if not isinstance(input_items, list):
        _trace_prefix_image_preview_gate(request_kwargs, "no-input-list")
        return None
    last_encrypted_index = max(
        (
            index
            for index, item in enumerate(input_items)
            if _value_has_encrypted_content(item)
        ),
        default=-1,
    )
    elision_result: Optional[dict] = None
    if last_encrypted_index < 0:
        # Without a signed prefix nothing must stay byte-exact, and the history
        # is still replayed in full on every turn.  A conversation that carried
        # a few screenshots then grows past the upstream gateway's limit while
        # the client's own window accounting still shows headroom, because
        # base64 image bytes are what the gateway counts and what the client
        # discounts.
        #
        # An image whose producing call names a local path is bounded below by
        # the same preview-plus-reference treatment the signed prefix gets: the
        # model keeps a readable preview and a path it can re-open, which costs
        # the gateway a fraction of the original bytes.  Only an image with no
        # path behind it -- a ``js`` screenshot or any exec-shaped tool dump --
        # is beyond recovery, and those are dropped for a note stating what
        # they were.
        #
        # The note is a last resort, never the treatment for a path-backed
        # image: telling the model an image it still needs "is no longer
        # current, run the tool again" invites exactly the re-capture loop this
        # pass exists to prevent, because a fresh capture is elided on the next
        # turn in the same way.
        last_encrypted_index = len(input_items) - 1
        elided = _with_elided_oversized_history_images(request_kwargs, input_items)
        if elided is not None:
            elision_result = elided
            request_kwargs = elided
            input_items = request_kwargs["input"]
    min_bytes = _prefix_image_preview_min_bytes()
    call_paths: dict[str, list[str]] = {}
    original_calls: set[str] = set()
    call_index_by_id: dict[str, int] = {}
    image_output_items = 0
    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"custom_tool_call", "function_call"}:
            paths = _codex_view_image_paths_from_call(item)
            if not paths:
                continue
            for identifier in _item_pair_identifiers(item):
                # Replayed wire history can carry only one of ``call_id`` /
                # ``id`` on the call while the paired output carries the
                # other; index every identifier so outputs match regardless
                # of the wire convention.
                call_paths.setdefault(identifier, paths)
                call_index_by_id[identifier] = index
                if _codex_view_image_call_requests_original(item):
                    original_calls.add(identifier)
            continue
        if item.get("type") in {"custom_tool_call_output", "function_call_output"}:
            output = item.get("output")
            if isinstance(output, list) and _codex_view_image_output_parts(output):
                image_output_items += 1
    if not call_paths and image_output_items:
        # No view_image call survived with a path.  The image outputs are still
        # bounded in place below (a reduced preview, no reference): the bytes
        # are what the gateway counts and the client discounts, and dropping
        # them entirely would delete the model's only view of the image.  The
        # record exists because a history whose calls lost their path is what
        # made the previous pass skip every output.
        from collections import Counter

        type_counts: "Counter[str]" = Counter()
        view_image_types: "Counter[str]" = Counter()
        wire_shapes: dict[str, Any] = {}
        arguments_sample: Optional[str] = None
        for item in input_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if not isinstance(item_type, str):
                item_type = type(item).__name__
            type_counts[item_type] += 1
            if "view_image" in json.dumps(item, default=str):
                view_image_types[item_type] += 1
                if item_type not in wire_shapes:
                    wire_shapes[item_type] = {
                        key: (
                            type(value).__name__
                            if not isinstance(value, str)
                            else f"str[{len(value)}]"
                        )
                        for key, value in item.items()
                    }
                if item_type == "function_call" and arguments_sample is None:
                    for key in ("arguments", "input"):
                        raw = item.get(key)
                        if isinstance(raw, str):
                            import re as _re

                            arguments_sample = _re.sub(
                                r"/[^\"'\\s,;)]{4,}",
                                "/<path>",
                                raw,
                            )[:180]
                            break
        _trace_prefix_image_preview_gate(
            request_kwargs,
            "no-view-image-calls",
            extra={
                "input_items": len(input_items),
                "item_type_counts": dict(type_counts.most_common(10)),
                "view_image_item_types": dict(view_image_types.most_common(6)),
                "wire_shapes": wire_shapes,
                "arguments_sample": arguments_sample,
            },
        )

    recent_count = _prefix_image_recent_count() if mode == "path-recent" else 0
    candidate_indices: list[int] = []
    candidate_paths: dict[int, list[str]] = {}
    candidate_is_original: dict[int, bool] = {}
    claimed_call_ids: set[str] = set()
    skipped_referenced = 0
    skipped_original = 0
    skipped_not_oversized = 0
    skipped_unpaired = 0
    for index in range(last_encrypted_index + 1):
        item = input_items[index]
        if not (
            isinstance(item, dict)
            and item.get("type") in {"custom_tool_call_output", "function_call_output"}
        ):
            continue
        output = item.get("output")
        if not isinstance(output, list):
            continue
        if _image_inputs_module._output_has_codex_view_image_references(
            output
        ) or _image_inputs_module._output_has_codex_view_image_original_references(output):
            skipped_referenced += 1
            continue
        image_parts = _codex_view_image_output_parts(output)
        if not image_parts:
            continue
        oversized = [
            part
            for part in image_parts
            if _image_inputs_module._image_data_url_size(part["image_url"]) > min_bytes
        ]
        if not oversized:
            skipped_not_oversized += 1
            continue
        paths, is_original = _resolve_output_call_paths(
            input_items,
            index,
            item,
            call_paths,
            original_calls,
            call_index_by_id,
            claimed_call_ids,
        )
        if not paths:
            # No path was named by the producing call, so none may be invented:
            # bound the bytes in place and claim no reference at all.
            paths = []
            is_original = False
        if is_original and not _prefix_image_original_path_enabled():
            skipped_original += 1
            continue
        candidate_indices.append(index)
        candidate_paths[index] = paths
        candidate_is_original[index] = is_original

    # The most recent image outputs keep their preview treatment so the model
    # retains visual continuity near the current turn; older ones fall back to
    # pure path references (path-recent mode) and can still be re-opened at
    # full resolution with one view_image call.  A zero recent count means
    # every aged output converts, so it never resolves to the whole list.
    recent_candidates = (
        set(candidate_indices[-recent_count:]) if recent_count > 0 else set()
    )
    updated_items = list(input_items)
    changed = False
    kept_inline_candidates = 0
    for index in candidate_indices:
        item = input_items[index]
        output = item["output"]
        image_parts = _codex_view_image_output_parts(output)
        paired = len(candidate_paths[index]) == len(image_parts)
        # A path reference is only ever the one the producing call wrote.  This
        # proxy does not substitute a copy of its own: on a remote gateway that
        # copy names a file the client's view_image cannot open.
        reference_paths: List[str] = (
            list(candidate_paths[index]) if paired else []
        )
        reopenable = paired
        if not reopenable:
            kept_inline_candidates += 1
        references = "\n".join(
            f"{number}. {path}"
            for number, path in enumerate(reference_paths, start=1)
        )
        if mode == "path-recent" and index not in recent_candidates and reopenable:
            reference_part = {
                "type": "input_text",
                "text": (
                    f"{_CODEX_VIEW_IMAGE_REFERENCE_MARKER}\n"
                    "Historical inline images below were replaced by local-path "
                    "references to keep the replay small. To view an original, "
                    "call view_image on its local path:\n"
                    f"{references}"
                ),
            }
            kept_parts = [
                part
                for part in output
                if not (
                    isinstance(part, dict) and part.get("type") == "input_image"
                )
            ]
            updated_item = item.copy()
            updated_item["output"] = [reference_part, *kept_parts]
            updated_items[index] = updated_item
            changed = True
            continue

        oversized = [
            part
            for part in image_parts
            if _image_inputs_module._image_data_url_size(part["image_url"]) > min_bytes
        ]
        new_parts: list[dict] = []
        resized_any = False
        for part in output:
            if (
                isinstance(part, dict)
                and part in oversized
                and isinstance(part.get("image_url"), str)
            ):
                resized = _prefix_image_preview_resized(part["image_url"])
                if resized != part["image_url"]:
                    updated_part = dict(part)
                    updated_part["image_url"] = resized
                    new_parts.append(updated_part)
                    resized_any = True
                    continue
            new_parts.append(part)
        if not resized_any:
            continue
        updated_item = item.copy()
        if paired and reopenable:
            if candidate_is_original.get(index):
                # A re-open explicitly asked for the original resolution, so
                # the preview must keep that promise and name the matching
                # path the same way the mutable-suffix pass does.
                reference_text = (
                    f"{_CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER}\n"
                    "Original-resolution image requested for this explicit re-open; "
                    "the inline image below carries the highest resolution this "
                    "route forwards. For the original file, call view_image on the "
                    "matching local path:\n"
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
            updated_item["output"] = [reference_part, *new_parts]
        else:
            # The call exposes no reopenable one-to-one path mapping, so shrink
            # the bytes without claiming a correspondence; the paths stay
            # visible in the matching call item's script text.
            updated_item["output"] = new_parts
        updated_items[index] = updated_item
        changed = True

    configured_root = _configured_replay_image_cache_root()
    if kept_inline_candidates:
        _trace_prefix_image_reference_paths(
            request_kwargs,
            materialized=[],
            kept_inline=kept_inline_candidates,
        )

    if not changed:
        _trace_prefix_image_preview_gate(
            request_kwargs,
            "nothing-oversized-or-unpaired-usable",
            extra={
                "image_output_items": image_output_items,
                "view_image_call_items": len(call_index_by_id),
                "candidates": len(candidate_indices),
                "skipped_referenced": skipped_referenced,
                "skipped_original": skipped_original,
                "skipped_not_oversized": skipped_not_oversized,
                "skipped_unpaired": skipped_unpaired,
            },
        )
        # An unsigned history with no path-backed image may still have had a
        # path-less one dropped above; that rewrite is the whole result.
        return elision_result
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


def _codex_unwrap_function_arguments(value: str) -> str:
    text = value.strip()
    if not text.startswith("```") or not text.endswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) < 2 or lines[-1].strip() != "```":
        return text
    body = lines[1:-1]
    if body and body[0].strip().lower() in {"json", "javascript", "js"}:
        body = body[1:]
    return "\n".join(body).strip()


def _codex_remove_trailing_commas(value: str) -> str:
    output: list[str] = []
    quote: Optional[str] = None
    escaped = False
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {"\"", "'"}:
            quote = character
            output.append(character)
            index += 1
            continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(value) and value[lookahead].isspace():
                lookahead += 1
            if lookahead < len(value) and value[lookahead] in {"]", "}"}:
                output.extend(value[index + 1 : lookahead])
                index = lookahead
                continue
        output.append(character)
        index += 1
    return "".join(output)


def _codex_quote_unquoted_function_keys(value: str) -> str:
    output: list[str] = []
    containers: list[str] = []
    quote: Optional[str] = None
    escaped = False
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {"\"", "'"}:
            quote = character
            output.append(character)
            index += 1
            continue
        if character == "{":
            containers.append("object")
            output.append(character)
            index += 1
            continue
        if character == "[":
            containers.append("array")
            output.append(character)
            index += 1
            continue
        if character in {"}", "]"}:
            if containers:
                containers.pop()
            output.append(character)
            index += 1
            continue
        if containers and containers[-1] == "object":
            previous = "".join(output).rstrip()[-1:] or ""
            if previous in {"{", ","}:
                match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", value[index:])
                if match is not None:
                    end = index + len(match.group(0))
                    lookahead = end
                    while lookahead < len(value) and value[lookahead].isspace():
                        lookahead += 1
                    if lookahead < len(value) and value[lookahead] == ":":
                        output.append(json.dumps(match.group(0), ensure_ascii=False))
                        index = end
                        continue
        output.append(character)
        index += 1
    return "".join(output)


def _codex_function_arguments_object(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, str):
        return None
    text = _codex_unwrap_function_arguments(value)
    if not text:
        return {}

    candidates: list[str] = []

    def add_candidate(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    add_candidate(text)
    without_placeholders = text
    while without_placeholders.startswith("{}"):
        without_placeholders = without_placeholders[2:].lstrip()
        add_candidate(without_placeholders)

    for candidate in candidates:
        transformed = _codex_quote_unquoted_function_keys(candidate)
        variants = (
            candidate,
            _codex_remove_trailing_commas(candidate),
            transformed,
            _codex_remove_trailing_commas(transformed),
        )
        import ast

        for variant in variants:
            try:
                parsed = json.loads(variant)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return parsed
            try:
                parsed = ast.literal_eval(variant)
            except (SyntaxError, TypeError, ValueError, MemoryError, RecursionError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _codex_normalized_function_arguments(
    value: Any,
    *,
    empty_is_object: bool = False,
) -> tuple[Optional[str], bool]:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")), True
    if not isinstance(value, str):
        if empty_is_object and value is None:
            return "{}", True
        return None, False
    if not value.strip():
        return ("{}", True) if empty_is_object else (None, False)
    parsed = _codex_function_arguments_object(value)
    if parsed is None:
        return None, False
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), True


def _codex_open_container_closers(text: str) -> Optional[str]:
    """The brackets needed to close ``text``, or ``None`` when it cannot be.

    Walks the fragment the way a JSON reader does — string literals and their
    escapes hide every structural character — and reports the containers still
    open at the end.  ``None`` means the cut landed where nothing may be
    appended: inside a string literal, or on a separator (``,``/``:``) whose
    value never arrived.  Closing *those* would mean writing a value the model
    did not, so they are refused rather than completed.
    """

    stack: list[str] = []
    quote: Optional[str] = None
    escaped = False
    for character in text:
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character == '"':
            quote = character
            continue
        if character in "{[":
            stack.append("}" if character == "{" else "]")
            continue
        if character in "}]":
            if stack and stack[-1] == character:
                stack.pop()
            continue
    if quote is not None or not stack:
        return None
    # A fragment that stops on a separator is missing a whole member, and one
    # that stops on an opening bracket is missing a whole value.
    if text.rstrip().endswith((",", ":", "{", "[")):
        return None
    return "".join(reversed(stack))


def _codex_closed_function_arguments(value: Any) -> Optional[dict[str, Any]]:
    """Read a JSON object an output limit cut short by closing its brackets.

    An upstream that runs out of output tokens mid-tool-call streams a prefix
    of a JSON object: every byte of it is exactly what the model wrote, and
    only the closing brackets never arrived.  That is an *incomplete*
    document, not a malformed one, so it is closed here rather than discarded
    — the arguments that did arrive are real, and a call whose arguments are
    unusable ends a turn the model was still in the middle of.

    Nothing is invented: the fragment must not end inside a string or on a
    separator, so only containers that were genuinely opened get closed.  A
    truncated value is therefore never completed with a guessed tail (a
    shortened ``rm -rf`` path is not the command that was meant).
    """

    if not isinstance(value, str):
        return None
    text = _codex_unwrap_function_arguments(value)
    if not text.startswith("{"):
        return None
    closers = _codex_open_container_closers(text)
    if not closers:
        return None
    try:
        parsed = json.loads(text + closers)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _codex_repaired_function_arguments(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized, valid = _codex_normalized_function_arguments(value)
    if not valid or normalized is None:
        return None
    try:
        original = json.loads(value)
    except (TypeError, ValueError):
        original = None
    if isinstance(original, dict):
        return None
    return normalized


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


_MCP_AUTO_APPROVE_ENV = "YOUNG_ROUTER_MCP_AUTO_APPROVE"
_CODEX_DESCENDANT_CLEANUP_METADATA_KEY = "codex_descendant_cleanup"
_CODEX_DESCENDANT_LIFECYCLE_TOOLS = {
    "followup_task",
    "interrupt_agent",
    "spawn_agent",
}


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


_CODEX_ROOT_AGENT_MARKER = "You are `/root`, the primary agent"
_CODEX_SUBAGENT_PATH_MARKER = "You are `/root/"
_CODEX_GENERIC_AGENT_MARKER = (
    "You are an agent in a team of agents collaborating to complete a task."
)


def _codex_agent_identity_in_text(text: str, is_root: Optional[bool]) -> Optional[bool]:
    """Return the agent identity a block of Codex instructions declares.

    The identity block is delivered either as a leading ``developer`` input
    message or folded into the request's ``instructions`` string, depending on
    the client's instruction layout.  Both shapes carry the same marker text,
    so they are classified the same way: the last marker in a block wins, and
    ``None`` means the block says nothing about the agent's identity.
    """
    if not text:
        return is_root
    markers = (
        (text.rfind(_CODEX_ROOT_AGENT_MARKER), True),
        (text.rfind(_CODEX_SUBAGENT_PATH_MARKER), False),
        (text.rfind(_CODEX_GENERIC_AGENT_MARKER), False),
    )
    position, marker_is_root = max(markers, key=lambda marker: marker[0])
    if position < 0:
        return is_root
    return marker_is_root


def _codex_request_is_root_agent(request_kwargs: dict) -> bool:
    input_value = request_kwargs.get("input")
    if not isinstance(input_value, list):
        return False
    is_root: Optional[bool] = None
    for item in input_value:
        if (
            not isinstance(item, dict)
            or str(item.get("role") or "").lower() not in {"developer", "system"}
        ):
            continue
        is_root = _codex_agent_identity_in_text(_codex_message_text(item), is_root)
    if is_root is None:
        # A resumed or continuation window replays the conversation tail without
        # the leading identity message, but the client folds the same role block
        # into ``instructions`` instead.  Only consulted when the input itself
        # carries no identity, so the composed instruction string can never
        # override a role block the request already declared.
        instructions = request_kwargs.get("instructions")
        if isinstance(instructions, str):
            is_root = _codex_agent_identity_in_text(instructions, None)
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

    Only a replay that already carries a descendant-management call can hold a
    live descendant.  A root whose request merely declares the collaboration
    namespace has never spawned anything, so its snapshot is not missing: there
    is nothing to enumerate, and forcing a tool call here spends the whole
    answer on an inventory of an empty tree.
    """
    if not _codex_request_is_root_agent(request_kwargs):
        return None
    input_value = request_kwargs.get("input")
    if not isinstance(input_value, list):
        return None
    if not _codex_descendant_cleanup_has_history(request_kwargs):
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
        if isinstance(item_type, str) and item_type in {"function_call", "custom_tool_call"}:
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


def _codex_replay_has_tool_free_progress_message(request_kwargs: dict) -> bool:
    """Whether the replay already contains a progress message with no tool call.

    Such a message was recorded as a completed turn outcome, so a client that
    sends one mid-turn has already ended a turn it meant to continue.  The
    next request is the model being asked to pick the work back up, and a
    second tool-free response would end that one too -- which is how a long
    investigation stops partway with its own progress sentence as the answer.
    """

    input_value = request_kwargs.get("input")
    if not isinstance(input_value, list):
        return False
    saw_assistant_text = False
    for item in input_value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"function_call", "custom_tool_call"}:
            saw_assistant_text = False
            continue
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            continue
        if str(item.get("role") or "").lower() != "assistant":
            continue
        saw_assistant_text = bool(_codex_message_text(item).strip())
    return saw_assistant_text


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
        append(direct_names, name)
        qualified = f"{namespace}.{name}" if namespace else name
        append(qualified_names, qualified)

    for tool in _codex_declared_tools(request_kwargs):
        visit(tool)
    return direct_names, qualified_names


# The surface-level request adaptations shared by every Codex-shaped request.
# The web-search bridge instructions are added ahead of these by the surfaces
# that need them, and ``young_router.proxy.codex_contract`` prepends the Codex
# contract steps (tool registry, descendant barrier, progress-message rule).
_CODEX_REQUEST_INSTRUCTION_STEPS = (
    _with_empty_tool_controls_removed,
    _with_codex_compaction_controls,
    _with_responses_native_extra_body,
    _with_codex_compaction_headers,
)


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


def _with_streaming_plaintext_headers(request_kwargs: dict) -> Optional[dict]:
    """Ask the upstream not to compress a streamed answer.

    A relay that gzips ``text/event-stream`` defeats incremental delivery: the
    whole body arrives decodable only when the last block lands, so a reasoning
    summary produced over twenty seconds reaches the client as one burst after
    the model stops thinking.  Compression buys nothing on a stream we forward
    immediately, so streamed requests are negotiated as plaintext.
    """

    if request_kwargs.get("stream") is not True:
        return None
    existing_headers = request_kwargs.get("extra_headers")
    merged_headers: Dict[str, str] = (
        existing_headers.copy() if isinstance(existing_headers, dict) else {}
    )
    encoded = False
    for header_name, value in (
        ("Accept-Encoding", "identity"),
        ("Accept", "text/event-stream"),
    ):
        existing_key = _header_key(merged_headers, header_name)
        if existing_key is None:
            merged_headers[header_name] = value
            encoded = True
        elif merged_headers[existing_key] != value:
            merged_headers[existing_key] = value
            encoded = True
    if not encoded:
        return None
    modified_kwargs = request_kwargs.copy()
    modified_kwargs["extra_headers"] = merged_headers
    return modified_kwargs


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
