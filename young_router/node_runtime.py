"""Resolve the Node.js executable the bundled worker adapters run on.

dsh-vision-router and WorkBuddy each ship a Node shim beside their staged package, and
both resolve the interpreter the same way: an explicit environment override,
the runtime bundled with the installed app, then ``PATH``.  Keeping that
resolution here means one adapter cannot drift from the other.
"""

from __future__ import annotations

from collections.abc import Sequence
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


def bundled_executable(*, env_name: str, name: str) -> str:
    """Resolve an optional executable this app ships beside its Node runtime.

    Unlike :func:`node_command` this answers empty rather than raising: a
    bundled helper the app can do without, or one an older artifact build did
    not stage, is a package's own fallback to take, not this side's refusal.
    An explicit override is honoured first so a host can point at its own copy.
    """

    configured = os.environ.get(env_name, "").strip()
    if configured:
        return configured
    # Both spellings are tried, for the same reason the Node runtime is looked
    # up under both: an artifact can be inspected from a host it was not built
    # for.  A missing helper degrades to the package's own discovery rather
    # than to a path that never existed.
    for alias in (name, Path(name).stem):
        candidate = core_root() / "runtime" / "bin" / alias
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name) or ""


__all__ = ["bundled_executable", "core_root", "node_command"]
