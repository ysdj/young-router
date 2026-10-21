"""Expose DeepSeek's extended reasoning on the Chat response bridge."""

from __future__ import annotations

from typing import Any, Mapping


def is_deepseek_request(request_data: Mapping[str, Any]) -> bool:
    """Whether this bridge request targets a DeepSeek model."""

    params = request_data.get("litellm_params")
    params = params if isinstance(params, Mapping) else {}
    model = str(
        request_data.get("model")
        or params.get("model")
        or ""
    ).lower()
    metadata = request_data.get("litellm_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    model = model or str(
        metadata.get("model_group")
        or metadata.get("model")
        or ""
    ).lower()
    return "deepseek" in model


def raw_reasoning_requested(request_data: Mapping[str, Any]) -> bool:
    """Whether the client explicitly asked to receive raw DeepSeek reasoning."""

    if not is_deepseek_request(request_data):
        return False
    reasoning = request_data.get("reasoning")
    if isinstance(reasoning, Mapping):
        summary = reasoning.get("summary")
    else:
        summary = request_data.get("model_reasoning_summary")
    return summary == "raw"


__all__ = ["is_deepseek_request", "raw_reasoning_requested"]
