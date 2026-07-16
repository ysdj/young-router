from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit


_KNOWN_API_ENDPOINT_SUFFIXES = (
    ("v1", "chat", "completions"),
    ("v1", "chat", "completion"),
    ("v1", "images", "generations"),
    ("v1", "images", "generation"),
    ("v1", "completions"),
    ("v1", "completion"),
    ("v1", "complete"),
    ("v1", "responses"),
    ("v1", "response"),
    ("v1", "messages"),
    ("v1", "message"),
    ("v1", "models"),
    ("v1", "model"),
    ("v1", "chat"),
    ("v1", "images"),
    ("chat", "completions"),
    ("chat", "completion"),
    ("images", "generations"),
    ("images", "generation"),
    ("completions",),
    ("completion",),
    ("complete",),
    ("responses",),
    ("response",),
    ("messages",),
    ("message",),
    ("models",),
    ("model",),
    ("chat",),
    ("images",),
)


def _split_api_url(value: Any) -> Optional[tuple[Any, list[str]]]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().rstrip("/")
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        if "://" in text:
            return None
        parsed = urlsplit(f"https://{text}")
    if not parsed.scheme or not parsed.netloc:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    return parsed, parts


def _matched_endpoint_suffix(parts: list[str]) -> Optional[tuple[str, ...]]:
    lowered = [part.lower() for part in parts]
    for suffix in _KNOWN_API_ENDPOINT_SUFFIXES:
        if len(lowered) >= len(suffix) and tuple(lowered[-len(suffix) :]) == suffix:
            return suffix
    return None


def _api_root_and_version(value: Any) -> Optional[tuple[Any, list[str], Optional[bool]]]:
    split = _split_api_url(value)
    if split is None:
        return None
    parsed, parts = split
    suffix = _matched_endpoint_suffix(parts)
    if suffix is None:
        return parsed, parts, None
    return parsed, parts[: -len(suffix)], suffix[0] == "v1"


def _url_with_path(parsed: Any, parts: list[str]) -> str:
    path = f"/{'/'.join(parts)}" if parts else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def normalize_configured_api_base(value: Any) -> str:
    split = _split_api_url(value)
    if split is None:
        return str(value or "").strip().rstrip("/")
    parsed, parts = split
    if _matched_endpoint_suffix(parts) is not None:
        # An explicit endpoint records whether its server is versioned. Keep
        # that distinction until the selected protocol maps it to a request.
        return _url_with_path(parsed, parts).rstrip("/")
    if not parts or parts[-1].lower() != "v1":
        parts.append("v1")
    return _url_with_path(parsed, parts).rstrip("/")


def api_base_for_surface(value: Any, surface: Any) -> str:
    root = _api_root_and_version(value)
    if root is None:
        return str(value or "").strip().rstrip("/")
    parsed, root_parts, endpoint_was_versioned = root
    surface_text = str(surface or "").strip().lower()

    if root_parts and root_parts[-1].lower() == "v1":
        versioned_parts = root_parts
    elif endpoint_was_versioned is False:
        versioned_parts = root_parts
    else:
        versioned_parts = [*root_parts, "v1"]

    if surface_text == "anthropic":
        # LiteLLM's Anthropic client appends `/v1/messages` unless that complete
        # endpoint is already present. Supplying the final endpoint prevents a
        # configured `/v1` base from becoming `/v1/v1/messages`.
        anthropic_parts = [*versioned_parts]
        if endpoint_was_versioned is not False and (
            not anthropic_parts or anthropic_parts[-1].lower() != "v1"
        ):
            anthropic_parts.append("v1")
        anthropic_parts.append("messages")
        return _url_with_path(parsed, anthropic_parts).rstrip("/")
    if surface_text in {"openai/chat", "openai/responses"}:
        return _url_with_path(parsed, versioned_parts).rstrip("/")
    return normalize_configured_api_base(value)


def is_unversioned_anthropic_messages_endpoint(value: Any) -> bool:
    split = _split_api_url(value)
    if split is None:
        return False
    _, parts = split
    return bool(parts) and parts[-1].lower() == "messages" and (
        len(parts) == 1 or parts[-2].lower() != "v1"
    )


def apply_surface_api_base(request_kwargs: dict, surface: Any) -> bool:
    litellm_params = request_kwargs.get("litellm_params")
    if not isinstance(litellm_params, dict):
        return False
    configured = litellm_params.get("api_base")
    if not isinstance(configured, str) or not configured.strip():
        return False
    normalized = api_base_for_surface(configured, surface)
    if not normalized or normalized == configured:
        return False
    updated = litellm_params.copy()
    updated["api_base"] = normalized
    request_kwargs["litellm_params"] = updated
    request_kwargs["api_base"] = normalized
    metadata = request_kwargs.get("litellm_metadata")
    if isinstance(metadata, dict):
        updated_metadata = metadata.copy()
        updated_metadata["api_base"] = normalized
        request_kwargs["litellm_metadata"] = updated_metadata
    return True
