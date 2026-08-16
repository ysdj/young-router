from __future__ import annotations

import json
from typing import Any, Optional

from . import responses_output as _responses_output_module
from . import request_context as _request_context_module

from .base import (
    _IMAGE_GENERATION_TOOL_CAPABILITY_UNSUPPORTED_ATTR,
    _IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY,
)


def _request_forces_image_generation_tool(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    tool_choice = request_kwargs.get("tool_choice")
    if tool_choice == "image_generation":
        return True
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "image_generation":
        return True
    return False


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


def _image_generation_tool_runtime_fallback_exception(
    *,
    capability_unsupported: bool = False,
) -> Exception:
    exception = RuntimeError("image_generation runtime fallback")
    try:
        exception.image_generation_tool_runtime_fallback = True  # type: ignore[attr-defined]
        setattr(
            exception,
            _IMAGE_GENERATION_TOOL_CAPABILITY_UNSUPPORTED_ATTR,
            capability_unsupported,
        )
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
    if isinstance(response, (str, bytes)):
        try:
            text = response.decode("utf-8") if isinstance(response, bytes) else response
        except UnicodeDecodeError:
            return False
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload_text = line.split(":", 1)[1].strip()
            if payload_text == "[DONE]":
                continue
            try:
                payload = json.loads(payload_text)
            except (TypeError, ValueError):
                continue
            if _response_has_image_generation_result(payload):
                return True
        return False
    return "image_generation_call" in _responses_output_module._response_types(response)


def _image_generation_result_is_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (bytes, bytearray)):
        return bool(value)
    return value is not None


def _response_has_image_generation_result_payload(response: Any) -> bool:
    def walk(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, list):
            return any(walk(item) for item in value)
        if isinstance(value, dict):
            if (
                value.get("type") == "image_generation_call"
                and _image_generation_result_is_present(value.get("result"))
            ):
                return True
            return any(walk(item) for item in value.values())
        if hasattr(value, "model_dump"):
            try:
                return walk(value.model_dump())
            except Exception:
                return False
        return False

    return walk(response)


def _normalize_image_generation_result_status(value: Any) -> None:
    """Mark image output items complete once their result payload is present."""

    if isinstance(value, list):
        for item in value:
            _normalize_image_generation_result_status(item)
        return
    if not isinstance(value, dict):
        return
    if (
        value.get("type") == "image_generation_call"
        and _image_generation_result_is_present(value.get("result"))
        and str(value.get("status") or "").lower() in {"generating", "in_progress"}
    ):
        value["status"] = "completed"
    for item in value.values():
        _normalize_image_generation_result_status(item)


def _response_has_image_generation_activity(response: Any) -> bool:
    if isinstance(response, (str, bytes)):
        try:
            text = response.decode("utf-8") if isinstance(response, bytes) else response
        except UnicodeDecodeError:
            return False
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload_text = line.split(":", 1)[1].strip()
            if payload_text == "[DONE]":
                continue
            try:
                payload = json.loads(payload_text)
            except (TypeError, ValueError):
                continue
            if _response_has_image_generation_activity(payload):
                return True
        return False
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


def _response_is_image_generation_policy_refusal(response: Any) -> bool:
    if _response_has_image_generation_result(response):
        return False
    text = _responses_output_module._response_text(response).lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "content_policy",
            "content policy",
            "content-policy",
            "contentpolicy",
            "policy_violation",
            "policy violation",
            "safety policy",
            "safety violation",
            "violates our policy",
            "violates the policy",
            "blocked by safety",
            "blocked due to safety",
            "unsafe prompt",
            "unsafe content",
            "disallowed content",
            "disallowed prompt",
            "high risk",
            "high-risk request",
            "违反政策",
            "安全策略",
            "不符合政策",
        )
    )


def _response_should_trigger_image_generation_fallback(response: Any) -> bool:
    return not _response_has_image_generation_result(response)
