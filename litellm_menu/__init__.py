"""LiteLLM Menu Python package.

The Core/IPC package must be importable by a native host before the optional
LiteLLM runtime is installed.  Keep this module side-effect free and lazily
retain the legacy hook export for the service process.
"""

from __future__ import annotations

__all__ = ["LiteLLMMenuHook"]


def __getattr__(name: str):
    if name == "LiteLLMMenuHook":
        from .hook import LiteLLMMenuHook

        return LiteLLMMenuHook
    raise AttributeError(name)
