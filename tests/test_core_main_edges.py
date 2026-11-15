"""Shutdown edges of the Core process entry point.

``run`` owns loops that keep working after startup, so what it stops on the way
out is as much a part of its contract as what it starts.
"""

from __future__ import annotations

import unittest
from unittest import mock

from young_router.core import __main__ as core_main


class FakeScheduler:
    """A background loop that records the calls ``run`` makes to it."""

    def __init__(self, events: list[str], name: str) -> None:
        self._events = events
        self._name = name

    def start(self) -> None:
        self._events.append(f"{self._name}.start")

    def stop(self) -> None:
        self._events.append(f"{self._name}.stop")


class FakeCore:
    """The loops every store owns, the optional one included."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def webdav_scheduler(self) -> FakeScheduler:
        return FakeScheduler(self._events, "webdav")

    def image_context_sweeper(self) -> FakeScheduler:
        return FakeScheduler(self._events, "sweeper")

    def client_model_sync(self) -> FakeScheduler:
        return FakeScheduler(self._events, "model_sync")

    def shutdown(self, *, stopped_by: str = "app_exit") -> None:
        self._events.append("core.shutdown")


class FakeServer:
    def __init__(self, _core: object, *, address: str, port: int, web_sessions_path: object = None) -> None:
        self.address = address
        self.port = port
        self.web_sessions_path = web_sessions_path

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class StopAtOnce:
    """The stop event, which answers its own wait so ``run`` returns at once."""

    def __init__(self) -> None:
        self._stopped = False

    def is_set(self) -> bool:
        return self._stopped

    def set(self) -> None:
        self._stopped = True

    def wait(self, _timeout: object = None) -> bool:
        self._stopped = True
        return True


def run_core(argv: list[str] | None = None) -> list[str]:
    """Run ``run`` against a fake store and return the loop calls it made."""

    events: list[str] = []
    with (
        mock.patch.object(core_main.CoreStore, "with_default_domains", return_value=FakeCore(events)),
        mock.patch.object(core_main, "CoreIPCServer", FakeServer),
        mock.patch.object(core_main, "_packaged_web_host", return_value=None),
        mock.patch.object(core_main.signal, "signal"),
        mock.patch.object(core_main.threading, "Event", StopAtOnce),
    ):
        assert core_main.run(argv or []) == 0
    return events


class CoreMainShutdownTests(unittest.TestCase):
    def test_core_stops_the_client_model_sync_loop_when_it_exits(self) -> None:
        events = run_core()

        self.assertIn("model_sync.start", events)
        self.assertIn("model_sync.stop", events)


if __name__ == "__main__":
    unittest.main()
