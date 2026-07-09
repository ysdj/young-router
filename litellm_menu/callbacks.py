from __future__ import annotations

from .hook import LiteLLMMenuHook
from .patches import install_all

install_all()

image_generation_routing_hook = LiteLLMMenuHook()

__all__ = ["LiteLLMMenuHook", "image_generation_routing_hook"]
