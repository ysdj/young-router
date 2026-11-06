"""What *this* host serves, as distinct from what the shared document says.

One document is edited by whichever machine you are at and reaches the others
over WebDAV.  Most of it means the same thing everywhere -- the routes, the
credits, the ordering -- but a few facts belong to one machine:

* A provider whose endpoint is a literal loopback address lives on the machine
  that wrote it.  No other host can open that port, so the route stays out of
  what they serve.  An ``os.environ/NAME`` reference is the opposite case and
  travels: the name is resolved by the launcher on the host that starts the
  worker (see ``young_router.adapters.managed``).  Dropping those was why the
  WorkBuddy and Our Free Model lanes never arrived at a gateway at all.
* The proxy key on a machine other people's clients point at is that machine's
  own fact.  Letting a published key win silently retires every client already
  pointed at it.
* A callback is code.  Whichever machine edited the document, the callback list
  a host runs has to be one this install can import, and it has to include the
  router's own hook: image-history bounding, capacity classification, the
  protocol bridges and stream recovery are methods on that hook, so an empty
  list is not "no token saver" but plain LiteLLM behind a health check that
  still answers 200.

This used to live in a shell script in the deployment repository, which meant a
host's behaviour was described in two places and only one of them was in the
app.  It is a staging-time projection: the editor document keeps everything
(including the parked rows and the disabled providers), and what the proxy
loads is this function's answer.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

#: Set on a machine that serves other people's clients.  Absent on a desktop,
#: which serves itself and wants the document exactly as edited.
PROFILE_ENV = "YOUNG_ROUTER_HOST_PROFILE"
GATEWAY = "gateway"
#: A host may state the key its own clients present.  A file rather than an
#: environment value: it is a credential, and a unit that carried it would be
#: readable by anyone allowed to run ``systemctl show``.
PROXY_KEY_ENV = "YOUNG_ROUTER_HOST_PROXY_KEY_FILE"

#: The router's own hook is the data plane, not a desktop convenience.
HOOK_CALLBACK = "young_router.callbacks.image_generation_routing_hook"


def active() -> bool:
    """Whether this process serves a host rather than the machine's own desktop."""

    return os.environ.get(PROFILE_ENV, "").strip().lower() == GATEWAY


def stated_key() -> str:
    """The key this host states for itself, or ``""`` when it states none."""

    configured = os.environ.get(PROXY_KEY_ENV, "").strip()
    if not configured:
        return ""
    try:
        text = Path(configured).expanduser().read_text(encoding="utf-8")
    except OSError:
        return ""
    stripped = text.strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def reachable(base: object) -> bool:
    """Whether a host other than the machine that wrote this route can send it."""

    if not isinstance(base, str) or not base:
        return False
    if base.startswith("os.environ/"):
        # The reference travels; the launcher on the host resolves it from the
        # worker it started there.
        return True
    return "127.0.0.1" not in base and "localhost" not in base and "[::1]" not in base


def document(local_doc: Mapping[str, Any], master_key: str) -> dict[str, Any]:
    """The document this host serves, derived from the one it was handed.

    Everything not named in the module docstring is passed through unchanged:
    the order is the user's, it already reflects the app's own measurements, and
    a table of numbers kept on the host would silently stop matching the moment
    the desktop changed it.
    """

    providers = {
        name: spec
        for name, spec in (local_doc.get("providers") or {}).items()
        if (spec or {}).get("enabled") is not False and reachable((spec or {}).get("api_base"))
    }

    model_list: list[dict[str, Any]] = []
    for entry in local_doc.get("model_list") or []:
        info = entry.get("model_info") or {}
        if info.get("provider") not in providers:
            continue
        if info.get("x-young-router-model-enabled") is False:
            continue
        params = dict(entry.get("litellm_params") or {})
        # LiteLLM's anthropic provider appends /v1/messages itself, so a base
        # that already ends in /v1 becomes /v1/v1/messages and 404s.
        api_base = str(params.get("api_base") or "")
        if str(params.get("model") or "").startswith("anthropic/"):
            params["api_base"] = api_base.rstrip("/").removesuffix("/v1")
        model_list.append(
            {"model_name": entry["model_name"], "litellm_params": params, "model_info": info}
        )

    routed = {entry["model_name"] for entry in model_list}
    settings = dict(local_doc.get("litellm_settings") or {})
    settings["public_model_groups"] = [
        name for name in settings.get("public_model_groups") or [] if name in routed
    ]
    # Always published: see HOOK_CALLBACK.  Anything else in the document is a
    # callback this host must not run -- injected from another machine's
    # install, or a desktop-side one that would alter prompts instead of
    # adding a capability.
    settings["callbacks"] = [HOOK_CALLBACK]

    general = dict(local_doc.get("general_settings") or {})
    if master_key:
        general["master_key"] = master_key
    general["ui"] = False
    general["dangerously_permit_weak_or_unset_master_key"] = False

    router = dict(local_doc.get("router_settings") or {})
    router.setdefault("disable_cooldowns", True)

    return {
        "providers": providers,
        "model_list": model_list,
        "litellm_settings": settings,
        "general_settings": general,
        "router_settings": router,
    }


__all__ = [
    "GATEWAY",
    "HOOK_CALLBACK",
    "PROFILE_ENV",
    "PROXY_KEY_ENV",
    "active",
    "document",
    "reachable",
    "stated_key",
]
