"""Publishing a commit is not part of the commit.

A write path used to project the whole store and fan the event out inside its
own mutation lane, so an edit's response was timed by a cold projection plus
one copy per open window.  These tests pin the two halves of the split: the
response no longer projects, and a burst of revisions is one event carrying the
last of them.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from young_router.core.emitter import MAX_WINDOW_SECONDS, CoalescingEmitter
from young_router.core.service import CoreStore, MemoryDomain


class CoreEmitTests(unittest.TestCase):
    def test_emit_outside_mutation_lane(self) -> None:
        """A slow publish never reaches the dispatch that asked for it.

        ``_emit`` is the thing that projects ten domains and encodes a
        snapshot for every window; 50 ms stands in for the hundreds of
        milliseconds a real installation measured.  Dispatch answers with its
        own revision, and the publish it asked for happens somewhere else.
        """

        core = CoreStore(domains=[MemoryDomain("runtime", {"choice": "system"})])
        core.snapshot()
        published = threading.Event()
        publisher_threads: list[threading.Thread] = []

        def slow_emit(snapshot: object | None = None) -> None:
            time.sleep(0.05)
            publisher_threads.append(threading.current_thread())
            published.set()

        with mock.patch.object(core, "_emit", slow_emit):
            started = time.perf_counter()
            answer = core.dispatch(
                {"domain": "runtime", "type": "set", "payload": {"choice": "en"}}
            )
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.1, "dispatch waited for its own event to be published")
            self.assertIn("revision", answer)
            self.assertTrue(published.wait(2.0), "the commit never published its event")
            self.assertIsNot(publisher_threads[-1], threading.current_thread())

    def test_emit_coalesces_burst(self) -> None:
        """Three revisions inside one window are one event."""

        core = CoreStore(domains=[MemoryDomain("runtime", {"choice": "system"})])
        events: list[dict[str, object]] = []
        unsubscribe = core.subscribe(events.append)
        self.addCleanup(unsubscribe)

        core._request_emit()
        core._request_emit()
        core._request_emit()
        core._emitter.flush()

        self.assertEqual(1, len(events), "the burst produced one event per revision")
        # The window thread wakes after a flush that already answered the
        # request; it must not publish the same revision a second time.
        time.sleep(0.1)
        self.assertEqual(1, len(events))
        self.assertEqual(core.revision, events[0]["revision"])

    def test_dispatch_response_does_not_project(self) -> None:
        """The response carries a revision, not a rebuilt projection."""

        core = CoreStore(domains=[MemoryDomain("runtime", {"choice": "system"})])
        # What a window does before it edits: the store holds a projection, so
        # the mutation scope has nothing to publish on its own.
        core.snapshot()
        events: list[dict[str, object]] = []
        unsubscribe = core.subscribe(events.append)
        self.addCleanup(unsubscribe)

        with mock.patch.object(
            core, "_project_domains", side_effect=core._project_domains
        ) as project:
            core.dispatch({"domain": "runtime", "type": "set", "payload": {"choice": "en"}})
            self.assertEqual(0, project.call_count, "the dispatch response projected the store")


class CoalescingEmitterTests(unittest.TestCase):
    def test_the_window_is_capped(self) -> None:
        """A caller may slow publishing down, but not past the ceiling."""

        emitter = CoalescingEmitter(lambda: None, window_seconds=5.0)
        self.assertEqual(MAX_WINDOW_SECONDS, emitter._window)
        self.addCleanup(emitter.stop)
        with self.assertRaises(ValueError):
            CoalescingEmitter(lambda: None, window_seconds=-0.001)

    def test_stop_publishes_what_the_last_request_left_pending(self) -> None:
        """A quit that lands right after a commit still sends that commit."""

        sent: list[int] = []
        emitter = CoalescingEmitter(lambda: sent.append(1), window_seconds=30.0)
        emitter.request()
        emitter.stop()
        self.assertEqual([1], sent)

        # A stopped emitter publishes nothing on its own, and does not restart.
        emitter.request()
        time.sleep(0.05)
        self.assertEqual([1], sent)


if __name__ == "__main__":
    unittest.main()
