"""A background failure has no caller, so it has to state itself.

The failures that leave a host serving nothing are the ones with nobody to ask:
a replacement that does not come up, a recovery pass that keeps failing, a
document that cannot be staged before the proxy starts.  Each of these used to
end in a silent `return`, and the unit went on reporting `active`.
"""

from __future__ import annotations

import contextlib
import io
import unittest

from young_router.core import service_supervisor
from young_router.core.oplog import note


class StatedFailureTests(unittest.TestCase):
    def test_a_note_reaches_stderr_with_its_prefix(self) -> None:
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            note("the proxy did not come up")
        self.assertEqual(captured.getvalue().strip(), "young-router: the proxy did not come up")

    def test_a_note_carries_the_traceback_it_was_given(self) -> None:
        captured = io.StringIO()
        try:
            raise RuntimeError("staging failed")
        except RuntimeError as exc:
            with contextlib.redirect_stderr(captured):
                note("staging failed", trace=exc)
        written = captured.getvalue()
        self.assertIn("young-router: staging failed", written)
        self.assertIn("RuntimeError: staging failed", written)

    def test_a_failed_recovery_pass_is_stated_and_the_loop_keeps_going(self) -> None:
        calls: list[int] = []

        def supervise() -> None:
            calls.append(1)
            raise RuntimeError("the proxy could not be started")

        supervisor = service_supervisor.ServiceSupervisor(supervise=supervise, interval=1.0)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            supervisor.run_once()
        self.assertEqual(len(calls), 1)
        self.assertIn("a recovery pass failed", captured.getvalue())
        self.assertIn("the proxy could not be started", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
