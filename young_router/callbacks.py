from __future__ import annotations

from .hook import YoungRouterHook
from .patches import install_all

install_all()

image_generation_routing_hook = YoungRouterHook()

__all__ = ["YoungRouterHook", "image_generation_routing_hook"]
