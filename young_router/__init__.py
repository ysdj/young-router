"""Young Router Python package.

The Core/IPC package must be importable by a native host before the optional
LiteLLM runtime is installed.  Keep this module side-effect free and lazily
load the proxy hook only in the service process.
"""

from __future__ import annotations

__all__ = ["YoungRouterHook"]


def __getattr__(name: str):
    if name == "YoungRouterHook":
        from .hook import YoungRouterHook

        return YoungRouterHook
    raise AttributeError(name)
