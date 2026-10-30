"""The fetch tool over HTTP, for a gateway whose client is somewhere else.

A desktop install registers ``view_image_by_id`` as a stdio server in the
client's own configuration -- it runs beside the client, on the same machine.
A Young Router deployed on a server has no such file to edit: the client is on
somebody's laptop, so the only tool it can reach is a URL.

This module is that URL.  It speaks the same JSON-RPC the stdio server speaks
(``image_context_mcp.handle``), served at ``POST /mcp`` on the proxy itself, and
it is authenticated with the proxy's own master key so the images a conversation
omitted are not readable by anyone who can reach the port.

Two boundaries are deliberate:

* **Nothing here decides which images are omitted.**  That is the pass in
  ``image_context.py``; this only answers for an id it stored.
* **The route is inert unless the operator asked for it.**  Registration is
  skipped unless the fetch mode is ``http`` (or ``auto`` on a host with no client
  configuration), so a desktop install never opens a second door to its own
  conversation images.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Mapping, Optional

from . import image_context as _image_context_module
from . import image_context_mcp as _mcp_module

# Not ``/mcp``: LiteLLM serves its own MCP gateway there, and a route added after
# it never matches.  These two spellings are this app's own, and the ``/v1`` one
# survives a deployment that puts the proxy behind a path prefix.
MCP_PATHS = ("/v1/image-context/mcp", "/image-context/mcp")
MAX_BODY_BYTES = 1 * 1024 * 1024


def enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether this host should serve the endpoint at all."""

    env = os.environ if environ is None else environ
    mode = _image_context_module.fetch_mode(env)
    if mode == "http":
        return True
    if mode != "auto":
        # ``stdio`` is a desktop client whose configuration this app edits, and
        # ``off`` promises no tool: neither opens a port.
        return False
    return not _image_context_module._client_config_present(env)


def _expected_token(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    for name in (
        "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_TOKEN",
        "LITELLM_MASTER_KEY",
    ):
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def authorized(authorization: Optional[str], environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether a request may read this installation's omitted images."""

    expected = _expected_token(environ)
    if not expected:
        # No token configured is not "open to everyone": an unauthenticated
        # endpoint would hand a conversation's images to any caller.
        return False
    presented = str(authorization or "").strip()
    if presented.lower().startswith("bearer "):
        presented = presented[7:].strip()
    return bool(presented) and hmac.compare_digest(presented, expected)


def answer(body: bytes) -> tuple:
    """One JSON-RPC message in, ``(status, payload)`` out."""

    if len(body) > MAX_BODY_BYTES:
        return 413, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "request too large"}}
    try:
        message = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
    if not isinstance(message, dict):
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    response = _mcp_module.handle(message)
    if response is None:
        # A notification carries no answer; the transport still needs one.
        return 202, {}
    return 200, response


def register(app: Any) -> None:
    """Add the endpoint to a running app, when this host should serve it.

    The handler is defined here so its annotations resolve against the real
    ``Request`` class at import time; the class itself stays out of this
    module's scope so hosts without FastAPI can still import it.
    """

    if not enabled():
        return

    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def handler(request: Request) -> JSONResponse:
        if not authorized(request.headers.get("authorization")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        status, payload = answer(await request.body())
        return JSONResponse(payload, status_code=status)

    handler.__annotations__ = {"request": Request, "return": JSONResponse}
    for path in MCP_PATHS:
        app.add_api_route(path, handler, methods=["POST"], include_in_schema=False)
