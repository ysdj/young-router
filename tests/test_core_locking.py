"""One domain's slow write must not queue another domain's, and a probe is a job.

The store used to serialize every mutation on one ``_mutation`` lock, so a
probe of a station, a relay sign-in, or a WebDAV round trip held the lane for
the whole network call: an unrelated ``add`` waited for it, and the native
host's 900 s probe budget was spent inside a normal dispatch.  These tests pin
the two halves of the fix at the boundary a window sees:

* a write in one domain answers while another domain is blocked, and
* a probe answers with a job id at once, its result arriving as an event.

``snapshot``'s own contract -- a reader gets the last committed view instead of
waiting for a write -- is asserted here too, because the per-domain lanes are
what could regress it.
"""

from __future__ import annotations

import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import yaml

from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.service import CoreStore, MemoryDomain


class _BlockingDomain(MemoryDomain):
    """A domain whose one action stands in for a slow upstream call."""

    def __init__(self, name: str, action: str = "slow_write") -> None:
        super().__init__(name)
        self.action = action
        self.entered = threading.Event()
        self.release = threading.Event()

    def dispatch(self, action: str, payload: object | None = None):
        if action == self.action:
            self.entered.set()
            self.release.wait(30.0)
            return super().dispatch("set", payload)
        return super().dispatch(action, payload)


class _ProbeDomain(MemoryDomain):
    """A domain whose probe answers with the payload it was asked about."""

    def __init__(self, name: str = "providers_models", *, release: threading.Event | None = None) -> None:
        super().__init__(name)
        self.started = threading.Event()
        self.release = release
        self.calls = 0

    def probe(self, payload=None):
        self.calls += 1
        self.started.set()
        if self.release is not None:
            self.release.wait(30.0)
        return {
            "ok": True,
            "protocols": ["test"],
            "detail": "probe answered",
            "payload": dict(payload or {}) if isinstance(payload, dict) else {},
        }

    def dispatch(self, action: str, payload: object | None = None):
        if action == "model.delete":
            self._draft["route_deleted"] = True
            self.revision += 1
            return self.snapshot()
        return super().dispatch(action, payload)


class _RelayFixture(MemoryDomain):
    """The relay half of a removal: it reports the resources it retired."""

    def __init__(self) -> None:
        super().__init__("relay_accounts")
        self.actions: list[str] = []
        self.lanes: list[dict[str, object]] = []
        # A function is copied by reference, so recording the lanes through one
        # keeps the CoreStore itself out of this adapter's checkpointable state.
        self.lane_probe = _no_lanes

    def snapshot(self):
        return {
            "domain": "relay_accounts",
            "accounts": [],
            "stations": [],
            "last_action": {
                "resources": [
                    {"station_id": "station-1", "account_id": "account-1", "resource_id": "key-1"}
                ],
                "dependency_policy": "detach",
            },
        }

    def dispatch(self, action: str, payload: object | None = None):
        self.actions.append(action)
        self.lanes.append(self._lanes())
        self.revision += 1
        return self.snapshot()

    def _lanes(self) -> dict[str, object]:
        return self.lane_probe()


class _ProvidersFixture(MemoryDomain):
    """The provider half of a removal: it releases the linked slots."""

    def __init__(self) -> None:
        super().__init__("providers_models")
        self.released: list[tuple[list[object], str]] = []
        self.lanes: list[dict[str, object]] = []
        self.lane_probe = _no_lanes

    def apply_relay_dependency_policy(self, resources, policy, *, rebind=None):
        self.released.append((list(resources), str(policy)))
        self.lanes.append(self._lanes())
        self.revision += 1
        return {"issues": []}

    def _lanes(self) -> dict[str, object]:
        return self.lane_probe()


def _no_lanes() -> dict[str, object]:
    return {}


def _store(*domains: MemoryDomain, service_handlers=None) -> CoreStore:
    core = CoreStore(domains=list(domains), service_handlers=service_handlers)
    # A window always reads before it edits, and that first read is what
    # publishes the view a reader is answered with while a write is in flight.
    core.snapshot()
    return core


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class DomainLockTests(unittest.TestCase):
    def test_domain_lock_parallel(self) -> None:
        """A blocked providers write does not hold the runtime domain's lane."""

        providers = _BlockingDomain("providers_models")
        runtime = MemoryDomain("runtime", {"theme": "system"})
        core = _store(providers, runtime)
        self.addCleanup(core.stop_jobs)

        blocked = threading.Thread(
            target=lambda: core.dispatch(
                {"domain": "providers_models", "type": "slow_write", "payload": {"n": 1}}
            ),
            name="slow-providers-write",
            daemon=True,
        )
        blocked.start()
        self.assertTrue(providers.entered.wait(2.0), "the slow write never started")
        try:
            started = time.monotonic()
            response = core.dispatch(
                {"domain": "runtime", "type": "set", "payload": {"theme": "dark"}}
            )
            elapsed = time.monotonic() - started
        finally:
            providers.release.set()
            blocked.join(timeout=5.0)

        self.assertIn("revision", response)
        self.assertLess(elapsed, 0.3, f"the runtime write waited {elapsed:.3f}s for providers")

    def test_snapshot_answers_during_a_domain_write(self) -> None:
        """A reader gets the last committed view, never a half-written domain."""

        providers = _BlockingDomain("providers_models")
        core = _store(providers)
        self.addCleanup(core.stop_jobs)
        committed = core.revision

        blocked = threading.Thread(
            target=lambda: core.dispatch(
                {"domain": "providers_models", "type": "slow_write", "payload": {"n": 1}}
            ),
            name="slow-providers-write",
            daemon=True,
        )
        blocked.start()
        self.assertTrue(providers.entered.wait(2.0), "the slow write never started")
        try:
            started = time.monotonic()
            view = core.snapshot()
            elapsed = time.monotonic() - started
        finally:
            providers.release.set()
            blocked.join(timeout=5.0)

        self.assertLess(elapsed, 0.3, f"snapshot waited {elapsed:.3f}s for the write")
        self.assertEqual(committed, view["revision"])

    def test_a_blocked_write_does_not_freeze_the_revision_a_reader_sees(self) -> None:
        """The projection may lag a write; the revision may not.

        A snapshot answered while another thread holds the lane carries the last
        committed projection — that is the point of it.  It used to carry that
        projection's revision too, so a reader believed Core still sat where the
        projection did.  A pane re-sent that number as its Apply for as long as
        a proxy reload held the lane, and every attempt was refused as a
        conflict for a change that had already happened: the strip reported
        更改未生效 over a write that landed before the first retry.
        """

        providers = _BlockingDomain("providers_models")
        runtime = MemoryDomain("runtime", {"theme": "system"})
        core = _store(providers, runtime)
        self.addCleanup(core.stop_jobs)
        committed = core.revision

        blocked = threading.Thread(
            target=lambda: core.dispatch(
                {"domain": "providers_models", "type": "slow_write", "payload": {"n": 1}}
            ),
            name="slow-providers-write",
            daemon=True,
        )
        blocked.start()
        self.assertTrue(providers.entered.wait(2.0), "the slow write never started")
        try:
            # A write on another domain still advances the shared revision.
            core.dispatch({"domain": "runtime", "type": "set", "payload": {"theme": "dark"}})
            view = core.snapshot()
            # The blocked write may still land after this; compare against the
            # revision Core stated while the reader was answered.
            during = core.revision
        finally:
            providers.release.set()
            blocked.join(timeout=5.0)

        self.assertGreater(during, committed)
        # The reader is told where Core is now, not where the projection it was
        # answered with had stopped.
        self.assertEqual(during, view["revision"])
        # The projection itself still lags the write in flight — that is what
        # this view is for — while the draft state carried beside it is the
        # current one.  Its own revision travels beside the head: a window
        # chains patches on the state it holds, and that state is committed.
        self.assertEqual("system", view["domains"]["runtime"]["state"]["theme"])
        self.assertTrue(view["drafts"]["runtime"]["dirty"])
        self.assertEqual(committed, view["projected_revision"])
        self.assertLess(view["projected_revision"], view["revision"])
        # A view with no foreign write in the way is its own projection, and
        # the two numbers agree again.
        self.assertTrue(
            _wait_for(lambda: core.projected_revision == core.revision),
            "the projection never caught up with the head it had already stated",
        )
        settled = core.snapshot()
        self.assertEqual(settled["revision"], settled["projected_revision"])
        self.assertEqual("dark", settled["domains"]["runtime"]["state"]["theme"])

    def test_cross_domain_declared(self) -> None:
        """A relay removal still releases the provider slots, in one transaction."""

        relay = _RelayFixture()
        providers = _ProvidersFixture()
        core = _store(relay, providers)
        self.addCleanup(core.stop_jobs)

        def lanes() -> dict[str, object]:
            return {
                "mutation_depth": core._mutation_depth,
                "providers_models": core._domain_lock("providers_models")._is_owned(),
                "relay_accounts": core._domain_lock("relay_accounts")._is_owned(),
            }

        relay.lane_probe = lanes
        providers.lane_probe = lanes

        self.assertEqual(
            ("providers_models", "relay_accounts"),
            core._dispatch_domain_set("relay_accounts", "station_remove"),
        )
        response = core.dispatch(
            {"domain": "relay_accounts", "type": "station_remove", "payload": {"id": "account-1"}}
        )

        self.assertIn("station_remove", relay.actions)
        self.assertEqual(1, len(providers.released), "the linked provider slots were not released")
        resources, policy = providers.released[0]
        self.assertEqual("key-1", resources[0]["resource_id"])
        self.assertEqual("detach", policy)
        self.assertIn("revision", response)
        # The action declared both domains, so it took the transaction lane and
        # both write lanes before it touched either adapter.
        for lanes in relay.lanes + providers.lanes:
            self.assertGreater(int(lanes["mutation_depth"]), 0)
            self.assertTrue(lanes["providers_models"])
            self.assertTrue(lanes["relay_accounts"])


class ProbeJobTests(unittest.TestCase):
    def test_probe_returns_job(self) -> None:
        """A probe answers with a job id, and its result arrives as an event."""

        domain = _ProbeDomain("providers_models")
        core = _store(domain)
        self.addCleanup(core.stop_jobs)
        events: list[dict[str, object]] = []
        unsubscribe = core.subscribe(events.append)
        self.addCleanup(unsubscribe)

        started = time.monotonic()
        response = core.dispatch(
            {
                "domain": "providers_models",
                "type": "probe",
                "payload": {"provider_id": "primary", "model_id": "default-chat"},
            }
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.3, f"the probe dispatch answered in {elapsed:.3f}s")
        self.assertIn("job_id", response)
        self.assertIsInstance(response["job_id"], str)
        self.assertTrue(response["job_id"])
        job_id = str(response["job_id"])
        self.assertTrue(
            _wait_for(lambda: any(event.get("job_id") == job_id and event.get("state") == "running" for event in events)),
            "no running job event was published",
        )
        self.assertTrue(
            _wait_for(lambda: any(event.get("job_id") == job_id and event.get("state") == "done" for event in events)),
            "the probe job never finished",
        )
        done = next(event for event in events if event.get("job_id") == job_id and event.get("state") == "done")
        result = done["result"]
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertTrue(result["ok"])
        self.assertEqual(["test"], result["protocols"])
        self.assertEqual("primary", result["payload"]["provider_id"])
        self.assertEqual(1, domain.calls)

    def test_probe_does_not_block_dispatch(self) -> None:
        """Five ordinary writes land while a probe is still reading."""

        release = threading.Event()
        domain = _ProbeDomain("providers_models", release=release)
        runtime = MemoryDomain("runtime", {"theme": "system"})
        core = _store(domain, runtime)
        self.addCleanup(core.stop_jobs)
        events: list[dict[str, object]] = []
        unsubscribe = core.subscribe(events.append)
        self.addCleanup(unsubscribe)

        response = core.dispatch({"domain": "providers_models", "type": "probe", "payload": {}})
        job_id = str(response["job_id"])
        self.assertTrue(domain.started.wait(2.0), "the probe never started")
        try:
            for index in range(5):
                started = time.monotonic()
                core.dispatch({"domain": "runtime", "type": "set", "payload": {"n": index}})
                elapsed = time.monotonic() - started
                self.assertLess(
                    elapsed,
                    0.3,
                    f"add {index} waited {elapsed:.3f}s behind the probe",
                )
            # The probe is still inside its own read: nothing above paused it.
            self.assertFalse(
                any(event.get("job_id") == job_id and event.get("state") == "done" for event in events),
                "the probe finished before its release",
            )
        finally:
            release.set()
        self.assertTrue(
            _wait_for(lambda: any(event.get("job_id") == job_id and event.get("state") == "done" for event in events)),
            "the probe job never finished",
        )

    def test_a_same_domain_delete_does_not_wait_for_a_legacy_probe(self) -> None:
        """A route edit is still immediate for a combined probe adapter."""

        release = threading.Event()
        domain = _ProbeDomain("providers_models", release=release)
        core = _store(domain)
        self.addCleanup(core.stop_jobs)
        events: list[dict[str, object]] = []
        unsubscribe = core.subscribe(events.append)
        self.addCleanup(unsubscribe)

        response = core.dispatch({"domain": "providers_models", "type": "probe", "payload": {}})
        job_id = str(response["job_id"])
        self.assertTrue(domain.started.wait(2.0), "the probe never started")
        try:
            started = time.monotonic()
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.delete",
                    "payload": {"model_id": "route-under-test"},
                }
            )
            elapsed = time.monotonic() - started
        finally:
            release.set()

        self.assertLess(elapsed, 0.3, f"the route delete waited {elapsed:.3f}s behind the probe")
        self.assertTrue(
            _wait_for(lambda: any(event.get("job_id") == job_id and event.get("state") == "done" for event in events)),
            "the probe job never finished",
        )

    def test_provider_catalog_read_does_not_hold_the_route_lane(self) -> None:
        """Deleting and saving a route finish before its provider's listing."""

        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(2) as pool:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump({
                "providers": {"fixture": {"api_base": "https://example.test/v1", "api_keys": [{"name": "default", "value": "sk-replace-test"}]}},
                "model_list": [{
                    "model_name": "route-under-test",
                    "litellm_params": {"model": "openai/route-under-test", "api_base": "https://example.test/v1", "api_key": "sk-replace-test"},
                    "model_info": {"id": "00000001", "provider": "fixture"},
                }],
            }), encoding="utf-8")
            providers = ProvidersModelsDomain(path)
            row = providers.snapshot()["providers"][0]
            core = _store(providers)

            def read(*args):
                entered.set()
                self.assertTrue(release.wait(5))
                return {**args[2], "available": True, "models": ["route-under-test"], "model_count": 1}

            def remove():
                staged = core.dispatch({"domain": "providers_models", "type": "model.delete", "payload": {
                    "provider_id": row["id"], "model_id": row["models"][0]["editor_id"],
                }})
                return core.apply("providers_models", revision=staged["revision"])

            with mock.patch.object(providers, "_read_provider_models", side_effect=read):
                listing = pool.submit(core.dispatch, {
                    "domain": "providers_models", "type": "providers.fetch_models", "payload": {"provider_id": row["id"]},
                })
                try:
                    self.assertTrue(entered.wait(2), "the catalog read never started")
                    self.assertTrue(pool.submit(remove).result(2)["applied"])
                    self.assertFalse(listing.done())
                    self.assertEqual([], yaml.safe_load(path.read_text())["model_list"])
                finally:
                    release.set()
                self.assertEqual(["route-under-test"], listing.result(2)["action_summary"]["models"])
                self.assertEqual([], core.snapshot()["domains"]["providers_models"]["providers"][0]["models"])


class ServiceRowProbeTests(unittest.TestCase):
    def test_a_status_probe_is_not_on_the_dispatch_path(self) -> None:
        """A commit answers without running the proxy's health probe.

        ``_request_emit`` used to call the controller's own ``status`` handler,
        which is a real loopback health request.  Every dispatch therefore spent
        that read inside its write lane.  The publish path owns it now: the
        dispatch answers immediately and the row is refreshed by the event it
        asked for.
        """

        calls: list[str] = []

        def status(operation: str) -> dict[str, object]:
            calls.append(operation)
            if operation == "status":
                time.sleep(0.2)
            return {"state": "running", "port": 41234}

        runtime = MemoryDomain("runtime", {"theme": "system"})
        core = _store(runtime, service_handlers={"status": status})
        self.addCleanup(core.stop_jobs)
        events: list[dict[str, object]] = []
        unsubscribe = core.subscribe(events.append)
        self.addCleanup(unsubscribe)
        # Simulate a row left behind by an earlier commit: the dispatch has to
        # be the thing whose publish refreshes it.
        with core._lock:
            core._service = {"state": "unknown"}
        calls.clear()

        started = time.monotonic()
        core.dispatch({"domain": "runtime", "type": "set", "payload": {"theme": "dark"}})
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.3, f"the dispatch paid {elapsed:.3f}s for a status probe")
        self.assertEqual([], calls, "the dispatch ran the controller's health probe inline")
        self.assertTrue(_wait_for(lambda: len(events) > 0), "no state event was published")
        self.assertTrue(
            _wait_for(lambda: core._service.get("state") == "running"),
            "the published event did not refresh the service row",
        )


if __name__ == "__main__":
    unittest.main()
