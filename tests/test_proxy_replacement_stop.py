"""The stop that precedes a replacement has to end.

Two real failures met in this path on 2026-11-06:

* the stopped proxy was this Core's own child and nobody collected it, so its
  zombie stayed a member of its process group -- ``killpg(pgid, 0)`` answered
  success forever and the drain loop never saw the group go away;
* that loop's budget was the four-hour app-quit drain, so "never" was long
  enough to be an outage: the reload thread held the port shut, and the recovery
  loop is suppressed while a reload owns the proxy.

These drive real processes, because the whole point is what the kernel answers.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from young_router.core.operations import CoreServiceController


def spawn(code: str, **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        start_new_session=True,
        **kwargs,
    )


@unittest.skipUnless(hasattr(os, "killpg"), "process groups are POSIX")
class ReplacementStopTests(unittest.TestCase):
    def test_a_dead_child_left_uncollected_does_not_hold_the_group_alive(self) -> None:
        # `Popen.wait()` would reap it, which is the very thing the app did not
        # do.  Exiting without collecting it leaves exactly the zombie a stopped
        # proxy used to leave.
        child = spawn("pass")
        pid = child.pid
        for _ in range(50):
            if not os.path.exists(f"/proc/{pid}") or _is_zombie(pid):
                break
            time.sleep(0.02)
        self.assertTrue(CoreServiceController._group_alive(pid), "a zombie is still a group member")

        started = time.monotonic()
        CoreServiceController._stop_process_group(pid)
        elapsed = time.monotonic() - started

        self.assertFalse(CoreServiceController._group_alive(pid), "the group still answers after the stop")
        self.assertLess(elapsed, 3.0, "the stop waited on its own zombie instead of collecting it")
        child.wait(timeout=5)

    def test_a_child_that_ignores_sigterm_is_still_killed_at_once(self) -> None:
        child = spawn(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('up', flush=True)\n"
            "time.sleep(60)\n",
            stdout=subprocess.PIPE,
        )
        self.assertEqual(child.stdout.readline().strip(), b"up")
        try:
            started = time.monotonic()
            CoreServiceController._stop_process_group(child.pid)
            elapsed = time.monotonic() - started
        finally:
            if child.stdout is not None:
                child.stdout.close()

        self.assertLess(elapsed, 5.0, "the kill did not end the wait")
        self.assertFalse(CoreServiceController._group_alive(child.pid))
        child.wait(timeout=5)

    def test_a_stop_does_not_drain_before_it_kills(self) -> None:
        # The bound a drain used to give is not consulted by a stop any more:
        # a process that would ignore SIGTERM for a long time must still be gone
        # as soon as the stop returns, not one bound later.
        child = spawn(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: time.sleep(120))\n"
            "print('up', flush=True)\n"
            "time.sleep(120)\n",
            stdout=subprocess.PIPE,
        )
        self.assertEqual(child.stdout.readline().strip(), b"up")
        try:
            started = time.monotonic()
            CoreServiceController._stop_process_group(child.pid)
            elapsed = time.monotonic() - started
        finally:
            if child.stdout is not None:
                child.stdout.close()
        self.assertLess(elapsed, 5.0)
        child.wait(timeout=5)


def _is_zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            state = handle.read().rsplit(b")", 1)[-1].split()[0]
    except OSError:
        return True
    return state == b"Z"


if __name__ == "__main__":
    unittest.main()
