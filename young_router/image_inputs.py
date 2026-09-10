from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional
import base64
import binascii
import copy
import io

from . import responses_output as _responses_output_module

from .base import (
    _INLINE_IMAGE_MANY_MAX_EDGE,
    _INLINE_IMAGE_MANY_TOTAL_TARGET_BYTES,
    _INLINE_IMAGE_MANY_TARGET_BYTES,
    _INLINE_IMAGE_HISTORY_MIN_TARGET_BYTES,
    _CODEX_VIEW_IMAGE_PREVIEW_MIN_TARGET_BYTES,
    _CODEX_VIEW_IMAGE_PREVIEW_MAX_TARGET_BYTES,
    _CODEX_VIEW_IMAGE_PREVIEW_TOTAL_TARGET_BYTES,
    _CODEX_VIEW_IMAGE_PREVIEW_TARGET_BYTES,
    _CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER,
    _INLINE_IMAGE_SINGLE_MAX_EDGE,
    _INLINE_IMAGE_SINGLE_TARGET_BYTES,
    _CODEX_VIEW_IMAGE_REFERENCE_MARKER,
    _OMIT_RESPONSE_VALUE,
    _RESPONSES_IMAGE_INPUT_SUPPORT_KEY,
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


def _value_has_encrypted_content(value: Any) -> bool:
    if isinstance(value, dict):
        encrypted_content = value.get("encrypted_content")
        if isinstance(encrypted_content, str) and encrypted_content:
            return True
        return any(_value_has_encrypted_content(child) for child in value.values())
    if isinstance(value, list):
        return any(_value_has_encrypted_content(child) for child in value)
    return False


def _image_bounding_suffix(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    last_encrypted_item = max(
        (
            index
            for index, item in enumerate(value)
            if _value_has_encrypted_content(item)
        ),
        default=-1,
    )
    return value[last_encrypted_item + 1 :]


def _resize_data_url(value: str, *, target_bytes: int, max_edge: int) -> str:
    parsed = _split_image_data_url(value)
    if parsed is None:
        return value
    _, encoded = parsed
    if _image_data_url_size(value) <= target_bytes:
        return value
    try:
        from PIL import Image

        raw = base64.b64decode(encoded, validate=False)
        if raw.startswith(b"\xff\xd8") and not raw.endswith(b"\xff\xd9"):
            raw += b"\xff\xd9"
        with Image.open(io.BytesIO(raw)) as image:
            work = image.convert("RGB")
            quality = 86
            edge = min(max_edge, max(work.size))
            while True:
                resized = work.copy()
                if max(resized.size) > edge:
                    resized.thumbnail((edge, edge))
                buffer = io.BytesIO()
                resized.save(buffer, format="JPEG", quality=quality, optimize=True)
                data = buffer.getvalue()
                if len(data) <= target_bytes:
                    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
                if quality > 76:
                    quality -= 4
                else:
                    next_edge = max(64, int(edge * 0.85))
                    if next_edge == edge:
                        raise ValueError("inline image could not be compressed to the byte limit")
                    edge = next_edge
    except (binascii.Error, OSError, ValueError, ImportError) as exc:
        raise ValueError("oversized inline image could not be compressed") from exc


def _bound_image_data_urls(value: Any, *, target_bytes: int, max_edge: int) -> tuple[Any, bool]:
    if isinstance(value, str):
        if _image_data_url_size(value) <= target_bytes:
            return value, False
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
        if (
            value.get("type") == "custom_tool_call_output"
            and _output_has_codex_view_image_original_references(value.get("output"))
        ):
            return value, False
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


def _output_has_codex_view_image_references(output: Any) -> bool:
    if not isinstance(output, list):
        return False
    return any(
        isinstance(part, dict)
        and part.get("type") == "input_text"
        and isinstance(part.get("text"), str)
        and _CODEX_VIEW_IMAGE_REFERENCE_MARKER in part["text"]
        for part in output
    )


def _output_has_codex_view_image_original_references(output: Any) -> bool:
    if not isinstance(output, list):
        return False
    return any(
        isinstance(part, dict)
        and part.get("type") == "input_text"
        and isinstance(part.get("text"), str)
        and _CODEX_VIEW_IMAGE_ORIGINAL_REFERENCE_MARKER in part["text"]
        for part in output
    )


def _codex_view_image_preview_count(value: Any) -> int:
    """Count inline images in mutable, path-backed ``view_image`` outputs."""

    if isinstance(value, list):
        return sum(_codex_view_image_preview_count(item) for item in value)
    if not isinstance(value, dict):
        return 0
    if (
        value.get("type") == "custom_tool_call_output"
        and _output_has_codex_view_image_references(value.get("output"))
        and not _output_has_codex_view_image_original_references(value.get("output"))
    ):
        output = value.get("output")
        if not isinstance(output, list):
            return 0
        return sum(
            1
            for part in output
            if isinstance(part, dict)
            and part.get("type") == "input_image"
            and _image_data_url_size(part.get("image_url"))
        )
    return sum(_codex_view_image_preview_count(item) for item in value.values())


def _codex_view_image_preview_target(image_count: int) -> int:
    """Choose a per-image preview target for one mutable multi-image batch.

    Small batches get the maximum detail.  Larger batches share a 2.4 MB
    budget, but never fall below 128 KB per image; above that count the budget
    grows linearly so the lower bound remains meaningful.
    """

    if image_count <= 0:
        return _CODEX_VIEW_IMAGE_PREVIEW_MAX_TARGET_BYTES
    total_target = max(
        _CODEX_VIEW_IMAGE_PREVIEW_TOTAL_TARGET_BYTES,
        image_count * _CODEX_VIEW_IMAGE_PREVIEW_MIN_TARGET_BYTES,
    )
    return min(
        _CODEX_VIEW_IMAGE_PREVIEW_MAX_TARGET_BYTES,
        max(
            _CODEX_VIEW_IMAGE_PREVIEW_MIN_TARGET_BYTES,
            total_target // image_count,
        ),
    )


def _bound_codex_view_image_previews(
    value: Any,
    *,
    target_bytes: int = _CODEX_VIEW_IMAGE_PREVIEW_TARGET_BYTES,
) -> tuple[Any, bool]:
    """Keep a medium inline preview while retaining a full-resolution path reference."""

    if isinstance(value, list):
        changed = False
        updated_items: List[Any] = []
        for item in value:
            updated_item, item_changed = _bound_codex_view_image_previews(
                item,
                target_bytes=target_bytes,
            )
            updated_items.append(updated_item)
            changed = changed or item_changed
        return (updated_items if changed else value), changed
    if not isinstance(value, dict):
        return value, False
    if (
        value.get("type") == "custom_tool_call_output"
        and _output_has_codex_view_image_references(value.get("output"))
    ):
        if _output_has_codex_view_image_original_references(value.get("output")):
            return value, False
        updated_output, changed = _bound_image_data_urls(
            value["output"],
            target_bytes=target_bytes,
            max_edge=_INLINE_IMAGE_MANY_MAX_EDGE,
        )
        if not changed:
            return value, False
        updated_value = value.copy()
        updated_value["output"] = updated_output
        return updated_value, True
    changed = False
    updated_value: Dict[Any, Any] = {}
    for key, item in value.items():
        updated_item, item_changed = _bound_codex_view_image_previews(
            item,
            target_bytes=target_bytes,
        )
        updated_value[key] = updated_item
        changed = changed or item_changed
    return (updated_value if changed else value), changed


def _collect_compressible_image_data_url_sizes(value: Any, sizes: List[int]) -> None:
    if (
        isinstance(value, dict)
        and value.get("type") == "custom_tool_call_output"
        and _output_has_codex_view_image_original_references(value.get("output"))
    ):
        return
    _collect_image_data_url_sizes(value, sizes)


def _with_bounded_image_inputs(request_kwargs: dict) -> Optional[dict]:
    sizes: List[int] = []
    encrypted_prefix_image_count = 0
    encrypted_prefix_inline_image_bytes = 0
    bounded_suffixes: Dict[str, Any] = {}
    preview_changed_by_key: Dict[str, bool] = {}
    codex_preview_count = 0
    for key in ("input", "messages"):
        value = request_kwargs.get(key)
        suffix = _image_bounding_suffix(value)
        codex_preview_count += _codex_view_image_preview_count(suffix)
        bounded_suffixes[key] = suffix
        preview_changed_by_key[key] = False
        if isinstance(value, list):
            prefix = value[: len(value) - len(suffix)]
            prefix_stats = _image_input_stats(prefix)
            encrypted_prefix_image_count += prefix_stats["image_count"]
            encrypted_prefix_inline_image_bytes += prefix_stats["inline_image_bytes"]

    codex_preview_target = _codex_view_image_preview_target(codex_preview_count)
    for key in ("input", "messages"):
        value = request_kwargs.get(key)
        suffix = bounded_suffixes[key]
        bounded_suffix, preview_changed = _bound_codex_view_image_previews(
            suffix,
            target_bytes=codex_preview_target,
        )
        bounded_suffixes[key] = bounded_suffix
        preview_changed_by_key[key] = preview_changed
        _collect_compressible_image_data_url_sizes(bounded_suffix, sizes)
    if not sizes:
        return None

    total_image_count = encrypted_prefix_image_count + len(sizes)
    many_images = total_image_count > 1
    target_bytes = _INLINE_IMAGE_SINGLE_TARGET_BYTES
    if many_images:
        remaining_history_bytes = max(
            0,
            _INLINE_IMAGE_MANY_TOTAL_TARGET_BYTES
            - encrypted_prefix_inline_image_bytes,
        )
        remaining_per_image_bytes = remaining_history_bytes // len(sizes)
        history_min_target = _INLINE_IMAGE_HISTORY_MIN_TARGET_BYTES
        if codex_preview_count:
            history_min_target = max(
                history_min_target,
                _CODEX_VIEW_IMAGE_PREVIEW_MIN_TARGET_BYTES,
            )
        target_bytes = min(
            _INLINE_IMAGE_MANY_TARGET_BYTES,
            max(
                history_min_target,
                remaining_per_image_bytes,
            ),
        )
    if all(size <= target_bytes for size in sizes) and not any(
        preview_changed_by_key.values()
    ):
        return None

    max_edge = _INLINE_IMAGE_MANY_MAX_EDGE if many_images else _INLINE_IMAGE_SINGLE_MAX_EDGE

    modified_kwargs = copy.copy(request_kwargs)
    changed = False
    for key in ("input", "messages"):
        value = request_kwargs.get(key)
        suffix = _image_bounding_suffix(value)
        bounded_suffix = bounded_suffixes[key]
        updated_suffix, value_changed = _bound_image_data_urls(
            bounded_suffix,
            target_bytes=target_bytes,
            max_edge=max_edge,
        )
        if value_changed or preview_changed_by_key[key]:
            if isinstance(value, list):
                modified_kwargs[key] = value[: len(value) - len(suffix)] + updated_suffix
            else:
                modified_kwargs[key] = updated_suffix
            changed = True
    return modified_kwargs if changed else None


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
