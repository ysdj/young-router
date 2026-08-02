from __future__ import annotations

from typing import Any, Optional


def _request_metadata_dict(
    request_kwargs: Optional[dict],
    key: str,
) -> Optional[dict]:
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


def _request_model_group(request_kwargs: Optional[dict]) -> str:
    request_kwargs = request_kwargs or {}
    model = request_kwargs.get("model")
    return model if isinstance(model, str) else ""


def _request_model_for_error(request_kwargs: Optional[dict]) -> str:
    return _request_model_group(request_kwargs)


def _positive_int_value(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
