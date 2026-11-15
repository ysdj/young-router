from __future__ import annotations

import os
import tempfile
import unittest

from young_router import log_rotation


class ManagedStreamPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.log_path = os.path.join(self._tmpdir.name, "server.log")
        self._previous = os.environ.get("YOUNG_ROUTER_SERVICE_LOG")
        os.environ["YOUNG_ROUTER_SERVICE_LOG"] = self.log_path
        self.addCleanup(self._restore_environment)

    def _restore_environment(self) -> None:
        if self._previous is None:
            os.environ.pop("YOUNG_ROUTER_SERVICE_LOG", None)
        else:
            os.environ["YOUNG_ROUTER_SERVICE_LOG"] = self._previous

    def test_managed_stream_path_is_resolved_once_per_write(self) -> None:
        calls: list[object] = []
        original = log_rotation._stream_path

        def counting(stream: object) -> str | None:
            calls.append(stream)
            return original(stream)

        log_rotation._stream_path = counting
        self.addCleanup(setattr, log_rotation, "_stream_path", original)
        with open(self.log_path, "w", encoding="utf-8") as handle:
            log_rotation.write_bounded_stream(handle, "hello\n")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
