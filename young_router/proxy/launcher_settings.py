"""The names and limits a launcher needs before the workers' runtime is loaded.

``young_router/proxy/macos_proxy.py`` starts the forkserver whose preload imports
the complete LiteLLM runtime, and then imports what it needs from the app's own
modules.  Importing them from ``base`` or ``state`` used to load that runtime a
second time in the launcher itself -- before the preload had even started, so
the two ran one after the other and every restart paid for both.  The few names
the launcher and the state files share live here instead: importing this module
imports nothing but the standard library.
"""

from __future__ import annotations

# Files the state files live in.  Both sides resolve these by name, so the name
# is stated once and the launcher's own module no longer has to reach the
# runtime to read it.
_DEPLOYMENT_COOLDOWN_FILE_ENV = "YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_FILE"
_RECENT_REQUESTS_LOG_ENV = "LITELLM_RECENT_REQUESTS_LOG"
_ROUTE_RECOVERY_STATE_FILE_ENV = "YOUNG_ROUTER_ROUTE_RECOVERY_STATE_FILE"
_SESSION_DEPLOYMENT_AFFINITY_FILE_ENV = "YOUNG_ROUTER_SESSION_DEPLOYMENT_AFFINITY_FILE"

_WEBSOCKET_MAX_FRAME_BYTES_ENV = "YOUNG_ROUTER_WEBSOCKET_MAX_FRAME_BYTES"
_WEBSOCKET_MAX_FRAME_DEFAULT_BYTES = 64.0 * 1024.0 * 1024.0


def _websocket_max_frame_bytes() -> int:
    """Maximum inbound WebSocket frame size for the managed proxy.

    Codex sends each Responses turn request -- including the immutable replay
    prefix with its signed image bytes -- as one text frame.  uvicorn's default
    16 MiB limit closes such connections with code 1009 before the request
    reaches the pipeline, which Codex reports as ``websocket closed by server
    before response.completed`` followed by its reconnect ladder.
    """

    import os

    raw = os.getenv(_WEBSOCKET_MAX_FRAME_BYTES_ENV, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return int(_WEBSOCKET_MAX_FRAME_DEFAULT_BYTES)
