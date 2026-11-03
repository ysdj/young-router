"""The one step that points an external client at this app's own proxy.

Codex, Claude Code, and Claude Desktop reach the same loopback endpoint with
the same master key, and they differ only in *which document* names it.  The
endpoint resolution, the "is this client already on it" comparison, and the
remembered route the off direction returns to are therefore shared here; each
domain keeps its own writer, because the three clients spell the same
connection differently (a provider row in TOML, an ``env`` block, a
third-party inference profile) and one generic writer would have to know all
three.

The remembered routes and generated model lists live in one app-state file
under the runtime root (``client-routes.json``), beside ``model-contexts.json``
and the WebDAV status: they are this app's own bookkeeping about a client, not
a field of the client's document and not an operator setting.  They have to
survive a restart because the proxy serves *public* model names -- several
saved routes may share one -- so a client switched onto the proxy can no longer
read from its own document which route it left.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._shared import DomainError, local_proxy_endpoint


CLIENT_ROUTE_STATE_FILE_NAME = "client-routes.json"
#: The clients this file remembers a route and a generated model list for.
CLIENT_ROUTE_CLIENTS = ("codex", "claudeCode", "claudeDesktop")


def normalise_route(value: object) -> dict[str, str] | None:
    """Read one ``{model, provider, deployment_id}`` route, rejecting junk.

    Only these three identifiers are kept: a route is named by what it is, so a
    remembered entry can never carry a credential, an endpoint, or a label
    somebody typed somewhere else.
    """

    if not isinstance(value, Mapping):
        return None
    model = value.get("model")
    provider = value.get("provider")
    deployment_id = value.get("deployment_id")
    if not isinstance(model, str) or not model.strip():
        return None
    if not isinstance(provider, str) or not provider.strip():
        return None
    result = {"model": model.strip(), "provider": provider.strip()}
    if isinstance(deployment_id, str) and deployment_id.strip():
        result["deployment_id"] = deployment_id.strip()
    return result


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


def _write_state(runtime_root: object, state: Mapping[str, Any]) -> None:
    path = _state_path(runtime_root)
    if path is None:
        return
    from ..persistence import atomic_write_json

    try:
        atomic_write_json(path, dict(state))
    except Exception:
        # This file is memory, not configuration: failing to store one route
        # must not fail the switch the user just asked for.  The remembered
        # route only decides which way the *next* off action goes.
        pass


def read_route_setting(runtime_root: object, client: str) -> dict[str, str] | None:
    """The saved route this app remembers for one client, or ``None``."""

    entry = _read_state(runtime_root).get(client)
    if not isinstance(entry, Mapping):
        return None
    return normalise_route(entry.get("last_route"))


def store_route_setting(runtime_root: object, client: str, route: object) -> None:
    """Remember (or clear) the route one client left for this app's proxy."""

    if client not in CLIENT_ROUTE_CLIENTS:
        return
    state = _read_state(runtime_root)
    entry = state.get(client)
    remembered = dict(entry) if isinstance(entry, Mapping) else {}
    resolved = normalise_route(route)
    if resolved is None:
        remembered.pop("last_route", None)
    else:
        remembered["last_route"] = resolved
    if remembered:
        state[client] = remembered
    else:
        state.pop(client, None)
    _write_state(runtime_root, state)


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


def same_route(left: Mapping[str, str] | None, right: Mapping[str, str] | None) -> bool:
    """Identity of a saved route, including the deployment it pins."""

    if left is None or right is None:
        return False
    if left.get("model") != right.get("model") or left.get("provider") != right.get("provider"):
        return False
    # A route pinned to one deployment is a different route from the same model
    # on another; an unnamed deployment is the model's own first row.
    return (left.get("deployment_id") or "") == (right.get("deployment_id") or "")


def route_label(route: Mapping[str, str] | None) -> str:
    if route is None:
        return ""
    return f"{route.get('provider', '')} / {route.get('model', '')}".strip(" /")


def endpoint() -> tuple[str, str]:
    """This app's proxy base URL (with ``/v1``) and its master key."""

    return local_proxy_endpoint()


def on_local_api(configured: object, *, versioned: bool = True) -> bool:
    """Whether a client's configured base URL already names this proxy.

    Both spellings address the same gateway: a client that appends
    ``/messages`` itself stores the unversioned root, while one that appends
    the endpoint stores the base with ``/v1``.
    """

    if not isinstance(configured, str) or not configured.strip():
        return False
    try:
        base_url, _ = endpoint()
    except Exception:
        return False
    current = configured.strip().rstrip("/").lower()
    expected = base_url.rstrip("/").lower()
    unversioned = expected.removesuffix("/v1")
    return current in {expected, unversioned} if not versioned else current == expected


def saved_routes(runtime_config_path: object) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The provider document and every saved route it states.

    A route's *saved* identity is the one its own configuration declares, so
    this reads the provider document rather than a client's file: the same
    list backs Codex's designate menu and both Claude targets, and the runtime
    document is handed back because the route's own key lives beside it.
    """

    from .. import codex_config

    try:
        runtime = codex_config.load_yaml(runtime_config_path)
        rows = codex_config.configured_models(runtime)
    except Exception as exc:
        raise DomainError("The saved Young Router models could not be read") from exc
    if not isinstance(runtime, Mapping):
        raise DomainError("The saved Young Router models could not be read")
    return dict(runtime), [dict(row) for row in rows]


def one_saved_route(
    rows: list[dict[str, Any]],
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve exactly one saved route from a request's identifiers."""

    model = str(data.get("model") or "").strip()
    provider = str(data.get("provider") or "").strip()
    deployment_id = str(data.get("deployment_id") or "").strip()
    if not model or not provider:
        raise DomainError("Select one saved Young Router model")
    candidates = [
        row
        for row in rows
        if str(row.get("model") or "").strip() == model
        and str(row.get("provider") or "").strip() == provider
        and (not deployment_id or str(row.get("deployment_id") or "").strip() == deployment_id)
    ]
    if len(candidates) != 1:
        raise DomainError("Select one current Young Router model")
    return candidates[0]


def saved_route_api_key(runtime: object, deployment_id: str) -> str:
    """The route's own key from the runtime model list, when it has one."""

    if not deployment_id or not isinstance(runtime, Mapping):
        return ""
    entries = runtime.get("model_list")
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        info = entry.get("model_info")
        if not isinstance(info, Mapping) or str(info.get("id") or "").strip() != deployment_id:
            continue
        params = entry.get("litellm_params")
        value = params.get("api_key") if isinstance(params, Mapping) else None
        return value.strip() if isinstance(value, str) else ""
    return ""


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
    "anthropic_base_url",
    "endpoint",
    "names_a_claude_model",
    "normalise_route",
    "on_local_api",
    "one_saved_route",
    "read_generated_models",
    "read_route_setting",
    "route_label",
    "same_route",
    "saved_route_api_key",
    "saved_routes",
    "store_generated_models",
    "store_route_setting",
]
