from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import image_generation as _image_generation_module
from . import trace as _trace_module

from .base import (
    _VISION_BRIDGE_API_BASE_DEFAULT,
    _VISION_BRIDGE_API_BASE_ENV,
    _VISION_BRIDGE_API_KEY_ENV,
    _VISION_BRIDGE_BACKEND_DEFAULT,
    _VISION_BRIDGE_BACKEND_ENV,
    _VISION_BRIDGE_LOCAL_FORMAT_DEFAULT,
    _VISION_BRIDGE_LOCAL_FORMAT_ENV,
    _VISION_BRIDGE_MODEL_DEFAULT,
    _VISION_BRIDGE_MODEL_ENV,
    _VISION_BRIDGE_MODE_DEFAULT,
    _VISION_BRIDGE_MODE_ENV,
    _VISION_BRIDGE_PROMPT_DEFAULT,
    _VISION_BRIDGE_PROMPT_ENV,
    _VISION_BRIDGE_TIMEOUT_DEFAULT,
    _VISION_BRIDGE_TIMEOUT_ENV,
)


_VISION_BRIDGE_ATTEMPTED_METADATA_KEY = "vision_bridge_attempted"
_VISION_BRIDGE_BACKEND_API = "api"
_VISION_BRIDGE_BACKEND_LOCAL = "local"
_VISION_BRIDGE_BACKEND_AUTO = "auto"
_VISION_BRIDGE_BACKEND_OFF = "off"
_VISION_BRIDGE_BACKEND_VALUES = {
    _VISION_BRIDGE_BACKEND_AUTO,
    _VISION_BRIDGE_BACKEND_API,
    _VISION_BRIDGE_BACKEND_LOCAL,
    _VISION_BRIDGE_BACKEND_OFF,
}
_VISION_UNSUPPORTED_MARKERS = (
    "does not support image",
    "doesn't support image",
    "do not support image",
    "not support image",
    "image input is not supported",
    "image input not supported",
    "image inputs are not supported",
    "no endpoints found that support image input",
    "image_url is not supported",
    "image_url not supported",
    "input_image is not supported",
    "input_image not supported",
    "vision is not supported",
    "vision not supported",
    "multi-modal input is not supported",
    "multimodal input is not supported",
    "model does not support vision",
    "unsupported content type image",
    "unsupported image",
    "unsupported input_image",
    "unsupported image_url",
    "invalid image_url",
)


def _env_text(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    text = value.strip()
    return text if text else default


def _env_float(name: str, default: float, *, minimum: float = 0.001) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed >= minimum else default


def _bridge_backend() -> str:
    raw = os.environ.get(_VISION_BRIDGE_BACKEND_ENV)
    mode = os.environ.get(_VISION_BRIDGE_MODE_ENV)
    if isinstance(raw, str) and raw.strip():
        value = raw.strip().lower()
    elif isinstance(mode, str) and mode.strip():
        value = mode.strip().lower()
    else:
        value = _VISION_BRIDGE_BACKEND_DEFAULT
    if value in {"0", "false", "no", "off", "disabled"}:
        return _VISION_BRIDGE_BACKEND_OFF
    if value in {"1", "true", "yes", "on", "enabled"}:
        return _VISION_BRIDGE_BACKEND_AUTO
    if value in _VISION_BRIDGE_BACKEND_VALUES:
        return value
    return _VISION_BRIDGE_BACKEND_AUTO


def _api_base() -> str:
    return _env_text(_VISION_BRIDGE_API_BASE_ENV, _VISION_BRIDGE_API_BASE_DEFAULT).rstrip("/")


def _api_key() -> str:
    return _env_text(_VISION_BRIDGE_API_KEY_ENV, "")


def _bridge_model() -> str:
    return _env_text(_VISION_BRIDGE_MODEL_ENV, _VISION_BRIDGE_MODEL_DEFAULT)


def _bridge_timeout() -> float:
    return _env_float(_VISION_BRIDGE_TIMEOUT_ENV, _VISION_BRIDGE_TIMEOUT_DEFAULT)


def _bridge_prompt() -> str:
    return _env_text(_VISION_BRIDGE_PROMPT_ENV, _VISION_BRIDGE_PROMPT_DEFAULT)


def _local_format() -> str:
    value = _env_text(_VISION_BRIDGE_LOCAL_FORMAT_ENV, _VISION_BRIDGE_LOCAL_FORMAT_DEFAULT).lower()
    return value if value in {"compact", "detailed"} else _VISION_BRIDGE_LOCAL_FORMAT_DEFAULT


def _request_already_attempted(request_kwargs: Optional[dict]) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key)
        if metadata is not None and metadata.get(_VISION_BRIDGE_ATTEMPTED_METADATA_KEY) is True:
            return True
    return False


def _mark_attempted(request_kwargs: dict) -> None:
    for key in ("litellm_metadata", "metadata"):
        metadata = request_kwargs.get(key)
        if isinstance(metadata, dict):
            metadata[_VISION_BRIDGE_ATTEMPTED_METADATA_KEY] = True
        elif key == "litellm_metadata":
            request_kwargs[key] = {_VISION_BRIDGE_ATTEMPTED_METADATA_KEY: True}


def _exception_text(exception: Exception) -> str:
    parts = [str(exception)]
    for attr in ("message", "body", "litellm_debug_info"):
        value = getattr(exception, attr, None)
        if value is not None:
            parts.append(str(value))
    response = getattr(exception, "response", None)
    response_text = getattr(response, "text", None)
    if isinstance(response_text, str):
        parts.append(response_text)
    return "\n".join(parts).lower()


def _looks_like_vision_unsupported_error(exception: Exception) -> bool:
    status_code = getattr(exception, "status_code", None)
    if status_code is not None and status_code not in {400, 404, 422}:
        return False
    text = _exception_text(exception)
    return any(marker in text for marker in _VISION_UNSUPPORTED_MARKERS)


def should_attempt_vision_bridge(exception: Exception, request_kwargs: Optional[dict]) -> bool:
    return (
        _bridge_backend() != _VISION_BRIDGE_BACKEND_OFF
        and isinstance(request_kwargs, dict)
        and _image_generation_module._request_has_image_input(request_kwargs)
        and not _request_already_attempted(request_kwargs)
        and _looks_like_vision_unsupported_error(exception)
    )


def _image_part(reference: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": reference}}


def _chat_completion_payload(reference: str) -> dict[str, Any]:
    return {
        "model": _bridge_model(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _bridge_prompt()},
                    _image_part(reference),
                ],
            }
        ],
        "temperature": 0,
    }


def _request_bytes(url: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "LiteLLM-Menu-Vision-Bridge/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _data_url_to_bytes(reference: str) -> Optional[bytes]:
    if not reference.startswith("data:"):
        return None
    marker = ";base64,"
    marker_index = reference.find(marker)
    if marker_index == -1:
        return None
    encoded = reference[marker_index + len(marker):]
    try:
        return base64.b64decode(encoded, validate=False)
    except Exception:
        return None


def _load_local_reference(reference: str) -> Optional[tuple[bytes, str]]:
    if reference.startswith("data:"):
        data = _data_url_to_bytes(reference)
        return (data, "png") if data is not None else None
    if reference.startswith(("http://", "https://")):
        try:
            data = _request_bytes(reference, timeout=_bridge_timeout())
        except Exception:
            return None
        suffix = pathlib.Path(urllib.parse.urlparse(reference).path).suffix.lstrip(".") or "png"
        return data, suffix
    path = pathlib.Path(reference)
    if path.exists() and path.is_file():
        try:
            return path.read_bytes(), path.suffix.lstrip(".") or "png"
        except OSError:
            return None
    return None


def _vision_helper_source() -> pathlib.Path:
    app_bundle = os.environ.get("LITELLM_APP_PATH") or os.environ.get("APP_BUNDLE_PATH") or "/Applications/LiteLLM Menu.app"
    return pathlib.Path(app_bundle) / "Contents" / "Resources" / "App" / "bin" / "vision_ocr"


def _ensure_local_asset(reference: str) -> Optional[str]:
    loaded = _load_local_reference(reference)
    if loaded is None:
        return None
    data, suffix = loaded
    if not data:
        return None
    digest = hashlib.sha256(data).hexdigest()
    temp_dir = pathlib.Path(tempfile.gettempdir()) / "litellm-menu-vision"
    temp_dir.mkdir(parents=True, exist_ok=True)
    path = temp_dir / f"{digest}.{suffix or 'png'}"
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)
    return str(path)


def _local_vision_description(reference: str) -> str:
    path = _ensure_local_asset(reference)
    if not path:
        return ""
    helper = _vision_helper_source()
    if not helper.exists():
        return ""
    try:
        completed = subprocess.run(
            [str(helper), "--format", _local_format(), path],
            check=False,
            capture_output=True,
            text=True,
            timeout=_bridge_timeout(),
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def _post_chat_completion(payload: dict[str, Any]) -> str:
    url = f"{_api_base()}/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = _api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_bridge_timeout()) as response:
            response_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"vision bridge HTTP {exc.code}: {detail}") from exc
    data = json.loads(response_body)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [item.get("text") for item in content if isinstance(item, dict)]
            return "\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    text = first.get("text")
    return text.strip() if isinstance(text, str) else ""


async def _describe_image(reference: str) -> str:
    backend = _bridge_backend()
    if backend == _VISION_BRIDGE_BACKEND_OFF:
        return ""
    if backend in {_VISION_BRIDGE_BACKEND_AUTO, _VISION_BRIDGE_BACKEND_API}:
        try:
            payload = _chat_completion_payload(reference)
            description = await asyncio.to_thread(_post_chat_completion, payload)
            if description or backend == _VISION_BRIDGE_BACKEND_API:
                return description
        except Exception as exc:
            if backend == _VISION_BRIDGE_BACKEND_API:
                raise
            _trace_module._route_trace(
                "vision_bridge_api_fallback_to_local",
                exception=str(exc),
            )
    return await asyncio.to_thread(_local_vision_description, reference)


def _visual_context_block(descriptions: list[tuple[int, str]]) -> str:
    lines = ["The original request included image input. A local vision bridge produced this visual context:"]
    for index, description in descriptions:
        lines.append(f"\nImage {index}:\n{description.strip() or '[no description returned]'}")
    return "\n".join(lines).strip()


def _without_image_parts(value: Any) -> Any:
    if isinstance(value, list):
        items = [_without_image_parts(item) for item in value]
        return [item for item in items if item is not None]
    if isinstance(value, dict):
        item_type = value.get("type")
        if item_type in {"input_image", "image_url"} or isinstance(value.get("image_url"), (str, dict)):
            return None
        return {key: _without_image_parts(child) for key, child in value.items()}
    return value


def _append_responses_visual_context(request_kwargs: dict, visual_context: str) -> None:
    input_value = _without_image_parts(request_kwargs.get("input"))
    context_part = {"type": "input_text", "text": visual_context}
    if isinstance(input_value, list):
        input_value.append({"role": "user", "content": [context_part]})
        request_kwargs["input"] = input_value
    elif isinstance(input_value, str):
        request_kwargs["input"] = f"{input_value}\n\n{visual_context}"
    elif input_value is None:
        request_kwargs["input"] = [{"role": "user", "content": [context_part]}]
    else:
        request_kwargs["input"] = [input_value, {"role": "user", "content": [context_part]}]


def _append_chat_visual_context(request_kwargs: dict, visual_context: str) -> None:
    messages = _without_image_parts(request_kwargs.get("messages"))
    if not isinstance(messages, list):
        messages = []
    messages.append({"role": "user", "content": visual_context})
    request_kwargs["messages"] = messages


def _copy_request_kwargs_for_bridge(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy_request_kwargs_for_bridge(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_request_kwargs_for_bridge(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_request_kwargs_for_bridge(item) for item in value)
    if isinstance(value, set):
        try:
            return copy.deepcopy(value)
        except Exception:
            return set(value)
    if isinstance(value, frozenset):
        try:
            return copy.deepcopy(value)
        except Exception:
            return frozenset(value)
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


async def bridged_request_kwargs(request_kwargs: dict) -> Optional[dict]:
    references = sorted(_image_generation_module._request_image_references(request_kwargs))
    if not references:
        return None
    descriptions: list[tuple[int, str]] = []
    for index, reference in enumerate(references, start=1):
        description = await _describe_image(reference)
        descriptions.append((index, description))
    bridged_kwargs = _copy_request_kwargs_for_bridge(request_kwargs)
    _mark_attempted(bridged_kwargs)
    visual_context = _visual_context_block(descriptions)
    if _image_generation_module._request_is_responses_api(bridged_kwargs) or "input" in bridged_kwargs:
        _append_responses_visual_context(bridged_kwargs, visual_context)
    else:
        _append_chat_visual_context(bridged_kwargs, visual_context)
    _trace_module._route_trace(
        "vision_bridge_request_rewritten",
        image_count=len(descriptions),
        request=_trace_module._trace_request_summary(bridged_kwargs),
    )
    return bridged_kwargs


async def retry_with_vision_bridge(
    original_function: Any,
    request_kwargs: dict,
    *,
    model_group: Optional[str] = None,
) -> Any:
    bridged_kwargs = await bridged_request_kwargs(request_kwargs)
    if bridged_kwargs is None:
        raise RuntimeError("vision bridge could not extract image references")
    if (
        isinstance(model_group, str)
        and model_group.strip()
        and not (isinstance(bridged_kwargs.get("model"), str) and bridged_kwargs["model"].strip())
    ):
        bridged_kwargs["model"] = model_group
    return await original_function(**bridged_kwargs)
