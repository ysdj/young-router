"""The DSH vision-router environment keys Core and its runtime settings share.

The quick settings are published to the proxy process as individual
environment variables and persisted inside the ``..._CONFIG_JSON`` payload.
Runtime settings and service operations read the same map, so a key can never
be published under one name and read under another.
"""

from __future__ import annotations

DSH_VISION_ROUTER_CONFIG_KEY = "YOUNG_ROUTER_DSH_VISION_ROUTER_CONFIG_JSON"
DSH_VISION_ROUTER_QUICK_KEYS = {
    "YOUNG_ROUTER_DSH_VISION_ROUTER_ENABLED": "enabled",
    "YOUNG_ROUTER_DSH_VISION_ROUTER_BACKEND": "backend",
    "YOUNG_ROUTER_DSH_VISION_ROUTER_FREE_FALLBACK": "freeFallback",
    "YOUNG_ROUTER_DSH_VISION_ROUTER_TIMEOUT_SECONDS": "timeoutSeconds",
    "YOUNG_ROUTER_DSH_VISION_ROUTER_MAX_TOKENS": "maxTokens",
}
DSH_VISION_ROUTER_LOCAL_QUICK_KEYS = {
    "YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED": "localOllama",
    "YOUNG_ROUTER_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED": "localLmStudio",
}

__all__ = [
    "DSH_VISION_ROUTER_CONFIG_KEY",
    "DSH_VISION_ROUTER_LOCAL_QUICK_KEYS",
    "DSH_VISION_ROUTER_QUICK_KEYS",
]
