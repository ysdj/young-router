"""WorkBuddy adapter edges a stub worker cannot reach on its own."""

from __future__ import annotations

import tempfile
import threading
import types
import unittest
from unittest import mock

from young_router.adapters import workbuddy


class WorkBuddyReadyLineTests(unittest.TestCase):
    """The readiness read owns a deadline, not a check after each read."""

    def test_a_worker_that_never_announces_itself_is_abandoned(self) -> None:
        release = threading.Event()

        class SilentStdout:
            def readline(self) -> bytes:
                release.wait(5)
                return b""

        process = types.SimpleNamespace(stdout=SilentStdout(), poll=lambda: None)
        runtime = workbuddy.WorkBuddyRuntime(tempfile.mkdtemp())
        outcome: dict[str, object] = {}

        def read() -> None:
            outcome["value"] = runtime._read_ready(process)

        with mock.patch.object(workbuddy, "_WORKBUDDY_START_TIMEOUT_SECONDS", 0.05):
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            reader.join(2.0)
        release.set()
        self.assertFalse(reader.is_alive(), "the readiness read outlived its deadline")
        self.assertIsNone(outcome.get("value"))


if __name__ == "__main__":
    unittest.main()
