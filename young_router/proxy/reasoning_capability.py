"""Tell LiteLLM which routes of ours can reason and stream, before any turn runs.

LiteLLM ships a static cost map of the models it knows.  A relay's own model id
is absent from it, and an absent entry means "this model has no reasoning": the
Responses-to-Chat bridge then refuses ``reasoning.effort``, and with
``drop_params`` enabled it drops the whole ``reasoning`` object -- the client
asked for a visible summary and got an empty block for the whole turn.  Every
route this app serves is reachable through this proxy, so its upstream name is
registered here first.

Kept out of the gateway module so the rule can be tested without LiteLLM: the
import happens inside the call, not at module scope.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping


def runtime_config_path() -> str:
    """The config this worker was started with, the way the launcher states it."""

    raw = os.environ.get("WORKER_CONFIG", "").strip()
    if raw:
        try:
            value = json.loads(raw).get("config")
        except ValueError:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    for name in ("LITELLM_RUNTIME_CONFIG", "LITELLM_CONFIG_FILE"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def register(config_path: str | None = None) -> int:
    """Register every configured route as reasoning-capable; return how many."""

    path = config_path or runtime_config_path()
    if not path:
        return 0
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except (OSError, ValueError):
        return 0
    if not isinstance(document, Mapping):
        return 0
    deployments = document.get("model_list")
    if not isinstance(deployments, list):
        return 0

    import litellm

    cost = getattr(litellm, "model_cost", None)
    if not isinstance(cost, dict):
        return 0

    registered = 0
    for deployment in deployments:
        if not isinstance(deployment, Mapping):
            continue
        params = deployment.get("litellm_params")
        params = params if isinstance(params, Mapping) else {}
        model = str(params.get("model") or "").strip()
        if not model:
            continue
        # LiteLLM keys the map by the bare model id; a provider prefix is the
        # route's own spelling and never appears in the table.
        name = model.split("/", 1)[-1] if "/" in model else model
        adapter = model.split("/", 1)[0] if "/" in model else "openai"
        # An images route never takes the Responses streaming path, so leave its
        # entry's routing fields untouched.
        is_image = "image" in name.casefold()
        entry = cost.get(name)
        if isinstance(entry, Mapping) and entry.get("supports_reasoning") is True:
            if is_image or entry.get("litellm_provider") in (None, adapter):
                continue
        target: dict[str, Any] = dict(entry) if isinstance(entry, Mapping) else {}
        target["supports_reasoning"] = True
        if not is_image:
            # LiteLLM's OpenAI Responses adapter fakes a stream for a model it
            # cannot find in this map.  A faked stream calls the provider
            # without ``stream`` and only then slices the completed body into
            # events: the reader waits through the whole thinking phase and
            # receives every summary delta at once.  These routes are
            # OpenAI-compatible relays this proxy calls with the adapter's
            # provider, so state both facts here and the provider's own SSE
            # stays the wire.
            target["supports_native_streaming"] = True
            target["litellm_provider"] = adapter
        cost[name] = target
        registered += 1
    return registered


__all__ = ["register", "runtime_config_path"]
