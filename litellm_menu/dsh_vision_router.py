"""dsh-vision-router compatible fallback for LiteLLM Menu.

The upstream project is a Node/Cordis plugin and cannot be imported into the
Python LiteLLM process.  This module keeps its portable part: an ordered
vision-provider chain (local OpenAI-compatible backends, configured HTTP
providers, and the optional anonymous OVH chain).  It is deliberately a
fallback adapter: the selected LiteLLM deployment remains the source of the
answer, and this module is entered only after that deployment rejects image
input.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar
from typing import Any, Optional

from . import image_inputs as _image_inputs_module
from . import request_context as _request_context_module
from . import responses_request as _responses_request_module
from . import trace as _trace_module

_DSH_VISION_ROUTER_CONFIG_ENV = "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON"
_DSH_VISION_ROUTER_ENABLED_ENV = "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED"
_DSH_VISION_ROUTER_BACKEND_ENV = "LITELLM_MENU_DSH_VISION_ROUTER_BACKEND"
_DSH_VISION_ROUTER_FREE_FALLBACK_ENV = "LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK"
_DSH_VISION_ROUTER_TIMEOUT_SECONDS_ENV = "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS"
_DSH_VISION_ROUTER_MAX_TOKENS_ENV = "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS"
_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED_ENV = "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED"
_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED_ENV = "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED"
_DSH_VISION_ROUTER_ATTEMPTED_METADATA_KEY = "dsh_vision_router_attempted"
_BACKEND_API = "api"
_BACKEND_LOCAL = "local"
_BACKEND_AUTO = "auto"
_BACKEND_OFF = "off"
_BACKEND_VALUES = {
    _BACKEND_AUTO,
    _BACKEND_API,
    _BACKEND_LOCAL,
    _BACKEND_OFF,
}
_DEFAULT_TIMEOUT_SECONDS = 45.0
_DEFAULT_PROMPT = (
    "Describe the image accurately for a text-only language model. "
    "Include visible text, UI elements, layout, objects, and any important details."
)
_LOCAL_OCR_FORMAT = "compact"
_VISION_UNSUPPORTED_MARKERS = (
    "does not support image",
    "doesn't support image",
    "do not support image",
    "not support image",
    "image input is not supported",
    "image input not supported",
    "image inputs are not supported",
    "image input unsupported",
    "image inputs unsupported",
    "no endpoints found that support image input",
    "image_url is not supported",
    "image_url not supported",
    "input_image is not supported",
    "input_image not supported",
    "vision is not supported",
    "vision not supported",
    "vision input is not supported",
    "vision input not supported",
    "does not support vision",
    "doesn't support vision",
    "do not support vision",
    "does not support multimodal input",
    "doesn't support multimodal input",
    "do not support multimodal input",
    "multi-modal input is not supported",
    "multimodal input is not supported",
    "model does not support vision",
    "model cannot accept image input",
    "model can't accept image input",
    "model cannot process image input",
    "model can't process image input",
)
_DEFAULT_HTTP_PROVIDERS = (
    ("ovh", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "Qwen3.5-397B-A17B"),
    ("ovh", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "Qwen2.5-VL-72B-Instruct"),
    ("ovh", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "Qwen3.6-27B"),
    ("ovh", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "Mistral-Small-3.2-24B-Instruct-2506"),
    ("ovh", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", "Qwen3.5-9B"),
)
_LOCAL_OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
_LOCAL_OLLAMA_MODEL = "qwen2.5vl"
_LOCAL_LM_STUDIO_BASE_URL = "http://localhost:1234/v1"
_DEFAULT_HTTP_BASE_URL = _DEFAULT_HTTP_PROVIDERS[0][1]
_ACTIVE_PROVIDER: ContextVar[dict[str, Any] | None] = ContextVar(
    "dsh_vision_router_active_provider",
    default=None,
)
_ACTIVE_REQUEST_PROMPT: ContextVar[str | None] = ContextVar(
    "dsh_vision_router_active_request_prompt",
    default=None,
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


def _env_bool(name: str) -> bool | None:
    """Read an explicitly configured boolean environment override."""

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on", "enabled"}:
        return True
    if lowered in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _config_float(name: str, default: float, *, minimum: float = 0.001, maximum: float | None = None) -> float:
    value = _router_config().get(name)
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum or (maximum is not None and parsed > maximum):
        return default
    return parsed


def _config_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    value = _router_config().get(name)
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum or (maximum is not None and parsed > maximum):
        return default
    return parsed


def _router_backend() -> str:
    configured = _env_text(_DSH_VISION_ROUTER_BACKEND_ENV, "").lower()
    if not configured or configured == "inherit":
        configured = _router_config().get("backend")
    if isinstance(configured, str) and configured.strip():
        value = configured.strip().lower()
    else:
        value = _BACKEND_AUTO
    return value if value in _BACKEND_VALUES else _BACKEND_AUTO


def _router_timeout() -> float:
    direct = os.environ.get(_DSH_VISION_ROUTER_TIMEOUT_SECONDS_ENV)
    if direct is not None and direct.strip() and direct.strip().lower() not in {"inherit", "auto"}:
        return _env_float(
            _DSH_VISION_ROUTER_TIMEOUT_SECONDS_ENV,
            _DEFAULT_TIMEOUT_SECONDS,
            minimum=0.001,
        )
    configured = _config_float("timeoutSeconds", 0.0, minimum=0.001, maximum=600.0)
    if configured > 0:
        return configured
    return _DEFAULT_TIMEOUT_SECONDS


def _router_max_tokens() -> int:
    direct = os.environ.get(_DSH_VISION_ROUTER_MAX_TOKENS_ENV)
    if direct is not None and direct.strip() and direct.strip().lower() not in {"inherit", "auto"}:
        return max(1, min(32768, int(_env_float(_DSH_VISION_ROUTER_MAX_TOKENS_ENV, 4096, minimum=1))))
    return _config_int("maxTokens", 4096, minimum=1, maximum=32768)


def _router_prompt() -> str:
    configured = _router_config().get("prompt")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return _DEFAULT_PROMPT


def _router_local_format() -> str:
    configured = _router_config().get("localFormat")
    if isinstance(configured, str) and configured.strip().lower() in {"compact", "detailed"}:
        return configured.strip().lower()
    return _LOCAL_OCR_FORMAT


def _router_config() -> dict[str, Any]:
    raw = _env_text(_DSH_VISION_ROUTER_CONFIG_ENV, "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        _trace_module._route_trace("dsh_vision_router_invalid_config")
        return {"enabled": False}
    if not isinstance(parsed, dict):
        _trace_module._route_trace("dsh_vision_router_invalid_config")
        return {"enabled": False}
    return parsed


def _router_enabled() -> bool:
    direct = _env_bool(_DSH_VISION_ROUTER_ENABLED_ENV)
    if direct is not None:
        return direct
    config = _router_config()
    return config.get("enabled") is not False


def _router_free_fallback(config: dict[str, Any]) -> bool:
    direct = _env_bool(_DSH_VISION_ROUTER_FREE_FALLBACK_ENV)
    if direct is not None:
        return direct
    return config.get("freeFallback", True) is not False


def _router_local_enabled(config: dict[str, Any], key: str, env_name: str) -> bool:
    direct = _env_bool(env_name)
    if direct is not None:
        return direct
    local = config.get(key)
    return isinstance(local, dict) and local.get("enabled") is True


def _request_model_supports_vision(request_kwargs: Optional[dict]) -> bool:
    """Return true only for explicit positive capability metadata.

    Missing metadata is intentionally not treated as support: the upstream
    request is allowed to prove its capability, and this adapter is considered
    only after an explicit unsupported-image response.
    """

    info = _request_context_module._request_model_info(request_kwargs)
    if not isinstance(info, dict):
        return False
    if info.get("supports_vision") is True or info.get("supports_responses_image_input") is True:
        return True
    for key in (
        "input_modalities",
        "inputModalities",
        "modalities",
        "supported_modalities",
        "supportedModalities",
    ):
        value = info.get(key)
        if isinstance(value, str) and value.strip().lower() in {"image", "vision", "multimodal", "multimodal-input"}:
            return True
        if isinstance(value, (list, tuple, set, frozenset)) and any(
            isinstance(item, str) and item.strip().lower() in {"image", "vision", "multimodal", "multimodal-input"}
            for item in value
        ):
            return True
    return False


def _request_already_attempted(request_kwargs: Optional[dict]) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if metadata is not None and metadata.get(_DSH_VISION_ROUTER_ATTEMPTED_METADATA_KEY) is True:
            return True
    return False


def _mark_attempted(request_kwargs: dict) -> None:
    for key in ("litellm_metadata", "metadata"):
        metadata = request_kwargs.get(key)
        if isinstance(metadata, dict):
            metadata[_DSH_VISION_ROUTER_ATTEMPTED_METADATA_KEY] = True
        elif key == "litellm_metadata":
            request_kwargs[key] = {_DSH_VISION_ROUTER_ATTEMPTED_METADATA_KEY: True}


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


def should_attempt_dsh_vision_router(exception: Exception, request_kwargs: Optional[dict]) -> bool:
    """Return whether an unsupported-image failure should enter the router."""

    return (
        _router_backend() != _BACKEND_OFF
        and _router_enabled()
        and isinstance(request_kwargs, dict)
        and _image_inputs_module._request_has_image_input(request_kwargs)
        and not _request_model_supports_vision(request_kwargs)
        and not _request_already_attempted(request_kwargs)
        and _looks_like_vision_unsupported_error(exception)
    )


def _provider_from_entry(
    entry: Any,
    *,
    default_name: str = "http",
    default_base_url: str = "",
    default_model: str = "",
    default_max_tokens: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    base_url = entry.get("baseURL", entry.get("base_url", entry.get("api_base", default_base_url)))
    model = entry.get("model", default_model)
    if not isinstance(base_url, str) or not base_url.strip() or not isinstance(model, str) or not model.strip():
        return None
    api_key_env = entry.get("apiKeyEnv", entry.get("api_key_env", ""))
    if not isinstance(api_key_env, str):
        api_key_env = ""
    provider_format = entry.get("format", entry.get("protocol", "openai"))
    if not isinstance(provider_format, str) or provider_format.lower() not in {"openai", "anthropic"}:
        provider_format = "openai"
    configured_max_tokens = (
        _router_max_tokens() if default_max_tokens is None else default_max_tokens
    )
    max_tokens = entry.get("maxTokens", entry.get("max_tokens", configured_max_tokens))
    try:
        max_tokens = max(1, min(32768, int(max_tokens)))
    except (TypeError, ValueError):
        max_tokens = configured_max_tokens
    temperature = entry.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= float(temperature) <= 2:
        temperature = None
    top_p = entry.get("top_p", entry.get("topP"))
    if isinstance(top_p, bool) or not isinstance(top_p, (int, float)) or not 0 <= float(top_p) <= 1:
        top_p = None
    return {
        "name": str(entry.get("name") or entry.get("provider") or default_name),
        "base_url": base_url.strip().rstrip("/"),
        "model": model.strip(),
        "api_key": "",
        "api_key_env": api_key_env.strip(),
        "format": provider_format.lower(),
        "max_tokens": max_tokens,
        "temperature": float(temperature) if temperature is not None else None,
        "top_p": float(top_p) if top_p is not None else None,
    }


def _upstream_pair_entries(entry: Any) -> list[dict[str, Any]]:
    """Translate dsh's adapter-pair shape when it names a portable HTTP route.

    dsh can dispatch arbitrary registered adapters (WebSocket, RPC, private
    transports, and so on).  LiteLLM cannot invoke those adapters from this
    fallback process, but its built-in ``vision-http`` pair is equivalent to
    the direct OVH HTTP chain.  Explicit HTTP URLs are also accepted as a
    useful migration path; unknown adapter names are skipped with a trace
    instead of being mistaken for a URL.
    """
    if not isinstance(entry, dict):
        return []
    provider = entry.get("provider")
    model = entry.get("model")
    if not isinstance(provider, str) or not provider.strip() or not isinstance(model, str) or not model.strip():
        return []
    provider_name = provider.strip()
    model_name = model.strip()
    base_url = ""
    if provider_name.lower() in {"vision-http", "ovh", "ovhcloud"}:
        # dsh injects local rows as vision-http pairs for its own adapter.
        # They are already materialized from localOllama/localLmStudio below;
        # treating their names as OVH models would add a guaranteed bad HTTP
        # attempt before the real local row.
        if model_name.lower().startswith(("local-ollama/", "local-lmstudio/")):
            _trace_module._route_trace("dsh_vision_router_local_pair_already_materialized")
            return []
        base_url = _DEFAULT_HTTP_BASE_URL
        if model_name.lower().startswith("ovh/"):
            model_name = model_name[4:].strip()
    elif provider_name.lower().startswith(("http://", "https://")):
        base_url = provider_name
        prefix = provider_name.rstrip("/") + "/"
        if model_name.startswith(prefix):
            model_name = model_name[len(prefix):]
    else:
        _trace_module._route_trace("dsh_vision_router_unsupported_provider_entry")
        return []
    if not model_name:
        return []

    common = {
        "name": provider_name,
        "baseURL": base_url,
        "model": model_name,
        "apiKeyEnv": entry.get("apiKeyEnv", entry.get("api_key_env", "")),
        "maxTokens": entry.get("maxTokens", entry.get("max_tokens", _router_max_tokens())),
        "format": entry.get("format", entry.get("protocol", "openai")),
        "temperature": entry.get("temperature"),
        "top_p": entry.get("top_p", entry.get("topP")),
    }
    result = [common]
    fallbacks = entry.get("fallbacks")
    if isinstance(fallbacks, list):
        for fallback in fallbacks:
            if not isinstance(fallback, str) or not fallback.strip():
                continue
            fallback_name = fallback.strip()
            if provider_name.lower() in {"vision-http", "ovh", "ovhcloud"} and fallback_name.lower().startswith("ovh/"):
                fallback_name = fallback_name[4:].strip()
            result.append({**common, "model": fallback_name})
    return result


def _append_provider_if_new(providers: list[dict[str, Any]], item: Optional[dict[str, Any]]) -> None:
    if item is None:
        return
    identity = (item["base_url"], item["model"], item.get("format", "openai"))
    if any(
        (existing["base_url"], existing["model"], existing.get("format", "openai")) == identity
        for existing in providers
    ):
        return
    providers.append(item)


def _configured_provider_chain() -> list[dict[str, Any]]:
    config = _router_config()
    backend = _router_backend()
    if backend == _BACKEND_OFF:
        return []
    local_providers: list[dict[str, Any]] = []

    if backend != _BACKEND_API:
        for key, default_name in (("localOllama", "local-ollama"), ("localLmStudio", "local-lmstudio")):
            local = config.get(key)
            enabled_env = (
                _DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED_ENV
                if key == "localOllama"
                else _DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED_ENV
            )
            if _router_local_enabled(config, key, enabled_env):
                local = local if isinstance(local, dict) else {}
                if key == "localOllama":
                    default_base_url, default_model = _LOCAL_OLLAMA_BASE_URL, _LOCAL_OLLAMA_MODEL
                else:
                    default_base_url, default_model = _LOCAL_LM_STUDIO_BASE_URL, ""
                item = _provider_from_entry(
                    local,
                    default_name=default_name,
                    default_base_url=default_base_url,
                    default_model=default_model,
                    default_max_tokens=2048,
                )
                _append_provider_if_new(local_providers, item)

    # The legacy local mode means "use the bundled OCR helper".  If a dsh
    # local provider is explicitly enabled, let it participate first and keep
    # OCR as the final local leaf (see _dsh_describe_image).
    if backend == _BACKEND_LOCAL:
        return local_providers

    providers: list[dict[str, Any]] = list(local_providers)

    for key, value in (("providers", config.get("providers")), ("httpProviders", config.get("httpProviders"))):
        if not isinstance(value, list):
            continue
        for entry in value:
            item = _provider_from_entry(entry)
            if item is not None:
                _append_provider_if_new(providers, item)
                continue
            if key == "providers":
                for pair in _upstream_pair_entries(entry):
                    _append_provider_if_new(providers, _provider_from_entry(pair))

    # Accept a compact single HTTP provider object as a convenience for JSON
    # profiles exported by earlier dsh-vision-router revisions.
    single = config.get("provider")
    if isinstance(single, dict):
        item = _provider_from_entry(single, default_name="configured")
        if item is not None:
            _append_provider_if_new(providers, item)
    elif isinstance(single, str) and isinstance(config.get("model"), str):
        pair = {
            "provider": single,
            "model": config["model"],
            "fallbacks": config.get("fallbacks", []),
        }
        for entry in _upstream_pair_entries(pair):
            _append_provider_if_new(providers, _provider_from_entry(entry))

    if backend != _BACKEND_API and _router_free_fallback(config):
        seen = {(item["base_url"], item["model"]) for item in providers}
        for name, base_url, model in _DEFAULT_HTTP_PROVIDERS:
            if (base_url, model) in seen:
                continue
            providers.append(
                {
                    "name": name,
                    "base_url": base_url,
                    "model": model,
                    "api_key": "",
                    "api_key_env": "",
                    "format": "openai",
                    "max_tokens": _router_max_tokens(),
                    "temperature": None,
                    "top_p": None,
                }
            )
    return providers


def _image_media_type(suffix: str) -> str:
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "png": "image/png",
    }.get(suffix.lower().lstrip("."), "image/png")


def _openai_image_reference(reference: str) -> str:
    """Return a provider-readable URL and reject opaque attachment IDs.

    OpenAI-compatible endpoints can dereference HTTP URLs, but they cannot
    see a path or a LiteLLM/Responses ``file_id`` in the proxy process.  Local
    files are therefore converted to data URLs; unresolved references fail
    the vision attempt instead of producing a misleading text-only retry.
    """
    value = reference.strip()
    if value.startswith("data:"):
        data = _data_url_to_bytes(value)
        if data is None or not data:
            raise RuntimeError("dsh-vision-router could not decode image data")
        return value
    if value.startswith(("http://", "https://")):
        return value
    loaded = _load_local_reference(value)
    if loaded is None:
        raise RuntimeError("dsh-vision-router could not materialize image reference")
    data, suffix = loaded
    if not data:
        raise RuntimeError("dsh-vision-router received an empty image")
    return (
        f"data:{_image_media_type(suffix)};base64,"
        f"{base64.b64encode(data).decode('ascii')}"
    )


def _image_part(reference: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": _openai_image_reference(reference)}}


def _chat_completion_payload(reference: str, provider: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    active = provider or _ACTIVE_PROVIDER.get() or {}
    config = _router_config()
    prompt = config.get("prompt", _router_prompt())
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = _router_prompt()
    question = _ACTIVE_REQUEST_PROMPT.get()
    if isinstance(question, str) and question.strip() and question.strip() not in prompt:
        prompt = f"{prompt.rstrip()}\n\nUser request:\n{question.strip()}"
    payload = {
        "model": active.get("model") or "",
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    _image_part(reference),
                ],
            }
        ],
        "max_tokens": active.get("max_tokens", _router_max_tokens()),
    }
    if active.get("temperature") is not None:
        payload["temperature"] = active["temperature"]
    if active.get("top_p") is not None:
        payload["top_p"] = active["top_p"]
    return payload


def _request_bytes(url: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "LiteLLM-Menu-dsh-vision-router/1.7.6"})
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
    if not encoded or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", encoded):
        return None
    if "=" in encoded[:-2]:
        return None
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    try:
        return base64.b64decode(encoded, validate=True)
    except Exception:
        return None


def _load_local_reference(reference: str) -> Optional[tuple[bytes, str]]:
    if reference.startswith("data:"):
        data = _data_url_to_bytes(reference)
        return (data, "png") if data is not None else None
    if reference.startswith(("http://", "https://")):
        try:
            data = _request_bytes(reference, timeout=_router_timeout())
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
    override = os.environ.get("LITELLM_MENU_VISION_HELPER", "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    core_root = os.environ.get("LITELLM_MENU_CORE_ROOT", "").strip()
    if core_root:
        return pathlib.Path(core_root).expanduser() / "bin" / "vision_ocr"
    return pathlib.Path(__file__).resolve().parents[1] / "bin" / "vision_ocr"


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


def _dsh_local_vision_description(reference: str) -> str:
    path = _ensure_local_asset(reference)
    helper = _vision_helper_source()
    if not path or not helper.exists():
        return ""
    try:
        completed = subprocess.run(
            [str(helper), "--format", _router_local_format(), path],
            check=False,
            capture_output=True,
            text=True,
            timeout=_router_timeout(),
        )
    except Exception:
        return ""
    return (completed.stdout or "").strip() if completed.returncode == 0 else ""


def _provider_api_key(provider: dict[str, Any]) -> str:
    env_name = provider.get("api_key_env")
    if isinstance(env_name, str) and env_name.strip():
        return _env_text(env_name, "")
    value = provider.get("api_key")
    return value.strip() if isinstance(value, str) else ""


def _post_chat_completion(payload: dict[str, Any]) -> str:
    """Call the active OpenAI-compatible dsh provider.

    Keeping the payload-only signature preserves the small testing seam used
    by the original LiteLLM hook while provider routing lives in ContextVar.
    """

    provider = _ACTIVE_PROVIDER.get() or {}
    url = f"{str(provider.get('base_url') or _DEFAULT_HTTP_BASE_URL).rstrip('/')}/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = _provider_api_key(provider)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_router_timeout()) as response:
            response_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        error = RuntimeError(f"dsh vision provider HTTP {exc.code}: {detail}")
        error.status_code = exc.code
        raise error from exc
    data = json.loads(response_body)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [item.get("text") for item in content if isinstance(item, dict)]
            return "\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    text = choices[0].get("text")
    return text.strip() if isinstance(text, str) else ""


def _anthropic_image_source(reference: str) -> Optional[dict[str, str]]:
    """Turn an OpenAI image reference into an Anthropic base64 source."""

    media_type = "image/png"
    data: Optional[bytes]
    if reference.startswith("data:"):
        header, separator, encoded = reference.partition(",")
        if not separator:
            return None
        media_type = header[5:].split(";", 1)[0].strip() or media_type
        data = _data_url_to_bytes(reference)
    else:
        loaded = _load_local_reference(reference)
        if loaded is None:
            return None
        data, suffix = loaded
        suffix = suffix.lower()
        media_type = _image_media_type(suffix)
    if not data:
        return None
    return {
        "type": "base64",
        "media_type": media_type,
        "data": base64.b64encode(data).decode("ascii"),
    }


def _anthropic_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    converted: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            converted.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"text", "input_text"} and isinstance(part.get("text"), str):
            converted.append({"type": "text", "text": part["text"]})
            continue
        if part_type == "image" and isinstance(part.get("source"), dict):
            source = part["source"]
            if source.get("type") == "base64" and isinstance(source.get("data"), str):
                converted.append({"type": "image", "source": source})
                continue
            raise RuntimeError("dsh-vision-router received an unsupported Anthropic image source")
        if part_type in {"image_url", "input_image"} or "image_url" in part:
            image = part.get("image_url") or part.get("file_id") or part.get("url")
            if isinstance(image, dict):
                image = image.get("url") or image.get("file_id")
            if not isinstance(image, str) or not image.strip():
                raise RuntimeError("dsh-vision-router could not find an image reference")
            source = _anthropic_image_source(image)
            if source is None:
                raise RuntimeError("dsh-vision-router could not materialize image reference")
            converted.append({"type": "image", "source": source})
    return converted


def _anthropic_messages(messages: Any) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return "", converted
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user").lower()
        content = _anthropic_content(message.get("content"))
        if not content:
            continue
        if role == "system":
            system_parts.extend(
                item["text"] for item in content if item.get("type") == "text" and isinstance(item.get("text"), str)
            )
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(content)
        else:
            converted.append({"role": role, "content": content})
    if converted and converted[0]["role"] != "user":
        converted.insert(
            0,
            {"role": "user", "content": [{"type": "text", "text": "(conversation history)"}]},
        )
    return "\n\n".join(part for part in system_parts if part.strip()), converted


def _post_anthropic_completion(payload: dict[str, Any]) -> str:
    provider = _ACTIVE_PROVIDER.get() or {}
    base_url = str(provider.get("base_url") or _DEFAULT_HTTP_BASE_URL).rstrip("/")
    if base_url.endswith("/messages"):
        base_url = base_url[: -len("/messages")]
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    url = f"{base_url}/v1/messages"
    system, messages = _anthropic_messages(payload.get("messages"))
    request_body: dict[str, Any] = {
        "model": payload.get("model") or "",
        "max_tokens": payload.get("max_tokens", _router_max_tokens()),
        "messages": messages,
    }
    if system:
        request_body["system"] = system
    if payload.get("temperature") is not None:
        request_body["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        request_body["top_p"] = payload["top_p"]
    body = json.dumps(request_body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    api_key = _provider_api_key(provider)
    if api_key:
        headers["x-api-key"] = api_key
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_router_timeout()) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        error = RuntimeError(f"dsh vision provider HTTP {exc.code}: {detail}")
        error.status_code = exc.code
        raise error from exc
    content = data.get("content") if isinstance(data, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(item["text"]).strip()
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()
        )
    return ""


async def _describe_with_provider(reference: str, provider: dict[str, Any]) -> str:
    token = _ACTIVE_PROVIDER.set(provider)
    try:
        payload = _chat_completion_payload(reference, provider)
        function = _post_anthropic_completion if provider.get("format") == "anthropic" else _post_chat_completion
        return await asyncio.to_thread(function, payload)
    finally:
        _ACTIVE_PROVIDER.reset(token)


async def _dsh_describe_image(reference: str) -> str:
    backend = _router_backend()
    if backend == _BACKEND_OFF or not _router_enabled():
        return ""
    providers = _configured_provider_chain()
    last_error: Optional[Exception] = None
    for provider in providers:
        try:
            description = await _describe_with_provider(reference, provider)
            if description:
                _trace_module._route_trace(
                    "dsh_vision_router_provider_success",
                    provider=provider.get("name"),
                    model=provider.get("model"),
                )
                return description
        except Exception as exc:
            last_error = exc
            _trace_module._route_trace(
                "dsh_vision_router_provider_failure",
                provider=provider.get("name"),
                model=provider.get("model"),
                exception=str(exc),
            )
    if backend == _BACKEND_API and last_error is not None:
        raise last_error
    if backend == _BACKEND_API:
        return ""
    # Preserve the portable Core OCR leaf as a local last resort.  It never
    # runs when the selected model already supports vision.
    return await asyncio.to_thread(_dsh_local_vision_description, reference)


def _visual_context_block(descriptions: list[tuple[int, str]]) -> str:
    lines = [
        "The original request included image input. "
        "dsh-vision-router produced this visual context:"
    ]
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


def _copy_request_kwargs_for_router(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy_request_kwargs_for_router(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_request_kwargs_for_router(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_request_kwargs_for_router(item) for item in value)
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


def _ordered_request_image_references(request_kwargs: dict) -> list[str]:
    references: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            item_type = value.get("type")
            candidate: Any = None
            if item_type in {"input_image", "image_url"}:
                candidate = value.get("image_url") or value.get("url") or value.get("file_id")
            elif "image_url" in value:
                candidate = value.get("image_url")
            if isinstance(candidate, dict):
                candidate = candidate.get("url") or candidate.get("file_id")
            if isinstance(candidate, str) and candidate.strip() and candidate not in references:
                references.append(candidate)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(request_kwargs.get("input"))
    visit(request_kwargs.get("messages"))
    return references


def _request_vision_prompt(request_kwargs: dict) -> str:
    texts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") in {"text", "input_text"} and isinstance(value.get("text"), str):
                text = value["text"].strip()
                if text:
                    texts.append(text)
                return
            if isinstance(value.get("content"), str) and value.get("role") in {"user", "system"}:
                text = value["content"].strip()
                if text:
                    texts.append(text)
                return
            for key, child in value.items():
                if key in {"image_url", "url", "file_id", "data", "text"}:
                    continue
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(request_kwargs.get("input"))
    visit(request_kwargs.get("messages"))
    if not texts:
        return ""
    return "\n".join(texts[-8:])[-4000:]


async def dsh_vision_router_request_kwargs(request_kwargs: dict) -> Optional[dict]:
    references = _ordered_request_image_references(request_kwargs)
    if not references:
        return None
    descriptions: list[tuple[int, str]] = []
    prompt_token = _ACTIVE_REQUEST_PROMPT.set(_request_vision_prompt(request_kwargs))
    try:
        for index, reference in enumerate(references, start=1):
            description = await _dsh_describe_image(reference)
            descriptions.append((index, description))
    finally:
        _ACTIVE_REQUEST_PROMPT.reset(prompt_token)
    if not any(description for _, description in descriptions):
        raise RuntimeError("dsh-vision-router produced no visual context")
    routed_kwargs = _copy_request_kwargs_for_router(request_kwargs)
    _mark_attempted(routed_kwargs)
    visual_context = _visual_context_block(descriptions)
    if _responses_request_module._request_is_responses_api(routed_kwargs) or "input" in routed_kwargs:
        _append_responses_visual_context(routed_kwargs, visual_context)
    else:
        _append_chat_visual_context(routed_kwargs, visual_context)
    _trace_module._route_trace(
        "dsh_vision_router_request_rewritten",
        image_count=len(descriptions),
        request=_trace_module._trace_request_summary(routed_kwargs),
    )
    return routed_kwargs


async def retry_with_dsh_vision_router(
    original_function: Any,
    request_kwargs: dict,
    *,
    model_group: Optional[str] = None,
) -> Any:
    routed_kwargs = await dsh_vision_router_request_kwargs(request_kwargs)
    if routed_kwargs is None:
        raise RuntimeError("dsh-vision-router could not extract image references")
    if (
        isinstance(model_group, str)
        and model_group.strip()
        and not (isinstance(routed_kwargs.get("model"), str) and routed_kwargs["model"].strip())
    ):
        routed_kwargs["model"] = model_group
    return await original_function(**routed_kwargs)


__all__ = [
    "dsh_vision_router_request_kwargs",
    "retry_with_dsh_vision_router",
    "should_attempt_dsh_vision_router",
    "_dsh_describe_image",
    "_dsh_local_vision_description",
    "_post_chat_completion",
    "_vision_helper_source",
]
