"""Shared process-lifecycle limits used by the Core and proxy workers."""

from __future__ import annotations

import math
import os


PROXY_DRAIN_GRACE_SECONDS = 14_400.0
PROXY_DRAIN_GRACE_ENV = "YOUNG_ROUTER_PROXY_DRAIN_SECONDS"


def proxy_drain_grace_seconds() -> float:
    """Maximum time a service replacement gives accepted proxy requests."""

    raw = os.environ.get(PROXY_DRAIN_GRACE_ENV, "").strip()
    if not raw:
        return PROXY_DRAIN_GRACE_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        return PROXY_DRAIN_GRACE_SECONDS
    if not math.isfinite(parsed) or parsed < 0:
        return PROXY_DRAIN_GRACE_SECONDS
    return parsed
