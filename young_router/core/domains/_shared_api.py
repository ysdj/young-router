"""Client API endpoints, remembered direct connections, and generated lists.

The state file is this app's own private bookkeeping (mode 0600, never part of
a Core snapshot and never synced): the generated model list a client owns, and
the direct connection a client left when it moved onto this app's proxy.  The
remembered connection holds the client's own API key because leaving the proxy
has to put that client back on the very endpoint and credential it used before;
the key is read only through Core's secret capability, exactly like the copy the
client document already holds.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..security import SENSITIVE_QUERY_MARKERS
from ._shared import DomainError, local_proxy_endpoint


CLIENT_ROUTE_STATE_FILE_NAME = "client-routes.json"
CLIENT_ROUTE_CLIENTS = ("claudeCode", "claudeDesktop")
DIRECT_CONNECTION_CLIENTS = ("codex", "claudeCode", "claudeDesktop")
DIRECT_CONNECTION_KEY = "direct_connection"


def direct_api_url(value: object, *, optional: bool = False) -> str:
    """Validate a URL from the direct connection form, without echoing it."""

    url = value.strip() if isinstance(value, str) else ""
    if optional and not url:
        return ""
    try:
        parsed = urlsplit(url)
        parsed.port  # reject invalid ports before saving the client config
        if (parsed.scheme in {"http", "https"} and parsed.hostname
                and parsed.username is None and not parsed.fragment
                and not any(char.isspace() for char in url)):
            return url
    except ValueError:
        pass
    raise DomainError("Enter an absolute HTTP or HTTPS URL without credentials or a fragment")


def public_direct_api_url(value: object) -> str:
    """Editable URL fields can be prefilled only from non-secret addresses."""
    try:
        url = direct_api_url(value, optional=True)
    except DomainError:
        return ""
    query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    return "" if any(marker in key.lower() for key, _ in query for marker in SENSITIVE_QUERY_MARKERS) else url


def _state_path(runtime_root: object) -> Path | None:
    if runtime_root is None:
        return None
    return Path(runtime_root).expanduser() / CLIENT_ROUTE_STATE_FILE_NAME


def _read_state(runtime_root: object) -> dict[str, Any]:
    path = _state_path(runtime_root)
    if path is None:
        return {}
    from ..persistence import PersistenceError, read_json

    try:
        value = read_json(path, default={})
    except PersistenceError:
        return {}
    return value if isinstance(value, Mapping) else {}


def _write_state(runtime_root: object, state: Mapping[str, Any], *, strict: bool = False) -> None:
    path = _state_path(runtime_root)
    if path is None:
        if strict:
            raise DomainError("The direct API connection could not be saved")
        return
    from ..persistence import atomic_write_json

    try:
        atomic_write_json(path, dict(state))
    except Exception:
        # A model list already saved to the client remains usable.  A direct
        # connection is the off position of a switch and the only copy of the
        # credential it will write, so its caller is told instead of leaving a
        # switch that cannot return.
        if strict:
            raise DomainError("The direct API connection could not be saved") from None


def _direct_text(value: object, *, limit: int = 4096) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > limit or "\x00" in text or "\n" in text or "\r" in text:
        return ""
    return text


def normalise_direct_connection(value: object) -> dict[str, Any] | None:
    """Read one remembered direct connection, rejecting junk.

    The endpoint is the identity of the connection, so a record without a valid
    one is nothing this app can restore -- except for the built-in OpenAI
    provider and a client on its own official endpoint, where "no address" is
    the connection.  The key is kept verbatim (single line only) because it is
    the credential the client's document held; every other field is a short
    plain-text identifier.
    """

    if not isinstance(value, Mapping):
        return None
    provider = _direct_text(value.get("provider"))
    try:
        base_url = direct_api_url(
            value.get("base_url"), optional=provider in {"openai", "official"}
        )
    except DomainError:
        return None
    result: dict[str, Any] = {"base_url": base_url}
    if provider:
        result["provider"] = provider
    for field in (
        "model",
        "model_catalog_url",
        "auth_mode",
        "env_key",
        "env_model",
        "web_search",
    ):
        text = _direct_text(value.get(field))
        if text:
            result[field] = text
    key = value.get("api_key")
    if (
        isinstance(key, str)
        and key
        and len(key) <= 16384
        and "\x00" not in key
        and "\n" not in key
        and "\r" not in key
    ):
        result["api_key"] = key
    if isinstance(value.get("supports_standalone_web_search"), bool):
        result["supports_standalone_web_search"] = value["supports_standalone_web_search"]
    return result


def read_direct_connection(runtime_root: object, client: str) -> dict[str, Any] | None:
    """The direct connection this app last saw one client on, if any."""

    if client not in DIRECT_CONNECTION_CLIENTS:
        return None
    entry = _read_state(runtime_root).get(client)
    if not isinstance(entry, Mapping):
        return None
    return normalise_direct_connection(entry.get(DIRECT_CONNECTION_KEY))


def store_direct_connection(runtime_root: object, client: str, connection: object | None) -> None:
    """Remember (or clear) the connection a client leaves for this app's proxy."""

    if client not in DIRECT_CONNECTION_CLIENTS:
        return
    state = _read_state(runtime_root)
    entry = state.get(client)
    remembered = dict(entry) if isinstance(entry, Mapping) else {}
    resolved = normalise_direct_connection(connection)
    if resolved is None:
        remembered.pop(DIRECT_CONNECTION_KEY, None)
    else:
        remembered[DIRECT_CONNECTION_KEY] = resolved
    if remembered:
        state[client] = remembered
    else:
        state.pop(client, None)
    _write_state(runtime_root, state, strict=True)


def read_generated_models(runtime_root: object, client: str) -> list[str]:
    """The public names this app last generated one client's model list from."""

    entry = _read_state(runtime_root).get(client)
    models = entry.get("generated_models") if isinstance(entry, Mapping) else None
    if not isinstance(models, list):
        return []
    return [item.strip() for item in models if isinstance(item, str) and item.strip()]


def store_generated_models(runtime_root: object, client: str, names: object) -> None:
    if client not in CLIENT_ROUTE_CLIENTS:
        return
    state = _read_state(runtime_root)
    entry = state.get(client)
    remembered = dict(entry) if isinstance(entry, Mapping) else {}
    resolved = [item.strip() for item in names if isinstance(item, str) and item.strip()] if isinstance(names, (list, tuple)) else []
    if resolved:
        remembered["generated_models"] = resolved
    else:
        remembered.pop("generated_models", None)
    if remembered:
        state[client] = remembered
    else:
        state.pop(client, None)
    _write_state(runtime_root, state)


def endpoint(config_path: Path | None = None) -> tuple[str, str]:
    """This app's proxy base URL (with ``/v1``) and its master key.

    ``config_path`` is the runtime document the calling domain manages, which is
    where this installation's master key is; a caller with none names nothing
    and the default locations answer.
    """

    return local_proxy_endpoint(config_path)


#: Brand tokens Claude Desktop refuses inside an ``inferenceModels`` entry,
#: even when the id starts with ``claude-``.  Verified one by one against the
#: app: ids carrying any of these are silently dropped from the picker, while
#: the same id with a neutral word in their place is kept.  It is the app's own
#: competitor list, so this is a denylist of what this router can be pointed
#: at -- an id it does not mention is assumed acceptable, which is the safe
#: direction: a name that is kept but unrecognised still shows in the picker.
CLAUDE_DESKTOP_REFUSED_TOKENS = frozenset(
    {
        "openai",
        "gpt",
        "chatgpt",
        "deepseek",
        "gemini",
        "bard",
        "kimi",
        "moonshot",
        "glm",
        "zhipu",
        "qwen",
        "dashscope",
        "grok",
        "xai",
        "llama",
        "mistral",
        "doubao",
        "bytedance",
        "yi",
        "minimax",
        "ernie",
        "baidu",
    }
)


def desktop_accepts_model_name(name: object) -> bool:
    """Whether Claude Desktop keeps this id in its third-party model list.

    Two conditions, both observed on the app itself: the id has to read as an
    Anthropic model (a bare ``grok-4.7`` is removed with a warning), and it may
    not carry a competitor's brand even behind the ``claude-`` prefix
    (``claude-gpt-test`` was removed while ``claude-fast-v3`` was kept).

    Verified additions that survive: ``claude-fast-v3``, ``claude-think-max``,
    ``claude-3-5-sonnet``, ``anthropic/claude-x``.  Verified removals:
    ``claude-gpt-test``, ``claude-deepseek-test``, ``claude-gemini-test``,
    ``claude-kimi-x``, ``claude-glm-5``, ``claude-openai-x``,
    ``claude-qwen-x``, ``claude-grok-x``, ``claude-llama-x``,
    ``claude-mistral-x``, ``claude-doubao-x``, ``claude-yi-x``.
    """

    if not names_a_claude_model(name):
        return False
    assert isinstance(name, str)
    tokens = {token for token in re.split(r"[^a-z0-9]+", name.strip().lower()) if token}
    return not (tokens & CLAUDE_DESKTOP_REFUSED_TOKENS)


def names_a_claude_model(name: object) -> bool:
    """Whether Claude Desktop keeps this model ID in its third-party picker.

    Claude Desktop on 3P builds its picker from the models it can map onto an
    Anthropic model, and it *deletes* every other ID from the configuration on
    launch -- with a warning that names the removal.  A gateway this app fronts
    serves whatever public names the user configured, so writing all of them
    into ``inferenceModels`` left Desktop with a stripped list and the user with
    a "removed from the list" warning per route.

    The rule is the one this proxy already uses to decide a route speaks the
    Messages wire, so both sides agree on what "names a Claude model" means;
    the caller only writes the names this accepts.
    """

    from ...config.schema import infer_upstream_fallback_surface

    if not isinstance(name, str) or not name.strip():
        return False
    return infer_upstream_fallback_surface(name) == "anthropic"


def anthropic_base_url(base_url: str) -> str:
    """The Messages root for a client that appends ``/v1/messages`` itself."""

    return base_url.rstrip("/").removesuffix("/v1")


__all__ = [
    "CLAUDE_DESKTOP_REFUSED_TOKENS",
    "CLIENT_ROUTE_CLIENTS",
    "CLIENT_ROUTE_STATE_FILE_NAME",
    "DIRECT_CONNECTION_CLIENTS",
    "DIRECT_CONNECTION_KEY",
    "anthropic_base_url",
    "direct_api_url",
    "endpoint",
    "names_a_claude_model",
    "normalise_direct_connection",
    "public_direct_api_url",
    "read_direct_connection",
    "read_generated_models",
    "store_direct_connection",
    "store_generated_models",
]
