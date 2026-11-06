"""The lines a host's own log has to keep when the app misbehaves silently.

Core answers a request or raises; a background loop has neither caller.  A
supervision pass that could not start the proxy, a replacement that died between
the stop and the launch -- those are the failures that leave a host serving
nothing, and they were the ones nothing wrote down.  The unit's journal and the
service log are what a person reads afterwards, so a background failure states
itself there, once, in the same shape as the rest of the process's output.

Not a logging framework: one function, one stream, and a prefix that says which
loop is talking.  Anything a caller can see as a return value still travels that
way -- this is for the paths that have no caller.
"""

from __future__ import annotations

import sys
import traceback

PREFIX = "young-router"


def note(message: str, *, trace: BaseException | None = None) -> None:
    """State one background failure on stderr, where the host's log collects it."""

    print(f"{PREFIX}: {message}", file=sys.stderr, flush=True)
    if trace is not None:
        detail = "".join(traceback.format_exception(type(trace), trace, trace.__traceback__))
        print(detail.rstrip(), file=sys.stderr, flush=True)
