from __future__ import annotations

import os
from typing import Any, Optional

from . import responses_output as _responses_output_module
from . import request_context as _request_context_module

from .base import (
    _IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY,
    _IMAGE_GENERATION_TOOL_FALLBACK_DEFAULT_MAX_ATTEMPTS,
    _IMAGE_GENERATION_TOOL_FALLBACK_MAX_ATTEMPTS_ENV,
)


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
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
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
    litellm_metadata = (
        _request_context_module._request_metadata_dict(
            request_kwargs, "litellm_metadata"
        )
        or {}
    )
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


def _deployment_supports_responses_image_generation_tool(deployment: Any) -> bool:
    if not isinstance(deployment, dict):
        return False
    model_info = deployment.get("model_info")
    if not isinstance(model_info, dict):
        return False
    return model_info.get("supports_responses_image_generation_tool") is True

def _response_has_image_generation_result(response: Any) -> bool:
    return "image_generation_call" in _responses_output_module._response_types(response)


def _response_has_image_generation_activity(response: Any) -> bool:
    return any(
        "image_generation_call" in item_type
        for item_type in _responses_output_module._response_types(response)
    )


def _response_is_image_generation_unavailable_refusal(response: Any) -> bool:
    if _response_has_image_generation_result(response):
        return False
    text = _responses_output_module._response_text(response).lower()
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
    return (
        _responses_output_module._response_is_effectively_empty(response)
        or _response_is_image_generation_unavailable_refusal(response)
    )
