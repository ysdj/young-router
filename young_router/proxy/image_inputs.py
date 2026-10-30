from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from . import responses_output as _responses_output_module

from .base import (
    _OMIT_RESPONSE_VALUE,
    _RESPONSES_IMAGE_INPUT_SUPPORT_KEY,
    _value_has_encrypted_content,
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
    if isinstance(item_type, str):
        return item_type in {"input_image", "image_url"}
    if item_type is None:
        return set(value).issubset(
            {"image_url", "url", "file_id", "detail"}
        )
    # A type that is not text is not a shape this app writes, and the image it
    # carries still matches one the request sent: it is an echo, so it is
    # stripped rather than handed back to the client that uploaded it.
    return True


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
        try:
            json_value = value.model_dump(mode="json", exclude_none=True)
        except TypeError:
            json_value = value.model_dump()
        except Exception:
            json_value = None
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
    return sanitized


def _sanitize_response_echoed_request_images_for_delivery(
    response: Any,
    request_kwargs: Optional[dict],
) -> Any:
    if not _request_image_references(request_kwargs):
        return response
    if _responses_output_module._response_is_async_iterable(response):
        async def _sanitize_stream() -> AsyncIterator[Any]:
            async for chunk in response:
                yield _sanitize_response_echoed_request_images(chunk, request_kwargs)

        return _sanitize_stream()
    return _sanitize_response_echoed_request_images(response, request_kwargs)


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


def _image_input_stats(value: Any) -> dict[str, int]:
    stats = {
        "image_count": 0,
        "inline_image_count": 0,
        "inline_image_bytes": 0,
        "largest_inline_image_bytes": 0,
    }

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            item_type = current.get("type")
            image_reference: Any = None
            is_image_part = (
                isinstance(item_type, str)
                and item_type in {"input_image", "image_url"}
            )
            if is_image_part:
                image_reference = (
                    current.get("image_url")
                    or current.get("url")
                    or current.get("file_id")
                )
            elif isinstance(current.get("image_url"), (str, dict)):
                is_image_part = True
                image_reference = current.get("image_url")
            if is_image_part:
                stats["image_count"] += 1
                reference = _image_reference_string(image_reference)
                size = _image_data_url_size(reference)
                if size:
                    stats["inline_image_count"] += 1
                    stats["inline_image_bytes"] += size
                    stats["largest_inline_image_bytes"] = max(
                        stats["largest_inline_image_bytes"],
                        size,
                    )
                return
            for child in current.values():
                visit(child)
            return
        if isinstance(current, list):
            for child in current:
                visit(child)

    visit(value)
    return stats


def _image_input_budget(request_kwargs: Optional[dict]) -> Optional[dict[str, Any]]:
    """Return numeric image sizes split at the immutable encrypted boundary."""
    request_kwargs = request_kwargs or {}
    total = {
        "image_count": 0,
        "inline_image_count": 0,
        "inline_image_bytes": 0,
        "largest_inline_image_bytes": 0,
    }
    prefix = {
        "image_count": 0,
        "inline_image_count": 0,
        "inline_image_bytes": 0,
        "largest_inline_image_bytes": 0,
    }
    suffix = {
        "image_count": 0,
        "inline_image_count": 0,
        "inline_image_bytes": 0,
        "largest_inline_image_bytes": 0,
    }

    for key in ("input", "messages"):
        value = request_kwargs.get(key)
        value_stats = _image_input_stats(value)
        suffix_value = _image_bounding_suffix(value)
        suffix_stats = _image_input_stats(suffix_value)
        if isinstance(value, list):
            prefix_value = value[: len(value) - len(suffix_value)]
        else:
            prefix_value = []
        prefix_stats = _image_input_stats(prefix_value)
        for name in total:
            total[name] += value_stats[name]
            suffix[name] += suffix_stats[name]
            prefix[name] += prefix_stats[name]

    if total["image_count"] == 0:
        return None

    return {
        "image_count": total["image_count"],
        "inline_image_count": total["inline_image_count"],
        "inline_image_bytes": total["inline_image_bytes"],
        "largest_inline_image_bytes": total["largest_inline_image_bytes"],
        "encrypted_prefix_image_count": prefix["image_count"],
        "encrypted_prefix_inline_image_count": prefix["inline_image_count"],
        "encrypted_prefix_inline_image_bytes": prefix["inline_image_bytes"],
        "encrypted_prefix_largest_inline_image_bytes": prefix["largest_inline_image_bytes"],
        "new_suffix_image_count": suffix["image_count"],
        "new_suffix_inline_image_count": suffix["inline_image_count"],
        "new_suffix_inline_image_bytes": suffix["inline_image_bytes"],
        "new_suffix_largest_inline_image_bytes": suffix["largest_inline_image_bytes"],
        "encrypted_boundary_present": any(
            _value_has_encrypted_content(request_kwargs.get(key))
            for key in ("input", "messages")
        ),
    }


def _image_bounding_suffix(value: Any) -> Any:
    """The mutable replay suffix: everything after the last signed item."""

    if not isinstance(value, list):
        return value
    last_encrypted_index = max(
        (
            index
            for index, item in enumerate(value)
            if _value_has_encrypted_content(item)
        ),
        default=-1,
    )
    return value[last_encrypted_index + 1 :]


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
