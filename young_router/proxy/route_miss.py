"""The route this app does not have, answered in words.

LiteLLM's app is FastAPI, so a request that nothing routed reaches Starlette's
own not-found and answers ``{"detail":"Not Found"}``: 22 bytes, no code, no
path, and nothing to tell "this host serves no such route" from "the model
refused this request".  Both clients read that as something else -- Pi renders
the status and the body verbatim as the assistant's answer, and Codex maps an
unrecognised code to ``Retryable`` -- so the mistake it nearly always describes
is both invisible on screen and retried on the wire.

That mistake has two shapes, and both are a *path* setting the client owns:

* ``/v1/v1/...`` -- the base URL already ends in ``/v1`` and the wire adds it
  again.  The Anthropic messages wire adds the prefix itself, so the same
  provider needs ``origin`` for ``/v1/messages`` and ``origin/v1`` for
  ``/responses``; copying one model's base URL to the other doubles it.
* ``/messages`` with no prefix at all -- the mirror error, a base URL that is
  missing the ``/v1`` the wire does not add.

Both are answered ``400`` with ``invalid_request_error``: only the client's
configuration can change the answer, so the status must not look like a missing
server route (404) or an upstream refusal.  A path that is simply unknown keeps
the honest ``404``, still with a body that names the method and the path instead
of Starlette's object.

The router's own not-found hook is the seam.  LiteLLM's routes are untouched,
the trailing-slash redirect and the 405 for a known path are both decided
*before* the hook runs, and a 404 raised inside a route keeps its own body, so
nothing here has to be recognised by its shape afterwards.  Only a request that
matched nothing arrives at this module.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_LOG = logging.getLogger(__name__)

#: The prefix this port publishes, and the one a client can add a second time.
API_PREFIX = "/v1"
#: The client's path setting, doubled or missing.  Both are 400s.
DOUBLED_PREFIX_CODE = "api_prefix_doubled"
MISSING_PREFIX_CODE = "api_prefix_missing"
#: A path this port really does not serve.
UNKNOWN_ROUTE_CODE = "unknown_route"

#: The paths this port serves under ``/v1``, as a client would write them last.
#:
#: A bare path that equals one of these was meant to be under ``/v1``: the wire
#: is right and the base URL under it is short by one segment.  LiteLLM serves
#: the OpenAI-style ones with and without the prefix, so in practice this names
#: the messages wire -- which is exactly the one whose base URL must not carry
#: ``/v1``, the same segment the doubled case cannot agree with.
WIRE_TAILS = (
    "messages",
    "messages/count_tokens",
    "responses",
    "chat/completions",
    "completions",
    "models",
    "embeddings",
    "images/generations",
)

_REGISTERED_ATTR = "_young_router_route_miss"


def doubled_api_prefix(path: str) -> bool:
    """Whether this path arrived with ``/v1`` twice."""

    doubled = API_PREFIX + API_PREFIX
    return path == doubled or path.startswith(doubled + "/")


def missing_api_prefix(path: str) -> bool:
    """Whether this path is a wire this port serves, minus its ``/v1``."""

    if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
        return False
    return path.strip("/") in WIRE_TAILS


def intended_path(path: str) -> str:
    """The path a doubled prefix meant: the same one with one prefix removed."""

    return path[len(API_PREFIX):] if doubled_api_prefix(path) else path


def _is_messages_wire(path: str) -> bool:
    """Whether this tail is the wire that adds ``/v1`` all by itself."""

    relative = path[len(API_PREFIX):] if path.startswith(API_PREFIX + "/") else path
    return relative.strip("/").split("/", 1)[0] == "messages"


def message_for(method: str, path: str) -> str:
    """Say what was asked for, and which setting only the client can change."""

    if doubled_api_prefix(path):
        meant = intended_path(path)
        # Which base URL is correct is decided by the wire, and the two wires
        # this port serves disagree: the messages wire adds ``/v1`` itself (so
        # its base URL is the origin), the OpenAI-style ones expect it in the
        # base URL and append only the endpoint.  Advice that covered both at
        # once would be advice about neither.
        advice = (
            "this client's base URL must be this host's origin with no /v1, because"
            " the messages wire adds the prefix itself"
            if _is_messages_wire(meant)
            else "this client's base URL ends in /v1 one time too many: it should end"
            " with exactly one /v1, because this wire appends only the endpoint"
        )
        return " ".join((
            f"This gateway has no route for {method} {path}; the path it meant is"
            f" {method} {meant}.",
            f"The /v1 prefix arrived twice, so {advice}.",
            "Nothing was sent to a provider.",
        ))
    if missing_api_prefix(path):
        return " ".join((
            f"This gateway has no route for {method} {path}; the path it meant is"
            f" {method} {API_PREFIX}{path}.",
            f"That path is served here, so this client's base URL is missing the {API_PREFIX}"
            " suffix instead.",
            "Nothing was sent to a provider.",
        ))
    return " ".join((
        f"This gateway has no route for {method} {path}.",
        "Check the endpoint and the base URL this client is configured with.",
        "Nothing was sent to a provider.",
    ))


def body_for(method: str, path: str) -> tuple[int, dict[str, Any]]:
    """The status and the body for whichever envelope this client reads.

    ``error.message`` and ``error.code`` are what an OpenAI-shaped client reads;
    ``{"type": "error", "error": {"type", "message"}}`` is the Anthropic envelope.
    It is the same object, so no request has to be told which shape it is
    getting.  ``invalid_request_error`` is the honest class for a path setting:
    the same bytes cannot succeed, so nothing here invites a retry.
    """

    if doubled_api_prefix(path):
        status, code = 400, DOUBLED_PREFIX_CODE
    elif missing_api_prefix(path):
        status, code = 400, MISSING_PREFIX_CODE
    else:
        status, code = 404, UNKNOWN_ROUTE_CODE
    return status, {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": message_for(method, path),
            "param": None,
            "code": code,
        },
    }


async def not_found(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Answer a path nothing matched, in place of Starlette's bare 404."""

    if scope.get("type") == "websocket":
        # The same close Starlette's not-found sends, so a websocket client
        # sees no new behaviour from this hook.
        await send({"type": "websocket.close", "code": 1000})
        return
    if scope.get("type") != "http":
        return
    method = str(scope.get("method") or "GET").upper()
    path = str(scope.get("path") or "/")
    status, body = body_for(method, path)
    # A doubled or missing prefix is a client whose base URL is wrong right now:
    # it is worth one line in the host's journal, because the operator is the one
    # who can name the client.  Any other miss is what a scanner produces.
    miss = body["error"]["code"] != UNKNOWN_ROUTE_CODE
    _LOG.log(
        logging.WARNING if miss else logging.INFO,
        "route miss: %s %s (%s)",
        method,
        path,
        body["error"]["code"],
    )
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(payload)).encode("latin-1")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


def register(app: Any) -> bool:
    """Take over the not-found answer of a running app.

    Returns whether a router was there to take over.  Idempotent: the proxy is
    imported once per worker, and a second registration must not stack hooks.
    """

    router = getattr(app, "router", None)
    if router is None or not hasattr(router, "default"):
        return False
    if getattr(router, _REGISTERED_ATTR, False):
        return True
    setattr(router, _REGISTERED_ATTR, True)
    router.default = not_found
    return True


__all__ = [
    "API_PREFIX",
    "DOUBLED_PREFIX_CODE",
    "MISSING_PREFIX_CODE",
    "UNKNOWN_ROUTE_CODE",
    "WIRE_TAILS",
    "body_for",
    "doubled_api_prefix",
    "intended_path",
    "message_for",
    "missing_api_prefix",
    "not_found",
    "register",
]
