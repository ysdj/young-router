"""Publishing a commit is not part of the commit.

A write path used to project the whole store and fan the event out inside its
own mutation lane, so an edit's response was timed by a cold projection plus
one copy per open window.  These tests pin the two halves of the split: the
response no longer projects, and a burst of revisions is one event carrying the
last of them.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.emitter import MAX_WINDOW_SECONDS, CoalescingEmitter
from young_router.core.ipc import MAX_EVENTS, CoreIPCClient, CoreIPCServer
from young_router.core.service import CoreStore, MemoryDomain


def _wait_for_events(core: CoreStore, events: list[dict[str, object]], count: int, timeout: float = 5.0) -> None:
    """Let the emitter publish until ``events`` holds ``count`` of them."""

    deadline = time.monotonic() + timeout
    while len(events) < count and time.monotonic() < deadline:
        core._emitter.flush()
        time.sleep(0.005)
    if len(events) < count:
        raise AssertionError(f"the store published {len(events)} events, expected {count}")


def _dispatch_provider_add(core: CoreStore) -> dict[str, object]:
    return core.dispatch(
        {
            "domain": "providers_models",
            "type": "provider.add",
            "payload": {
                "provider": {
                    "name": "primary",
                    "api_base": "https://example.test/v1",
                    "models": [
                        {
                            "model_name": "public-route",
                            "litellm_model": "openai/upstream-route",
                        }
                    ],
                }
            },
        }
    )


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

    def test_patch_contains_upsert(self) -> None:
        """An added provider travels as its own row, not as the projection.

        This is the payload a window pays for an ``add``: the first event is
        still the full state (a subscription starts from nothing), and every
        event after it names only what moved.
        """

        with tempfile.TemporaryDirectory() as directory:
            core = CoreStore(domains=[ProvidersModelsDomain(Path(directory) / "config.yaml")])
            events: list[dict[str, object]] = []
            unsubscribe = core.subscribe(events.append)
            self.addCleanup(unsubscribe)

            core._emit()
            self.assertEqual("snapshot", events[0]["event"])

            _dispatch_provider_add(core)
            _wait_for_events(core, events, 2)

            event = events[-1]
            self.assertEqual("patch", event["event"])
            self.assertEqual(["providers_models"], event["domains"])
            upserts = [op for op in event["ops"] if op["kind"] == "upsert"]
            provider_upserts = [op for op in upserts if str(op.get("id", "")).startswith("provider:")]
            self.assertEqual(1, len(provider_upserts), f"the added provider was not named: {event['ops']}")
            self.assertEqual("provider:primary", provider_upserts[0]["id"])
            self.assertEqual("primary", provider_upserts[0]["value"]["name"])
            payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
            self.assertLess(len(payload), 4 * 1024, f"the add patch is {len(payload)} bytes")

    def test_patch_contains_remove(self) -> None:
        """A deleted row is named, so no window keeps drawing it."""

        with tempfile.TemporaryDirectory() as directory:
            core = CoreStore(domains=[ProvidersModelsDomain(Path(directory) / "config.yaml")])
            events: list[dict[str, object]] = []
            unsubscribe = core.subscribe(events.append)
            self.addCleanup(unsubscribe)

            core._emit()
            _dispatch_provider_add(core)
            _wait_for_events(core, events, 2)
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.delete",
                    "payload": {"provider_id": "primary"},
                }
            )
            _wait_for_events(core, events, 3)

            event = events[-1]
            self.assertEqual("patch", event["event"])
            removed = [op for op in event["ops"] if op["kind"] == "remove"]
            self.assertIn("provider:primary", [op.get("id") for op in removed])
            self.assertNotIn("value", removed[0], "a removed row still carried a value")

    def test_patch_base_revision_chain(self) -> None:
        """Each patch starts where the previous one ended."""

        core = CoreStore(domains=[MemoryDomain("runtime", {"choice": "system"})])
        events: list[dict[str, object]] = []
        unsubscribe = core.subscribe(events.append)
        self.addCleanup(unsubscribe)

        core._emit()
        for choice in ("en", "zh"):
            core.dispatch({"domain": "runtime", "type": "set", "payload": {"choice": choice}})
            _wait_for_events(core, events, len(events) + 1)

        snapshot, first, second = events[0], events[1], events[2]
        self.assertEqual("snapshot", snapshot["event"])
        self.assertEqual(snapshot["revision"], first["base_revision"])
        self.assertEqual(first["revision"], second["base_revision"])
        self.assertEqual(core.revision, second["revision"])

    def test_overflow_resync(self) -> None:
        """A queue that cannot hold an event answers with a resync marker.

        Dropping the oldest event silently left a window applying patches on a
        revision it never reached.  The queue now refuses the event, marks the
        subscription, and the next read of it answers ``resync`` so the window
        can read the whole state instead.
        """

        core = CoreStore(domains=[MemoryDomain("runtime", {"choice": "system"})])
        server = CoreIPCServer(core)
        endpoint = server.start()
        self.addCleanup(server.stop)
        client = CoreIPCClient(endpoint, server.bootstrap_token)
        self.addCleanup(client.close)
        session = client._session_token
        subscription_id = server._new_subscription(session)
        queued = {"protocol_version": 1, "event": "patch", "revision": 0, "ops": []}
        for _ in range(MAX_EVENTS):
            server._publish(queued)
        with server._lock:
            subscription = server._subscriptions[subscription_id]
        self.assertEqual(MAX_EVENTS, subscription.queue.qsize())

        # The queue is full: the new patch is refused, not swapped in for the
        # oldest one, and the subscription is marked for a full read.
        server._publish(queued)
        self.assertEqual(MAX_EVENTS, subscription.queue.qsize(), "a queued event was dropped")
        self.assertTrue(subscription.needs_resync)

        event = server.next_event(subscription_id, session_token=session, timeout=0)
        assert event is not None
        self.assertEqual("resync", event["event"])
        self.assertEqual(core.revision, event["revision"])
        self.assertFalse(subscription.needs_resync)
        self.assertEqual(0, subscription.queue.qsize(), "stale events survived the resync")

        # The subscription is usable again after the read it was owed.
        server._publish({**queued, "event": "snapshot"})
        following = server.next_event(subscription_id, session_token=session, timeout=0)
        assert following is not None
        self.assertEqual("snapshot", following["event"])


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
