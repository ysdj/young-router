from __future__ import annotations

from typing import Any, Optional

from ..values import positive_int


def _request_metadata_dict(
    request_kwargs: Optional[dict],
    key: str,
) -> Optional[dict]:
    request_kwargs = request_kwargs or {}
    value = request_kwargs.get(key)
    return value if isinstance(value, dict) else None


def _request_metadata_flag(
    request_kwargs: Optional[dict],
    key: str,
) -> bool:
    """Whether any shape this request carries states ``key`` as true.

    A request reaches the proxy in more than one shape: the body the client
    sent, the deployment call LiteLLM built from it, and the copies handed to
    hooks and logging callbacks.  ``litellm_metadata`` can be top-level or
    nested under ``litellm_params``, and a flag that only one reader looks for
    is a flag the other shapes silently lose.  This reads all of them, so a
    marker this proxy set on a payload survives the router rebuilding its
    kwargs.
    """

    request_kwargs = request_kwargs or {}
    holders = [request_kwargs]
    litellm_params = request_kwargs.get("litellm_params")
    if isinstance(litellm_params, dict):
        holders.append(litellm_params)
    for holder in holders:
        for metadata_key in ("litellm_metadata", "metadata"):
            metadata = holder.get(metadata_key)
            if isinstance(metadata, dict) and metadata.get(key) is True:
                return True
    return False


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
    return positive_int(value)
