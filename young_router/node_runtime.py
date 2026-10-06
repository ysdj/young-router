"""Resolve the Node.js executable the bundled worker adapters run on.

dsh-vision-router and WorkBuddy each ship a Node shim beside their staged package, and
both resolve the interpreter the same way: an explicit environment override,
the runtime bundled with the installed app, then ``PATH``.  Keeping that
resolution here means one adapter cannot drift from the other.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Callable


def core_root() -> Path:
    """The Core directory that owns the bundled ``runtime/`` directory."""

    return Path(__file__).resolve().parents[1]


def node_command(
    *,
    env_name: str,
    label: str,
    unavailable: Callable[[str], Exception],
) -> str:
    """Resolve Node.js: an explicit override, the bundled runtime, then PATH."""

    configured = os.environ.get(env_name, "").strip()
    if configured:
        return configured
    bundled_name = "node.exe" if os.name == "nt" else "node"
    candidates = (
        core_root() / "runtime" / "bin" / bundled_name,
        core_root() / "runtime" / "bin" / "node",
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    node = shutil.which("node")
    if node:
        return node
    raise unavailable(
        f"{label} requires the bundled Node.js runtime; "
        f"set {env_name} to its executable path"
    )


__all__ = ["core_root", "node_command"]
