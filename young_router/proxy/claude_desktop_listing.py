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
not one of the Claude apps' own discovery gets exactly what it got before this
module existed.

One thing the Claude apps share is that a model picker is a list of
conversations: an image or video generator holds none, so both Claude views
(the desktop alias view and Claude Code's own canonical discovery) drop those
rows.  The filter is applied before LiteLLM builds either shape, from the
listing's own ``mode`` first and the shared catalog/registry answer second.
It is presentation only: the router keeps the route, so a request that names a
generator explicitly still reaches it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any, Final

from .. import claude_picker
from ..claude_aliases import CONTEXT_SUFFIX, context_suffix_free, desktop_keeps_model_id

#: A profile may name its client explicitly.  The desktop app sends
#: ``inferenceCustomHeaders`` on the discovery request as well as on turns, so
#: one static header is enough to identify it without a version-dependent user
#: agent.
CLAUDE_DESKTOP_CLIENT_HEADER: Final = "x-young-router-client"
CLAUDE_DESKTOP_CLIENT: Final = "claude-desktop"

#: Claude Code's discovery sends these agents, and a gateway may name the view
#: explicitly with ``x-gateway-client: claude-code`` (the spelling LiteLLM's own
#: Claude Code view reads).  Both are the client asking for a Claude picker, not
#: for the router's canonical OpenAI-shaped listing.
_CLAUDE_CODE_AGENT_PREFIXES: Final = ("claude-code/", "claude-cli/")
CLAUDE_CODE_CLIENT_HEADER: Final = "x-gateway-client"
CLAUDE_CODE_CLIENT: Final = "claude-code"

#: The ceiling every window this view states is clamped to.  A route at or above
#: it is published as exactly 1M, because the client's ``[1m]`` spelling is how
#: it says "the large window" and its per-model setting accepts 100k..1M; a
#: second spelling of the same ceiling (1,048,576) would only move the number the
#: picker reports.
_ONE_MILLION: Final = 1_000_000
#: Claude Code's selector endpoint has one config per client surface. The
#: standalone/remote CLI uses ``cc``/``ccr``; Claude Desktop's bundled CLI uses
#: ``ccd``.
_CLAUDE_CODE_SELECTOR_SURFACES: Final = frozenset({"cc", "ccd", "ccr"})

#: The paths whose Anthropic-shaped answer this view rewrites.  The single-model
#: lookup is not part of discovery and keeps the canonical id.
_LISTING_PATHS: Final = frozenset({"/v1/models", "/models"})

_VIEW_ACTIVE: ContextVar[bool] = ContextVar(
    "young_router_claude_desktop_listing", default=False
)
_GENERATION_FILTER_ACTIVE: ContextVar[bool] = ContextVar(
    "young_router_claude_generation_filter", default=False
)
_CODE_VIEW_ACTIVE: ContextVar[bool] = ContextVar(
    "young_router_claude_code_listing_view", default=False
)
_PATCH_ATTR: Final = "_young_router_claude_desktop_listing"
_REGISTERED_ATTR: Final = "_young_router_claude_desktop_listing_registered"
_SELECTOR_REGISTERED_ATTR: Final = "_young_router_claude_code_selector_registered"


def _normalized_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The request headers keyed by their lower-case names."""

    return {
        str(name).strip().lower(): str(value)
        for name, value in headers.items()
        if isinstance(name, str) and isinstance(value, str)
    }


def is_claude_desktop_client(headers: Mapping[str, str]) -> bool:
    """Whether this request is Claude Desktop's own model discovery.

    The explicit header wins.  Otherwise the request must speak the Anthropic
    listing shape and carry Electron's user agent, which is what the app's
    ``custom3p-probe`` session sends.  Claude Code's ``claude-code/`` and
    ``claude-cli/`` agents are excluded by name so a future Electron-based
    client cannot be mistaken for the desktop app by ``Electron/`` alone.
    """

    normalized = _normalized_headers(headers)
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
    return not agent.startswith(_CLAUDE_CODE_AGENT_PREFIXES)


def is_claude_code_client(headers: Mapping[str, str]) -> bool:
    """Whether this request is Claude Code's own model discovery.

    Claude Code sends ``claude-code/<version>`` for discovery and
    ``claude-cli/<version>`` on its model calls; either is the Claude picker.
    The explicit gateway-client header is the stable spelling a proxy may send
    on the client's behalf.
    """

    normalized = _normalized_headers(headers)
    if (
        normalized.get(CLAUDE_CODE_CLIENT_HEADER, "").strip().lower()
        == CLAUDE_CODE_CLIENT
    ):
        return True
    agent = normalized.get("user-agent", "").strip().lower()
    return agent.startswith(_CLAUDE_CODE_AGENT_PREFIXES)


def is_claude_listing_client(headers: Mapping[str, str]) -> bool:
    """Whether this request is one of the Claude apps' model discovery."""

    return is_claude_desktop_client(headers) or is_claude_code_client(headers)


def _route_is_generation(name: str) -> bool:
    """Whether the public route serves image or video generation and nothing else.

    The same predicate the generated pickers use: a generator is not a model to
    have a conversation with, so it never appears in a Claude view.  An
    unreadable catalog is not evidence of a generator, exactly as in
    ``model_catalog.route_serves_only_generation``.
    """

    try:
        from ..core.model_catalog import route_serves_only_generation

        return route_serves_only_generation(name)
    except Exception:
        return False


def _listing_row_serves_generation(row: object) -> bool:
    """Whether one canonical listing row is a generator the Claude picker drops.

    The row's own ``mode`` is the listing's statement and needs no catalog read;
    the registry is the fallback for a route whose upstream name no installed
    table knows.
    """

    if not isinstance(row, Mapping):
        return False
    try:
        from ..core.model_catalog import listing_mode_serves_generation

        if listing_mode_serves_generation(row.get("mode")):
            return True
    except Exception:
        pass
    model_id = row.get("id")
    return isinstance(model_id, str) and _route_is_generation(model_id)


def _public_context_window(name: str) -> int | None:
    """The window this app publishes for one route.

    ``ModelContextRegistry.record_for`` is the single resolution every other
    surface already reads (the pane's published window, the managed Codex
    catalog), so a discovery row cannot state a second, larger reading of the
    same route.  An unreadable registry or cache is no observation, and the row
    then states nothing.
    """

    try:
        from ..core.model_catalog import _image_filter_registry

        return int(_image_filter_registry().record_for(name).context_window)
    except Exception:
        return None


def _published_context(
    public_id: str,
    public_window: Callable[[str], int | None],
) -> object:
    """The ``max_input_tokens`` one discovery row states.

    The number is the route's *published* window -- the one this app shows in
    its own pane and writes into the managed Codex catalog (``record_for``),
    resolved once.  Never the number its upstream advertises in the listing: an
    advertised 1,048,576 turned routes into 1M rows whose only picker entry
    answered ``Invalid model name``, because the row named an id that did not
    resolve.

    There is one window per route in this app, and a client surface states it
    rather than judging it.  At or above 1M the statement is exactly 1M: the
    client's own 1M spelling means "this route has the large window", every
    surface here clamps to the same ceiling (the per-model setting accepts
    100k..1M), and a number like 1,048,576 would only be a second way to say it.
    Below 1M the number is stated as it is, which is what the picker reports for
    a 500k or 272k route.  A window this app cannot resolve, or a generator's
    ``0``, states nothing.

    Both row kinds state this window: ``_kept_row`` states it for an id the
    client keeps as written, and keeps the number the canonical row gave only
    when this resolution has no answer.
    """

    try:
        window = public_window(public_id)
        if not isinstance(window, int) or isinstance(window, bool):
            return None
        # A generator resolves ``0``: its catalog states no window because it
        # holds no conversation.  Stating ``0`` here would be a limit Claude
        # Desktop reads as "this model cannot be given a prompt"; the row
        # states nothing instead, exactly as for an unresolved route.
        if window <= 0:
            return None
        if window >= _ONE_MILLION:
            return _ONE_MILLION
        return window
    except Exception:
        return None


def _states_one_million(value: object) -> bool:
    """Whether a stated window is the client's large-window spelling.

    At or above 1M is the same statement: the client has one large-window
    spelling, its per-model setting accepts 100k..1M, and every other surface
    here clamps to that ceiling.
    """

    return isinstance(value, int) and not isinstance(value, bool) and value >= _ONE_MILLION


def _kept_row(
    public_id: str,
    row: Mapping[str, object],
    published: object,
) -> dict[str, object]:
    """One discovery row for an id the client already keeps as written.

    The row is the router's own: id, display name, and the window the route is
    published at.  One thing is this view's to decide -- a route stated at 1M
    is published under the client's ``[1m]`` spelling, exactly as an alias row
    is.  ``max_input_tokens`` at 1M alone is a row the picker answers with two
    entries for one route (``Opus 5.5`` and ``Opus 5.5 1M``): the plain one
    reports the client's 200k default, and both name the route this router
    serves.  A suffixed id reads as the large window by name, so there is no
    second entry -- and unlike the profile preference that hides it
    (``modelPrefer1mContext``), a name travels with the answer to any client,
    including one pointed at a published host whose profile this app never
    writes.
    """

    kept = dict(row)
    # The resolution Core's Code picker names the id by
    # (``_code_route_window``): reading the canonical row alone publishes one
    # id here and launches another there whenever the two numbers disagree.
    window = row.get("max_input_tokens") if published is None else published
    if not _states_one_million(window):
        # A row stating 1M for a route published below it is the second picker
        # entry the suffix exists to avoid, so the number follows the window.
        kept["max_input_tokens"] = window
        return kept
    kept["max_input_tokens"] = _ONE_MILLION
    if context_suffix_free(public_id) == public_id:
        kept["id"] = f"{public_id}{CONTEXT_SUFFIX}"
    return kept


def with_published_windows(response: Mapping[str, object]) -> dict[str, object]:
    """State this app's published window on every Anthropic listing row.

    LiteLLM's Anthropic shape carries ``max_input_tokens`` only when the
    deployment states one.  A public window this app resolves from its own
    registry -- the number the canonical ``/v1/models`` already answers --
    otherwise arrives as null, and every reader of this shape then guesses: the
    model-list-plugin, for one, fills a null from public catalogs and writes a
    picker whose auto-compact window is the guessed number.

    A row that already states a window keeps it: that is LiteLLM's reading of
    the route, and this function only fills what would otherwise be no
    observation at all.  A generator's zero and an unresolvable route stay
    absent, exactly as in ``_published_context``.
    """

    rows = response.get("data")
    if not isinstance(rows, list):
        return dict(response)
    filled: list[object] = []
    for row in rows:
        if not isinstance(row, Mapping):
            filled.append(row)
            continue
        current = row.get("max_input_tokens")
        if isinstance(current, int) and not isinstance(current, bool) and current > 0:
            filled.append(dict(row))
            continue
        model_id = row.get("id")
        published = (
            _published_context(model_id, _public_context_window)
            if isinstance(model_id, str) and model_id.strip()
            else None
        )
        filled.append({**dict(row), "max_input_tokens": published} if published is not None else dict(row))
    return {**dict(response), "data": filled}


def with_claude_picker_metadata(response: Mapping[str, object]) -> dict[str, object]:
    """Publish the id and behavior the Claude Code picker will use.

    The canonical ``/v1/models`` answer is read by the model-list plugin, which
    writes Claude Code's settings.  Without these fields the plugin has to
    re-derive the Desktop alias, the ``[1m]`` spelling and the behavior tier;
    every one of those is a client-compatibility decision this app already
    makes in ``claude_picker``.  Stating them here is what lets the plugin's
    output and this app's own picker name one id space.

    A generator is marked as such rather than advertised as a conversation
    model.  The plugin drops non-chat rows, and the Claude views already do;
    the canonical row was the one place still calling an image route a model.
    """

    rows = response.get("data")
    if not isinstance(rows, list):
        return dict(response)
    filled: list[object] = []
    for row in rows:
        if not isinstance(row, Mapping):
            filled.append(row)
            continue
        current = dict(row)
        if _listing_row_serves_generation(current):
            current["type"] = "image"
            current.pop("claude_alias", None)
            current.pop("behaves_as", None)
            filled.append(current)
            continue
        model_id = current.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            filled.append(current)
            continue
        window = current.get("max_input_tokens")
        published = window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else None
        current["claude_alias"] = claude_picker.published_id(model_id, published)
        behavior = claude_picker.published_behaves_as(model_id, published)
        if behavior:
            current["behaves_as"] = behavior
        else:
            current.pop("behaves_as", None)
        filled.append(current)
    return {**dict(response), "data": filled}


def desktop_model_rows(
    response: Mapping[str, object],
    display_names: Mapping[str, str] | None = None,
    public_window: Callable[[str], int | None] | None = None,
) -> dict[str, object]:
    """Rewrite one Anthropic-shaped listing for Claude Desktop's discovery.

    A route the app already reads as an Anthropic model keeps the id it was
    listed under, so the app's own display name and controls are untouched;
    a 1M one carries the client's ``[1m]`` spelling so it stays one row (see
    ``_kept_row``).  Every other route is published under the alias
    ``claude_aliases`` derives, with the public name as ``display_name`` -- the
    app shows that name and derives the full effort ladder from the alias
    family.  Image and video generators are dropped in both cases.

    ``public_window`` is the one public number a route states.  It is
    injectable so the view can be checked without reading this machine's catalog
    cache.
    """

    window_for = public_window or _public_context_window
    names = display_names if isinstance(display_names, Mapping) else {}
    rows: list[dict[str, object]] = []
    for row in response.get("data") or []:
        if not isinstance(row, Mapping):
            continue
        public_id = row.get("id")
        if not isinstance(public_id, str) or not public_id.strip():
            continue
        if _listing_row_serves_generation(row):
            continue
        if desktop_keeps_model_id(public_id):
            rows.append(
                _kept_row(public_id, row, _published_context(public_id, window_for))
            )
            continue
        display_name = row.get("display_name") or names.get(public_id) or public_id
        published = _published_context(public_id, window_for)
        # Claude Desktop's 1M row is a name before it is a number: an id
        # carrying its own ``[1m]`` suffix is read at 1M by the session, and a
        # suffixed id is not a row that "offers 1M" to the picker, so no second
        # variant is synthesized beside it.  Publishing the suffix here is the
        # only gateway-side way to state a 1M route as one picker row: a
        # ``max_input_tokens`` at 1M alone yields ``<name>`` and
        # ``<name> 1M``, and the profile preference that collapses them
        # (``modelPrefer1mContext``) belongs to the client, not the gateway.
        # ``claude_aliases.context_suffix_free`` resolves the id back, so a turn
        # naming it reaches the same route.
        alias = claude_picker.published_id(public_id, published)
        rows.append(
            {
                "type": "model",
                "id": alias,
                "display_name": str(display_name),
                "created_at": row.get("created_at"),
                # This is the app's reader of a route's window, and it decides
                # one thing only: whether the row reads as offering 1M.  The
                # number is the route's published window, never the one the
                # listing's upstream reading stated (see ``_published_context``).
                "max_input_tokens": published,
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


def claude_code_model_selector_payload(
    surface: str,
    listing: Mapping[str, object],
    *,
    public_window: Callable[[str], int | None] | None = None,
    behavior_for: Callable[[str, object], str | None] | None = None,
) -> dict[str, object]:
    """Build Claude Code's selector config from an authenticated model list.

    Claude Code reads ``runtime.max_input_tokens`` and ``behaves_as`` from
    ``model_selector_config``; the Anthropic ``/v1/models`` window alone is not
    consulted for its session context calculation. Keep the public id it will
    send to the router, but give custom ids the same known Claude behavior tier
    the settings projection uses. The per-model ``autoCompactWindow`` in client
    settings then states the exact published context.

    ``public_window`` and ``behavior_for`` are injectable for focused tests.
    The production behavior mapping is owned by ClaudeSettingsDomain so the
    picker file and this served catalog cannot choose different tiers.
    """

    if surface not in _CLAUDE_CODE_SELECTOR_SURFACES:
        raise ValueError("unsupported Claude Code model-selector surface")
    window_for = public_window or _public_context_window
    if behavior_for is None:
        behavior_for = claude_picker.published_behaves_as

    models: list[dict[str, object]] = []
    raw_rows = listing.get("data")
    if isinstance(raw_rows, list):
        for row in raw_rows:
            if not isinstance(row, Mapping) or _listing_row_serves_generation(row):
                continue
            model_id = row.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            model_id = model_id.strip()
            model: dict[str, object] = {"id": model_id, "section": "main"}
            name = row.get("display_name") or row.get("name")
            if isinstance(name, str) and name.strip():
                model["name"] = name.strip()
            description = row.get("description")
            if isinstance(description, str) and description.strip():
                model["description"] = description.strip()

            window = _published_context(model_id, window_for)
            if isinstance(window, int) and not isinstance(window, bool):
                model["runtime"] = {"max_input_tokens": window}
                behaves_as = behavior_for(model_id, window)
                if isinstance(behaves_as, str) and behaves_as.strip():
                    model["behaves_as"] = behaves_as.strip()
            models.append(model)

    return {"model_selector_config": [{"id": surface, "models": models}]}


def install_patch() -> None:
    """Publish the Claude views through LiteLLM's Anthropic listing builder.

    ``proxy_server.model_list`` imports the builder inside the endpoint, so
    replacing the module attribute reaches both the admin-scoped and the
    ordinary listing branches.  The patch only acts while the request
    middleware marked this call as a Claude app's own discovery: every Claude
    view drops generators before LiteLLM names the rows, and the desktop app's
    own request additionally gets the alias view.
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
        if _GENERATION_FILTER_ACTIVE.get():
            models = [
                row
                for row in models
                if not _listing_row_serves_generation(row)
            ]
        response = original(models, display_names or {}, listed_ids or {})
        if not isinstance(response, Mapping):
            return response
        response = with_published_windows(response)
        if _VIEW_ACTIVE.get():
            return desktop_model_rows(response, display_names)
        if _CODE_VIEW_ACTIVE.get():
            # LiteLLM has already published Claude Code's compatibility ids
            # (``claude-router-…``) for this view.  Adding this app's picker
            # alias on top of those ids made ``[1m][1m]``: the canonical alias
            # metadata belongs to the generic listing the plugin reads, not to
            # the Claude Code view.
            return response
        return with_claude_picker_metadata(response)

    setattr(patched, _PATCH_ATTR, True)
    setattr(patched, "_original_create_anthropic_model_list_response", original)
    anthropic_common.create_anthropic_model_list_response = patched


class _DesktopListingMiddleware:
    """Mark a Claude app's own listing request for the response builder."""

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
        desktop = is_claude_desktop_client(headers)
        code = is_claude_code_client(headers)
        view_token = _VIEW_ACTIVE.set(desktop)
        code_token = _CODE_VIEW_ACTIVE.set(code and not desktop)
        filter_token = _GENERATION_FILTER_ACTIVE.set(desktop or code)
        try:
            await self.app(scope, receive, send)
        finally:
            _GENERATION_FILTER_ACTIVE.reset(filter_token)
            _CODE_VIEW_ACTIVE.reset(code_token)
            _VIEW_ACTIVE.reset(view_token)


def register(app: Any) -> None:
    """Install discovery middleware and Claude Code's authenticated selector."""

    if not getattr(app, _REGISTERED_ATTR, False):
        app.add_middleware(_DesktopListingMiddleware)
        setattr(app, _REGISTERED_ATTR, True)

    if getattr(app, _SELECTOR_REGISTERED_ATTR, False):
        return

    from fastapi import Depends, Request
    from fastapi.responses import JSONResponse
    from litellm.proxy import proxy_server

    async def selector_handler(
        surface: str,
        request: Request,
        user_api_key_dict: Any = Depends(proxy_server.user_api_key_auth),
    ) -> JSONResponse:
        if surface not in _CLAUDE_CODE_SELECTOR_SURFACES:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # Use LiteLLM's own caller-scoped listing so access groups and hot
        # reloads have exactly the same visibility as /v1/models. Passing no
        # HTTP request keeps this JSON-only endpoint from switching to the
        # Anthropic or Desktop alias view.
        listing = await proxy_server.model_list(
            request=None,
            user_api_key_dict=user_api_key_dict,
        )
        if not isinstance(listing, Mapping):
            return JSONResponse({"error": "model listing unavailable"}, status_code=503)
        return JSONResponse(claude_code_model_selector_payload(surface, listing))

    # The module uses postponed annotations, but FastAPI resolves these classes
    # at route registration time. Bind the real runtime types just like the
    # other optional-FastAPI handlers in this package.
    selector_handler.__annotations__ = {
        "surface": str,
        "request": Request,
        "user_api_key_dict": proxy_server.UserAPIKeyAuth,
        "return": JSONResponse,
    }
    app.add_api_route(
        "/api/model_selector/{surface}",
        selector_handler,
        methods=["GET"],
        include_in_schema=False,
    )
    setattr(app, _SELECTOR_REGISTERED_ATTR, True)


__all__ = [
    "CLAUDE_CODE_CLIENT",
    "CLAUDE_CODE_CLIENT_HEADER",
    "CLAUDE_DESKTOP_CLIENT",
    "CLAUDE_DESKTOP_CLIENT_HEADER",
    "claude_code_model_selector_payload",
    "desktop_model_rows",
    "install_patch",
    "is_claude_code_client",
    "is_claude_desktop_client",
    "is_claude_listing_client",
    "register",
    "with_claude_picker_metadata",
    "with_published_windows",
]
