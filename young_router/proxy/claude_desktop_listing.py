"""The model list Claude Desktop's own discovery reads.

Claude Desktop's picker is filled from ``GET /v1/models`` when its profile
carries no ``inferenceModels`` list.  The desktop app filters that answer down
to ids it can read as Anthropic models, so a router whose public names are
``deepseek-v4.1-flash`` and ``gpt-6.1-sol`` would leave the picker with the one
Claude-shaped route it happens to serve.  The alias in ``claude_aliases`` is
the answer, but only when the listing actually publishes it: the app's own
discovery request is the one client that must see the alias rows, not the
config file this app used to write.

What identifies that request is the app's own Electron profile, not Claude
Code: every gateway call the desktop app makes for discovery and its connection
test goes through the ``custom3p-probe`` session, whose user agent is Electron's
default (``Claude/<app version> ... Electron/<electron version>``).  Claude
Code's own discovery sends ``claude-code/<version>`` (and ``claude-cli/`` on its
model calls), so the two never blur.  A profile can also name itself with the
explicit ``x-young-router-client: claude-desktop`` header through
``inferenceCustomHeaders``, which is the stable signal when a user agent changes.

The view is an addition, never a replacement: the canonical ``/v1/models``
answer keeps the public names every other client routes by.  A request that is
not the desktop app's own gets exactly what it got before this module existed.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, Final

from ..claude_aliases import claude_desktop_alias, desktop_keeps_model_id

#: A profile may name its client explicitly.  The desktop app sends
#: ``inferenceCustomHeaders`` on the discovery request as well as on turns, so
#: one static header is enough to identify it without a version-dependent user
#: agent.
CLAUDE_DESKTOP_CLIENT_HEADER: Final = "x-young-router-client"
CLAUDE_DESKTOP_CLIENT: Final = "claude-desktop"

#: The paths whose Anthropic-shaped answer this view rewrites.  The single-model
#: lookup is not part of discovery and keeps the canonical id.
_LISTING_PATHS: Final = frozenset({"/v1/models", "/models"})

_VIEW_ACTIVE: ContextVar[bool] = ContextVar(
    "young_router_claude_desktop_listing", default=False
)
_PATCH_ATTR: Final = "_young_router_claude_desktop_listing"
_REGISTERED_ATTR: Final = "_young_router_claude_desktop_listing_registered"


def is_claude_desktop_client(headers: Mapping[str, str]) -> bool:
    """Whether this request is Claude Desktop's own model discovery.

    The explicit header wins.  Otherwise the request must speak the Anthropic
    listing shape and carry Electron's user agent, which is what the app's
    ``custom3p-probe`` session sends.  Claude Code's ``claude-code/`` and
    ``claude-cli/`` agents are excluded by name so a future Electron-based
    client cannot be mistaken for the desktop app by ``Electron/`` alone.
    """

    normalized = {
        str(name).strip().lower(): str(value)
        for name, value in headers.items()
        if isinstance(name, str) and isinstance(value, str)
    }
    if (
        normalized.get(CLAUDE_DESKTOP_CLIENT_HEADER, "").strip().lower()
        == CLAUDE_DESKTOP_CLIENT
    ):
        return True
    if not normalized.get("anthropic-version", "").strip():
        return False
    agent = normalized.get("user-agent", "").strip().lower()
    if "electron/" not in agent:
        return False
    return not agent.startswith(("claude-code/", "claude-cli/"))


def _route_is_image(name: str) -> bool:
    """Whether the public route serves image generation and nothing else.

    The same predicate the generated pickers use: a generator is not a model to
    have a conversation with, so it never appears in the desktop view.  An
    unreadable catalog is not evidence of an image route, exactly as in
    ``model_catalog.route_serves_only_images``.
    """

    try:
        from ..core.model_catalog import route_serves_only_images

        return route_serves_only_images(name)
    except Exception:
        return False


def desktop_model_rows(
    response: Mapping[str, object],
    display_names: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Rewrite one Anthropic-shaped listing for Claude Desktop's discovery.

    A route the app already reads as an Anthropic model keeps the id it was
    listed under, so the app's own display name and controls are untouched.
    Every other route is published under the alias ``claude_aliases`` derives,
    with the public name as ``display_name`` -- the app shows that name and
    derives the full effort ladder from the alias family.  Image generators are
    dropped in both cases.
    """

    names = display_names if isinstance(display_names, Mapping) else {}
    rows: list[dict[str, object]] = []
    for row in response.get("data") or []:
        if not isinstance(row, Mapping):
            continue
        public_id = row.get("id")
        if not isinstance(public_id, str) or not public_id.strip():
            continue
        if _route_is_image(public_id):
            continue
        if desktop_keeps_model_id(public_id):
            rows.append(dict(row))
            continue
        display_name = row.get("display_name") or names.get(public_id) or public_id
        rows.append(
            {
                "type": "model",
                "id": claude_desktop_alias(public_id),
                "display_name": str(display_name),
                "created_at": row.get("created_at"),
                # The app reads this to know a route's context window is 1M.
                # Omitting it made every alias fall back to 200k, which is the
                # wrong number the picker showed for a 1M route.  The app marks
                # such a row as its 1M variant; modelPrefer1mContext keeps that
                # to one row instead of a second standard one.
                "max_input_tokens": row.get("max_input_tokens"),
                "max_tokens": row.get("max_tokens"),
            }
        )
    # The app's generated list was alphabetical by the name the picker shows,
    # and discovery treats the first entry as the default model.  Sorting the
    # view keeps the default route where the file list put it.
    rows.sort(key=lambda row: str(row.get("display_name") or row.get("id") or "").casefold())
    return {
        **dict(response),
        "data": rows,
        "first_id": rows[0]["id"] if rows else None,
        "last_id": rows[-1]["id"] if rows else None,
    }


def install_patch() -> None:
    """Publish the desktop view through LiteLLM's Anthropic listing builder.

    ``proxy_server.model_list`` imports the builder inside the endpoint, so
    replacing the module attribute reaches both the admin-scoped and the
    ordinary listing branches.  The patch only acts while the request
    middleware marked this call as the desktop app's own discovery.
    """

    try:
        from litellm.llms.anthropic import common_utils as anthropic_common
    except Exception:
        return
    original = getattr(anthropic_common, "create_anthropic_model_list_response", None)
    if not callable(original) or getattr(original, _PATCH_ATTR, False):
        return

    def patched(
        models: Any,
        display_names: Mapping[str, str] | None = None,
        listed_ids: Mapping[str, str] | None = None,
    ) -> Any:
        response = original(models, display_names or {}, listed_ids or {})
        if not _VIEW_ACTIVE.get() or not isinstance(response, Mapping):
            return response
        return desktop_model_rows(response, display_names)

    setattr(patched, _PATCH_ATTR, True)
    setattr(patched, "_original_create_anthropic_model_list_response", original)
    anthropic_common.create_anthropic_model_list_response = patched


class _DesktopListingMiddleware:
    """Mark the desktop app's own listing request for the response builder."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "").rstrip("/") or "/"
        if path not in _LISTING_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
            if isinstance(key, bytes) and isinstance(value, bytes)
        }
        token = _VIEW_ACTIVE.set(is_claude_desktop_client(headers))
        try:
            await self.app(scope, receive, send)
        finally:
            _VIEW_ACTIVE.reset(token)


def register(app: Any) -> None:
    """Install the request marker on the proxy app (one app, one marker)."""

    if getattr(app, _REGISTERED_ATTR, False):
        return
    app.add_middleware(_DesktopListingMiddleware)
    setattr(app, _REGISTERED_ATTR, True)


__all__ = [
    "CLAUDE_DESKTOP_CLIENT",
    "CLAUDE_DESKTOP_CLIENT_HEADER",
    "desktop_model_rows",
    "install_patch",
    "is_claude_desktop_client",
    "register",
]
