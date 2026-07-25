"""Codex-only default priority tier handling for native Responses requests.

Codex Desktop can omit ``service_tier`` after it has normalized its own Fast
setting.  This module deliberately treats the *incoming HTTP body* as the
authority for whether a client explicitly selected a tier.  It never changes
ordinary LiteLLM traffic or a request that actually supplied that field.
"""

from __future__ import annotations

import os
from pathlib import Path
import tomllib
from typing import Any, Optional
from urllib.parse import urlparse


_CODEX_FAST_DEFAULT_METADATA_KEY = "codex_fast_default_service_tier"
_CODEX_FAST_DEFAULT_TIER = "priority"

# The config is small, but each proxy worker may serve many requests.  A stat
# signature makes normal requests cheap while an Apply atomic-replace (or an
# ordinary edit) is visible to the next request without a service restart.
_CODEX_CONFIG_CACHE: dict[str, tuple[Optional[tuple[int, int, int, int]], tuple[bool, Optional[str]]]] = {}


def _codex_config_path() -> Path:
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    home = Path(configured_home).expanduser() if configured_home else Path("~/.codex").expanduser()
    return home / "config.toml"


def _config_signature(path: Path) -> Optional[tuple[int, int, int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _read_codex_fast_config() -> tuple[bool, Optional[str]]:
    """Return whether the effective Codex config explicitly enables Fast.

    A malformed or unreadable config is fail-closed.  This is intentional: the
    shim must never turn a broken/unapplied draft into a global priority tier.
    """

    path = _codex_config_path()
    cache_key = str(path)
    signature = _config_signature(path)
    cached = _CODEX_CONFIG_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    enabled = False
    configured_tier: Optional[str] = None
    if signature is not None:
        try:
            loaded = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            tier = loaded.get("service_tier")
            normalized_tier = tier.strip().lower() if isinstance(tier, str) else ""
            features = loaded.get("features")
            fast_mode = features.get("fast_mode") if isinstance(features, dict) else None
            if normalized_tier in {"fast", "priority"} and fast_mode is True:
                enabled = True
                configured_tier = normalized_tier

    result = (enabled, configured_tier)
    _CODEX_CONFIG_CACHE[cache_key] = (signature, result)
    return result


def _reset_codex_fast_config_cache_for_tests() -> None:
    """Reset the mtime cache for isolated tests."""

    _CODEX_CONFIG_CACHE.clear()


def _request_container_dicts(request_kwargs: Optional[dict]) -> list[dict]:
    if not isinstance(request_kwargs, dict):
        return []
    containers = [request_kwargs]
    for key in ("litellm_params", "litellm_metadata", "metadata", "client_metadata"):
        value = request_kwargs.get(key)
        if isinstance(value, dict):
            containers.append(value)
    return containers


def _incoming_proxy_requests(request_kwargs: Optional[dict]) -> list[dict]:
    proxy_requests: list[dict] = []
    for container in _request_container_dicts(request_kwargs):
        proxy_request = container.get("proxy_server_request")
        if isinstance(proxy_request, dict):
            proxy_requests.append(proxy_request)
    return proxy_requests


def _header_value(headers: Any, name: str) -> Optional[str]:
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() != name.lower():
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
    elif isinstance(headers, list):
        for value in headers:
            if not isinstance(value, (tuple, list)) or len(value) != 2:
                continue
            key, item = value
            if str(key).lower() == name.lower() and isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _request_is_native_v1_responses(request_kwargs: Optional[dict]) -> bool:
    for proxy_request in _incoming_proxy_requests(request_kwargs):
        method = proxy_request.get("method")
        if not isinstance(method, str) or method.strip().upper() != "POST":
            continue
        for key in ("url", "path", "route", "endpoint"):
            value = proxy_request.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            path = urlparse(value).path.rstrip("/").lower()
            if path.endswith("/v1/responses"):
                return True
    return False


def _metadata_has_codex_evidence(metadata: dict) -> bool:
    for key, value in metadata.items():
        if not isinstance(value, str) or not value.strip():
            continue
        key_name = str(key).lower()
        if key_name.startswith("x-codex-") or key_name == "x-openai-internal-codex-responses-lite":
            return True
    return False


def _request_has_reliable_codex_evidence(request_kwargs: Optional[dict]) -> bool:
    """Require a Codex-specific signal, not merely a Responses-shaped body."""

    for proxy_request in _incoming_proxy_requests(request_kwargs):
        headers = proxy_request.get("headers")
        for name in (
            "X-Codex-Turn-Metadata",
            "X-Codex-Window-Id",
            "X-Codex-Beta-Features",
            "X-Codex-Installation-Id",
            "X-OpenAI-Internal-Codex-Responses-Lite",
        ):
            if _header_value(headers, name):
                return True
        originator = _header_value(headers, "Originator")
        user_agent = _header_value(headers, "User-Agent")
        if (
            isinstance(originator, str)
            and "codex" in originator.lower()
            and isinstance(user_agent, str)
            and "codex" in user_agent.lower()
        ):
            return True

    return any(_metadata_has_codex_evidence(container) for container in _request_container_dicts(request_kwargs))


def _request_explicitly_supplies_service_tier(request_kwargs: Optional[dict]) -> bool:
    """Use the original body when present so normalized defaults do not win.

    LiteLLM can materialize ``service_tier=None`` or ``standard`` in internal
    kwargs after parsing.  If the captured HTTP body exists, only a key in that
    body (or in explicit ``extra_body``) counts as a user-selected tier.
    """

    request_kwargs = request_kwargs if isinstance(request_kwargs, dict) else {}
    saw_body = False
    for proxy_request in _incoming_proxy_requests(request_kwargs):
        body = proxy_request.get("body")
        if not isinstance(body, dict):
            continue
        saw_body = True
        if "service_tier" in body:
            return True

    extra_body = request_kwargs.get("extra_body")
    if isinstance(extra_body, dict) and "service_tier" in extra_body:
        return True

    # Direct calls and unit-level router paths do not always retain a captured
    # HTTP body.  In those paths, a top-level field is the best available
    # representation of an explicit caller choice.
    if not saw_body and "service_tier" in request_kwargs:
        return True
    return False


def _codex_fast_default_was_injected(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    metadata = request_kwargs.get("litellm_metadata")
    return isinstance(metadata, dict) and metadata.get(_CODEX_FAST_DEFAULT_METADATA_KEY) == _CODEX_FAST_DEFAULT_TIER


def _codex_fast_default_service_tier(request_kwargs: Optional[dict]) -> Optional[str]:
    if not _codex_fast_default_was_injected(request_kwargs):
        return None
    return _CODEX_FAST_DEFAULT_TIER


def _request_service_tier(request_kwargs: Optional[dict]) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    value = request_kwargs.get("service_tier")
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _with_codex_fast_default_service_tier(request_kwargs: dict) -> Optional[dict]:
    """Inject native Responses ``priority`` only for enabled Codex Fast calls."""

    if not isinstance(request_kwargs, dict):
        return None
    if _codex_fast_default_was_injected(request_kwargs):
        return None
    if not _request_is_native_v1_responses(request_kwargs):
        return None
    if not _request_has_reliable_codex_evidence(request_kwargs):
        return None
    if _request_explicitly_supplies_service_tier(request_kwargs):
        return None

    enabled, _configured_tier = _read_codex_fast_config()
    if not enabled:
        return None

    modified_kwargs = request_kwargs.copy()
    modified_kwargs["service_tier"] = _CODEX_FAST_DEFAULT_TIER
    metadata = request_kwargs.get("litellm_metadata")
    updated_metadata = metadata.copy() if isinstance(metadata, dict) else {}
    updated_metadata[_CODEX_FAST_DEFAULT_METADATA_KEY] = _CODEX_FAST_DEFAULT_TIER
    modified_kwargs["litellm_metadata"] = updated_metadata
    return modified_kwargs


def _response_service_tier(response: Any) -> Optional[str]:
    """Return an upstream-reported tier when the Responses payload exposes it."""

    candidates: list[Any] = [response]
    if isinstance(response, dict) and isinstance(response.get("response"), dict):
        candidates.append(response["response"])
    for candidate in candidates:
        value = candidate.get("service_tier") if isinstance(candidate, dict) else getattr(candidate, "service_tier", None)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _response_indicates_failure(response: Any) -> bool:
    """Recognize terminal Responses failures without exposing provider payloads."""

    if isinstance(response, dict):
        payload = response
    else:
        dump = getattr(response, "model_dump", None)
        if callable(dump):
            try:
                payload = dump(mode="json", exclude_none=True)
            except Exception:
                payload = {}
        else:
            payload = {}
    if not isinstance(payload, dict):
        return False
    event_type = payload.get("type")
    if isinstance(event_type, str) and event_type.lower() in {
        "response.failed",
        "response.incomplete",
        "error",
    }:
        return True
    nested_response = payload.get("response")
    if not isinstance(nested_response, dict):
        nested_response = payload
    status = nested_response.get("status")
    return isinstance(status, str) and status.strip().lower() in {
        "failed",
        "incomplete",
        "cancelled",
    }
