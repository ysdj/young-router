"""Shared process-lifecycle limits used by the Core and proxy workers.

Who waits for what -- the pair that keeps being got wrong:

* A stop waits for nothing.  A restart, an install, the app quitting and the
  Core stopping its own proxy all end the same way: the pids the bundle is
  running are killed together (``scripts/app-processes.sh stop``,
  ``CoreServiceController.stop``), and the replacement starts as soon as they
  are gone.  The loopback listener is free in that instant, and a turn cut
  there is one the client reconnects through.  No drain budget belongs in that
  path, and a signal ladder that gives the tree time to unwind is not a faster
  version of it -- it is the wait, put back.
* A worker that is *retired* rather than killed -- the rolling reload a settings
  apply performs, or a group signalled because its Core is gone -- runs its own
  shutdown.  It does not wait for the turns it is serving either
  (``APP_QUIT_GRACE_SECONDS``): the shutdown patch in ``proxy/base.py`` closes
  each open stream as a completed response carrying what already arrived, and
  the client reconnects for the rest.

There is no third case.  A marker file once meant to tell an app exit from a
replacement, and a stop grace that went with it, were never written by anything:
every stop was already a kill, so no worker was ever alive to read them.
"""

from __future__ import annotations


# The graceful bound handed to uvicorn when the proxy starts.  It is what would
# apply if a worker shut down without this app's shutdown patch installed -- the
# patch narrows it to the terminal-flush window for as long as it runs -- so it
# is a ceiling for that case, not a wait any stop path performs.
PROXY_DRAIN_GRACE_SECONDS = 14_400.0

# How long a worker waits for in-flight responses when it shuts down.
#
# Nothing.  Both clients this router serves reconnect on their own, so waiting
# for a turn that is still being written buys nothing and postpones the
# replacement.  A stream that is still open at shutdown is closed as a
# completed response carrying the output that already arrived, so the client
# keeps its partial answer instead of being shown an interruption, and then
# reconnects for the rest of its turn.
#
# This is a floor rather than the whole mechanism: uvicorn cancels a stream task
# the moment its graceful timeout expires, which would close the socket without
# the terminal the client needs.  The shutdown patch closes each open stream
# itself before that cancel lands, so a zero here means "do not wait", not "cut
# the socket mid-write".
APP_QUIT_GRACE_SECONDS = 0.0


def proxy_drain_grace_seconds() -> float:
    """The graceful bound uvicorn is started with (see the constant).

    Not a budget for a restart or an install: those kill the captured tree and
    start the replacement without waiting for any drain at all (module
    docstring), so nothing in those paths may read this.
    """

    return PROXY_DRAIN_GRACE_SECONDS


def app_quit_grace_seconds() -> float:
    """How long a worker waits for in-flight responses when it shuts down.

    Zero on purpose: the shutdown patch in ``proxy/base.py`` closes an open
    stream itself, so a replacement never postpones itself for a turn that is
    still being written.
    """

    return APP_QUIT_GRACE_SECONDS
