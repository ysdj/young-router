from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import re
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

    def walk(value: Any) -> bool:
        if isinstance(value, list):
            return any(walk(item) for item in value)
        if isinstance(value, dict):
            if (
                value.get("type") == "image_generation_call"
                and _image_generation_result_is_valid(value.get("result"))
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


def _image_generation_result_is_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (bytes, bytearray)):
        return bool(value)
    return value is not None


def _image_generation_result_bytes(value: Any) -> Optional[bytes]:
    """Decode a provider image result without accepting arbitrary text."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, dict):
        for key in ("b64_json", "base64", "data", "result"):
            if key in value:
                decoded = _image_generation_result_bytes(value.get(key))
                if decoded is not None:
                    return decoded
        return None
    if not isinstance(value, str):
        return None
    encoded = value.strip()
    if encoded.startswith("data:image/"):
        header, separator, encoded = encoded.partition(",")
        if not separator or ";base64" not in header.lower():
            return None
    if not encoded:
        return None
    encoded = "".join(encoded.split())
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None


def _image_generation_result_is_valid(value: Any) -> bool:
    """Return whether a result is decodable as a supported raster image."""

    image_bytes = _image_generation_result_bytes(value)
    if not image_bytes:
        return False
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return len(image_bytes) >= 33 and image_bytes[12:16] == b"IHDR" and b"IEND" in image_bytes[-32:]
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return len(image_bytes) >= 14 and image_bytes[-1:] == b";"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return image_bytes.endswith(b"\xff\xd9")
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return len(image_bytes) >= 20
    if image_bytes.startswith(b"BM"):
        return len(image_bytes) >= 26
    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return len(image_bytes) >= 8
    return False


def _image_generation_result_extension(value: Any) -> Optional[str]:
    image_bytes = _image_generation_result_bytes(value)
    if not image_bytes:
        return None
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "webp"
    if image_bytes.startswith(b"BM"):
        return "bmp"
    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    return None


def _image_generation_safe_path_component(value: Any, fallback: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    component = component.strip(".-")[:160]
    return component or fallback


def _image_generation_session_component(request_data: Optional[dict]) -> str:
    """Choose the Codex generated-image directory from request metadata."""

    preferred_keys = {
        "threadid",
        "sessionid",
        "conversationid",
        "taskid",
    }

    def visit(value: Any, depth: int = 0) -> Optional[str]:
        if depth > 7:
            return None
        if isinstance(value, dict):
            for key, candidate in value.items():
                normalized_key = re.sub(r"[^a-z0-9]+", "", str(key).lower())
                if normalized_key in preferred_keys and isinstance(candidate, (str, int)) and str(candidate).strip():
                    return _image_generation_safe_path_component(candidate, "unknown-session")
            for nested in value.values():
                found = visit(nested, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value[:32]:
                found = visit(nested, depth + 1)
                if found:
                    return found
        return None

    if isinstance(request_data, dict):
        for key in ("client_metadata", "litellm_metadata", "metadata", "litellm_params"):
            preferred = request_data.get(key)
            found = visit(preferred)
            if found:
                return found
    return visit(request_data) or "unknown-session"


def _image_generation_call_identifier(item: dict[str, Any]) -> str:
    for key in ("call_id", "id"):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return _image_generation_safe_path_component(value, "image-generation")
    return "image-generation"


_IMAGE_GENERATION_MARKDOWN_LINK_RE = re.compile(
    r"(!\[[^\]]*\]\()(?P<target><[^>]+>|[^)\s]+)(\))"
)
_IMAGE_GENERATION_PATH_RE = re.compile(
    r"(?<![\w])(?:/[A-Za-z0-9._-]+)+/generated_images/[A-Za-z0-9._/-]+"
    r"|(?<![\w/])generated_images/[A-Za-z0-9._/-]+"
)


def _image_generation_path_is_usable(value: str, current_path: str) -> bool:
    path_text = value.removeprefix("file://")
    if path_text == current_path:
        return True
    try:
        path = Path(path_text).expanduser()
        if not path.is_file():
            return False
        header = path.read_bytes()[:32]
    except (OSError, ValueError):
        return False
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        return width > 0 and height > 0 and width * height > 1
    if header.startswith((b"GIF87a", b"GIF89a")) and len(header) >= 10:
        width = int.from_bytes(header[6:8], "little")
        height = int.from_bytes(header[8:10], "little")
        return width > 0 and height > 0 and width * height > 1
    try:
        return path.stat().st_size > 256
    except OSError:
        return False


def _image_generation_rewrite_generated_image_references(
    value: Any,
    saved_paths: list[str],
) -> bool:
    """Point assistant-visible generated-image links at the current result."""

    if not saved_paths:
        return False
    current_path = saved_paths[-1]
    changed = False

    def rewrite_text(text: str) -> str:
        nonlocal changed

        def rewrite_markdown(match: re.Match[str]) -> str:
            nonlocal changed
            target = match.group("target")
            wrapped = target.startswith("<") and target.endswith(">")
            unwrapped = target[1:-1] if wrapped else target
            if (
                "generated_images/" not in unwrapped
                or _image_generation_path_is_usable(unwrapped, current_path)
            ):
                return match.group(0)
            changed = True
            replacement = f"<{current_path}>" if wrapped else current_path
            return f"{match.group(1)}{replacement})"

        rewritten = _IMAGE_GENERATION_MARKDOWN_LINK_RE.sub(rewrite_markdown, text)
        if "generated_images/" in rewritten:
            def rewrite_path(match: re.Match[str]) -> str:
                candidate = match.group(0)
                return (
                    current_path
                    if not _image_generation_path_is_usable(candidate, current_path)
                    else candidate
                )

            rewritten = _IMAGE_GENERATION_PATH_RE.sub(rewrite_path, rewritten)
            if rewritten != text:
                changed = True
        return rewritten

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, nested in list(node.items()):
                if isinstance(nested, str):
                    node[key] = rewrite_text(nested)
                else:
                    walk(nested)
        elif isinstance(node, list):
            for nested in node:
                walk(nested)

    walk(value)
    return changed


def _image_generation_payload_has_current_reference(
    value: Any,
    saved_paths: list[str],
) -> bool:
    if not saved_paths:
        return False
    current_path = saved_paths[-1]
    found = False

    def walk(node: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, str):
            for match in _IMAGE_GENERATION_PATH_RE.finditer(node):
                if _image_generation_path_is_usable(match.group(0), current_path):
                    found = True
                    return
        elif isinstance(node, dict):
            for key, nested in node.items():
                if key in {"saved_path", "result", "id", "call_id"}:
                    continue
                walk(nested)
        elif isinstance(node, list):
            for nested in node:
                walk(nested)

    walk(value)
    return found


def _image_generation_append_reference_to_message(
    item: dict[str, Any],
    saved_path: str,
) -> bool:
    if item.get("type") != "message" or item.get("role") not in {None, "assistant"}:
        return False
    content = item.get("content")
    link = f"![Generated image]({saved_path})"
    if isinstance(content, list):
        for part in reversed(content):
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                part["text"] = f"{text.rstrip()}\n\n{link}"
                return True
        content.append({"type": "output_text", "text": link})
        return True
    if isinstance(content, str):
        item["content"] = f"{content.rstrip()}\n\n{link}"
        return True
    return False


def _image_generation_append_reference_to_terminal_payload(
    payload: Any,
    saved_paths: list[str],
) -> bool:
    if not saved_paths or _image_generation_payload_has_current_reference(payload, saved_paths):
        return False
    saved_path = saved_paths[-1]
    if not isinstance(payload, dict):
        return False
    event_type = payload.get("type")
    if event_type == "response.output_item.done":
        item = payload.get("item")
        return isinstance(item, dict) and _image_generation_append_reference_to_message(
            item,
            saved_path,
        )
    if event_type == "response.completed":
        response = payload.get("response")
        if not isinstance(response, dict):
            return False
        output = response.get("output")
        if isinstance(output, list):
            for item in reversed(output):
                if isinstance(item, dict) and _image_generation_append_reference_to_message(
                    item,
                    saved_path,
                ):
                    return True
        output_text = response.get("output_text")
        if isinstance(output_text, str):
            response["output_text"] = (
                f"{output_text.rstrip()}\n\n![Generated image]({saved_path})"
            )
            return True
        return False
    if event_type == "message" and payload.get("role") == "assistant":
        return _image_generation_append_reference_to_message(payload, saved_path)
    return False


def _image_generation_materialize_result(
    item: dict[str, Any],
    request_data: Optional[dict],
) -> Optional[Path]:
    result = item.get("result")
    if not _image_generation_result_is_valid(result):
        return None
    image_bytes = _image_generation_result_bytes(result)
    extension = _image_generation_result_extension(result)
    if image_bytes is None or extension is None:
        return None
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    output_dir = codex_home / "generated_images" / _image_generation_session_component(request_data)
    output_path = output_dir / f"{_image_generation_call_identifier(item)}.{extension}"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        if not output_path.exists() or output_path.read_bytes() != image_bytes:
            output_path.write_bytes(image_bytes)
    except (OSError, ValueError):
        return None
    return output_path


def _image_generation_stream_chunk_payload(chunk: Any) -> Optional[dict[str, Any]]:
    if isinstance(chunk, dict):
        return chunk
    if hasattr(chunk, "model_dump"):
        try:
            dumped = chunk.model_dump()
        except Exception:
            return None
        return dumped if isinstance(dumped, dict) else None
    if isinstance(chunk, bytes):
        try:
            chunk = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(chunk, str):
        return None
    data_lines = [
        line.split(":", 1)[1].strip()
        for line in chunk.splitlines()
        if line.startswith("data:")
    ]
    payload_text = "\n".join(data_lines) if data_lines else chunk.strip()
    if not payload_text or payload_text == "[DONE]":
        return None
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _image_generation_attach_paths_to_payload(
    payload: Any,
    request_data: Optional[dict],
    saved_paths: Optional[list[str]] = None,
) -> bool:
    changed = False
    discovered_paths: list[str] = []

    def walk(value: Any) -> None:
        nonlocal changed
        if isinstance(value, dict):
            if value.get("type") == "image_generation_call":
                result = value.get("result")
                if _image_generation_result_is_valid(result):
                    output_path = _image_generation_materialize_result(value, request_data)
                    if output_path is not None and value.get("saved_path") != str(output_path):
                        value["saved_path"] = str(output_path)
                        changed = True
                    if output_path is not None:
                        path_text = str(output_path)
                        if path_text not in discovered_paths:
                            discovered_paths.append(path_text)
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
    active_paths = saved_paths if saved_paths is not None else discovered_paths
    if saved_paths is not None:
        for path_text in discovered_paths:
            if path_text not in saved_paths:
                saved_paths.append(path_text)
    if active_paths and _image_generation_rewrite_generated_image_references(
        payload,
        active_paths,
    ):
        changed = True
    if active_paths and _image_generation_append_reference_to_terminal_payload(
        payload,
        active_paths,
    ):
        changed = True
    return changed


def _image_generation_stream_text_chunk_for_delivery(
    chunk: str | bytes,
    request_data: Optional[dict],
    saved_paths: Optional[list[str]] = None,
) -> str | bytes:
    is_bytes = isinstance(chunk, bytes)
    if is_bytes:
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return chunk
    else:
        text = chunk

    lines = text.splitlines(keepends=True)
    saw_data_line = False
    changed = False
    updated_lines: list[str] = []
    for line in lines:
        if not line.startswith("data:"):
            updated_lines.append(line)
            continue
        saw_data_line = True
        payload_text = line.split(":", 1)[1].strip()
        if payload_text == "[DONE]":
            updated_lines.append(line)
            continue
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError):
            updated_lines.append(line)
            continue
        if not isinstance(payload, dict) or not _image_generation_attach_paths_to_payload(
            payload,
            request_data,
            saved_paths,
        ):
            updated_lines.append(line)
            continue
        newline = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
        updated_lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}{newline}")
        changed = True

    if saw_data_line:
        transformed = "".join(updated_lines) if changed else text
    else:
        try:
            payload = json.loads(text.strip())
        except (TypeError, ValueError):
            return chunk
        if not isinstance(payload, dict) or not _image_generation_attach_paths_to_payload(
            payload,
            request_data,
            saved_paths,
        ):
            return chunk
        transformed = json.dumps(payload, ensure_ascii=False)
    return transformed.encode("utf-8") if is_bytes else transformed


def _image_generation_stream_chunk_with_payload(
    chunk: Any,
    payload: dict[str, Any],
) -> Any:
    """Return a stream chunk carrying the mutated response payload."""

    if isinstance(chunk, dict):
        return chunk
    if hasattr(chunk, "model_dump"):
        return payload
    if isinstance(chunk, bytes):
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return chunk
        encoded = _image_generation_stream_chunk_with_payload(text, payload)
        return encoded.encode("utf-8") if isinstance(encoded, str) else chunk
    if not isinstance(chunk, str):
        return chunk
    if not any(line.startswith("data:") for line in chunk.splitlines()):
        return json.dumps(payload, ensure_ascii=False)
    encoded_payload = json.dumps(payload, ensure_ascii=False)
    lines = chunk.splitlines(keepends=True)
    replaced = False
    updated_lines: list[str] = []
    for line in lines:
        if line.startswith("data:") and not replaced:
            newline = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            updated_lines.append(f"data: {encoded_payload}{newline}")
            replaced = True
        elif line.startswith("data:"):
            updated_lines.append(line)
        else:
            updated_lines.append(line)
    return "".join(updated_lines) if replaced else chunk


def _image_generation_stream_chunks_for_delivery(
    chunk: Any,
    request_data: Optional[dict],
    saved_paths: Optional[list[str]] = None,
) -> list[Any]:
    """Persist real image bytes and attach the native Codex path to the item."""

    if isinstance(chunk, (str, bytes)):
        return [
            _image_generation_stream_text_chunk_for_delivery(
                chunk,
                request_data,
                saved_paths,
            )
        ]
    payload = _image_generation_stream_chunk_payload(chunk)
    if payload is not None:
        _image_generation_attach_paths_to_payload(payload, request_data, saved_paths)
    if payload is None:
        return [chunk]
    return [_image_generation_stream_chunk_with_payload(chunk, payload)]


def _response_has_image_generation_result_payload(response: Any) -> bool:
    def walk(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, list):
            return any(walk(item) for item in value)
        if isinstance(value, dict):
            if (
                value.get("type") == "image_generation_call"
                and _image_generation_result_is_valid(value.get("result"))
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
        and _image_generation_result_is_valid(value.get("result"))
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
