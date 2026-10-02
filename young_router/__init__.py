"""Young Router Python package.

The project (and its distribution) is named ``young-router``; Python import
names cannot contain hyphens, so the one package this repository owns is
``young_router`` — the same mapping every hyphenated Python project uses
(``python-dateutil`` -> ``dateutil``, ``scikit-learn`` -> ``sklearn``).

The Core/IPC package must be importable by a native host before the optional
LiteLLM runtime is installed.  Keep this module side-effect free and lazily
load the proxy hook only in the service process.
"""

from __future__ import annotations

__all__ = ["YoungRouterHook"]


def __getattr__(name: str):
    if name == "YoungRouterHook":
        from .proxy.hook import YoungRouterHook

        return YoungRouterHook
    raise AttributeError(name)
