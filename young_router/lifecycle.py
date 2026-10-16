"""Shared process-lifecycle limits used by the Core and proxy workers.

Who waits for what -- the pair that keeps being got wrong:

* A restart or an install waits for nothing.  Both capture the pids the bundle
  is running and kill them together (``scripts/app-processes.sh stop``), then
  start the replacement as soon as they are gone: the loopback listener is free
  in that instant, and a turn cut there is one the client reconnects through.
  No drain budget belongs in that path, and a signal ladder that gives the tree
  time to unwind is not a faster version of it -- it is the wait, put back.
* A deliberate service replacement -- the Core restarting the proxy on its own,
  not on its way out -- keeps ``PROXY_DRAIN_GRACE_SECONDS``: it is not in a
  hurry, and it closes the turns it accepted rather than cutting them.
* The app quitting on its own (the host's quit request, not a script) is the one
  stop that gets ``APP_EXIT_STOP_GRACE_SECONDS`` and the marker: enough time for
  an open stream to write its terminal, and no time spent draining.
"""

from __future__ import annotations

import os
from pathlib import Path


# The drain a *deliberate* replacement gives the requests it already accepted.
# A restart or an install never reads this (see the module docstring).
PROXY_DRAIN_GRACE_SECONDS = 14_400.0

# The Core owns the app-exit decision, but the proxy it stops was forked much
# earlier and cannot see the Core's environment.  A marker file in the state
# root both sides already resolve is what carries that one bit across the fork:
# the Core drops it before signalling the process group, and the worker reads it
# during teardown to pick the short app-exit bound instead of the long drain.  A
# restart or an install does not mark it and does not wait for anything: it kills
# the tree outright.
APP_EXIT_MARKER_FILE = "app-exiting.marker"


def app_exit_marker_path(root: Path | str | None = None) -> Path:
    """The app-exit marker both the Core and the proxy resolve."""

    if root is None:
        configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
            "YOUNG_ROUTER_HOME", ""
        ).strip()
        root = Path(configured).expanduser() if configured else Path.home() / ".young-router"
    return Path(root).expanduser() / APP_EXIT_MARKER_FILE


def mark_app_exit(root: Path | str | None = None) -> None:
    """Record that this stop belongs to an app exit rather than a replacement."""

    try:
        path = app_exit_marker_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1", encoding="utf-8")
    except OSError:
        pass


def clear_app_exit_marker(root: Path | str | None = None) -> None:
    """Drop a stale marker so a later deliberate replacement keeps its drain.

    Only a deliberate replacement reads that drain; a restart or an install does
    not wait for one (module docstring).
    """

    try:
        app_exit_marker_path(root).unlink()
    except OSError:
        pass


def app_exit_in_progress(root: Path | str | None = None) -> bool:
    """Whether an app exit has been recorded for this state root."""

    try:
        return app_exit_marker_path(root).is_file()
    except OSError:
        return False

# How long a worker waits for in-flight responses when the *app* is being
# replaced, as opposed to a proxy the Core is draining on purpose.
#
# Nothing.  Both clients this router serves reconnect on their own, so waiting
# for a turn that is still being written buys nothing and postpones the
# replacement the user asked for.  A stream that is still open at shutdown is
# closed as a completed response carrying the output that already arrived, so
# the client keeps its partial answer instead of being shown an interruption,
# and then reconnects for the rest of its turn.
#
# This is a floor rather than the whole mechanism: uvicorn cancels a stream task
# the moment its graceful timeout expires, which would close the socket without
# the terminal the client needs.  The shutdown patch closes each open stream
# itself before that cancel lands, so a zero here means "do not wait", not "cut
# the socket mid-write".
APP_QUIT_GRACE_SECONDS = 0.0

# What the Core gives the proxy's process group when the app is exiting, as
# opposed to the four-hour drain a deliberate replacement uses.  It is not a
# wait for the turn: the worker only needs enough time to run the marked branch
# that closes its open turn as completed.  A zero here is what made an app exit
# SIGKILL the group before that terminal could be written, so the client was
# shown a bare close even though the marker had been recorded correctly.
APP_EXIT_STOP_GRACE_SECONDS = 2.0


def proxy_drain_grace_seconds() -> float:
    """How long a *deliberate* service replacement drains what it accepted.

    Not a budget for a restart or an install: those kill the captured tree and
    start the replacement without waiting for any drain at all (module
    docstring), so nothing in those paths may read this.
    """

    return PROXY_DRAIN_GRACE_SECONDS


def app_quit_grace_seconds() -> float:
    """How long a worker waits for in-flight responses during an app exit.

    Zero on purpose: the shutdown patch in ``proxy/base.py`` closes an open
    stream itself, so a replacement never postpones itself for a turn that is
    still being written.
    """

    return APP_QUIT_GRACE_SECONDS
