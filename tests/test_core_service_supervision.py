"""The proxy is restored by Core, never by a person pressing 重启服务.

Two failure modes are pinned here.  A recorded child inside its own boot window
is *starting*, not unhealthy: the LiteLLM import alone takes tens of seconds,
and every window polling in that gap used to be shown a recovery button for a
proxy that was already on its way up.  And a settled failure -- an owned master
whose listener is gone, or a start that died -- is recovered by Core's own
supervisor, because nothing about that state is a decision the user made.
"""

from __future__ import annotations

from pathlib import Path
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from young_router.core import CoreStore
from young_router.core.operations import CoreServiceController, SERVICE_STARTUP_GRACE_SECONDS
from young_router.core.service import (
    SERVICE_RECOVERY_PATIENCE_SECONDS,
    SERVICE_RECOVERY_RETRY_SECONDS,
)
from young_router.core.service_supervisor import ServiceSupervisor


def _service_handlers(status: dict[str, object], calls: list[str]) -> dict[str, object]:
    """Handlers whose probe answers ``status`` and whose operations answer running."""

    def probe(operation: str) -> dict[str, object]:
        assert operation in {"health", "status"}, operation
        return dict(status)

    def operate(operation: str) -> dict[str, object]:
        calls.append(operation)
        if operation == "stop":
            return {"state": "stopped"}
        return {"state": "running", "pid": 2, "port": 49173}

    return {"health": probe, "start": operate, "restart": operate, "stop": operate}


class ServiceStartupGraceTests(unittest.TestCase):
    def test_a_child_inside_its_boot_window_is_starting_not_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            controller.paths.pid.write_text("4242\n", encoding="utf-8")

            with mock.patch.object(controller, "_pid", return_value=4242), mock.patch.object(
                controller, "_health", return_value=False
            ):
                self.assertEqual("starting", controller.status()["state"])

                # The same unanswered probe, after the boot window, is the
                # settled failure the supervisor exists to repair.
                stale = time.time() - SERVICE_STARTUP_GRACE_SECONDS - 1
                os.utime(controller.paths.pid, (stale, stale))
                self.assertEqual("unhealthy", controller.status()["state"])

    def test_start_never_launches_a_second_proxy_over_a_booting_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            controller.paths.pid.write_text("4242\n", encoding="utf-8")

            with mock.patch.object(controller, "_pid", return_value=4242), mock.patch.object(
                controller, "_health", return_value=False
            ), mock.patch("young_router.core.operations.subprocess.Popen") as popen:
                answer = controller.start()

            self.assertEqual("starting", answer["state"])
            popen.assert_not_called()


class OrphanedProxyGroupTests(unittest.TestCase):
    """A master can die before the tree it started does.

    The forkservers and workers keep the listening socket they inherited, so
    the port still answers while the pid the owner record names is gone.  That
    used to project as 未知 -- a state with no control in the pane and no
    cleanup anywhere -- over a proxy this installation had started itself.
    """

    def _record(self, directory: str) -> object:
        from young_router.core.operations import ServiceOwnerRecord

        return ServiceOwnerRecord(
            pid=4242,
            identity="posix-lstart-sha256:test",
            token="t" * 64,
            core_pid=1,
            core_identity="posix-lstart-sha256:core",
        )

    def test_a_surviving_group_of_a_dead_master_projects_as_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            with mock.patch.object(controller, "_recorded_pid", return_value=None), mock.patch.object(
                controller, "_read_owner_record", return_value=self._record(directory)
            ), mock.patch.object(controller, "_process_group_members", return_value=[4243, 4244]), mock.patch.object(
                controller, "_posix_process_has_token", return_value=True
            ), mock.patch.object(controller, "_health", return_value=True):
                self.assertEqual("stopped", controller.status()["state"])

    def test_a_foreign_group_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            with mock.patch.object(controller, "_recorded_pid", return_value=None), mock.patch.object(
                controller, "_read_owner_record", return_value=self._record(directory)
            ), mock.patch.object(controller, "_process_group_members", return_value=[4243, 4244]), mock.patch.object(
                controller, "_posix_process_has_token", return_value=False
            ), mock.patch.object(controller, "_health", return_value=True):
                # Somebody else's listener on the configured port: no ownership
                # evidence, so it is never adopted or stopped.
                self.assertEqual("unknown", controller.status()["state"])

    def test_start_ends_the_surviving_group_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            controller.paths.pid.write_text("4242\n", encoding="utf-8")
            replacement = mock.Mock(pid=9999)
            replacement.poll.return_value = None

            with mock.patch.object(
                controller,
                "status",
                side_effect=[{"state": "stopped"}, {"state": "stopped"}, {"state": "running"}],
            ), mock.patch.object(controller, "_recorded_pid", return_value=None), mock.patch.object(
                controller, "_read_owner_record", return_value=self._record(directory)
            ), mock.patch.object(controller, "_process_group_members", return_value=[4243]), mock.patch.object(
                controller, "_posix_process_has_token", return_value=True
            ), mock.patch.object(controller, "_stop_process_group") as stop_group, mock.patch.object(
                controller, "_stage_runtime_config"
            ), mock.patch.object(
                controller, "_runtime_env", return_value={"LITELLM_HEALTH_WAIT_SECONDS": "1"}
            ), mock.patch.object(controller, "_configured_port", return_value=4000), mock.patch(
                "young_router.core.operations.subprocess.Popen", return_value=replacement
            ), mock.patch.object(
                controller, "_write_owner_record"
            ), mock.patch.object(
                controller, "_health", return_value=True
            ):
                controller.start()

            stop_group.assert_called_once_with(4242)
            self.assertEqual("9999\n", controller.paths.pid.read_text(encoding="utf-8"))

    def test_a_stop_reaches_a_group_whose_leader_is_gone(self) -> None:
        """The group outlives its leader, and a stop still ends all of it.

        The member ignores SIGTERM deliberately: this stop is a kill, so the
        signal it ignores is not the one that ends it.  What the stop must not do
        is wait on a leader that is already gone -- the group is the subject.
        """

        code = (
            "import subprocess, sys, time;"
            " subprocess.Popen([sys.executable, '-c',"
            " 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']);"
            " time.sleep(60)"
        )
        leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        self.addCleanup(lambda: leader.poll() is None and leader.kill())
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and len(controller_members(leader.pid)) < 2:
                time.sleep(0.05)
            # The leader dies first: the member is what is left to stop.
            os.kill(leader.pid, signal.SIGKILL)
            leader.wait(timeout=5.0)
            started = time.monotonic()
            CoreServiceController._stop_process_group(leader.pid)
            elapsed = time.monotonic() - started
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and controller_members(leader.pid):
                time.sleep(0.05)
            self.assertEqual([], controller_members(leader.pid))
            self.assertLess(elapsed, 5.0, "the stop waited on a leader that was already gone")
        finally:
            if leader.poll() is None:
                leader.kill()


def controller_members(pgid: int) -> list[int]:
    return [pid for pid in CoreServiceController._process_group_members(pgid)]


class ServiceSupervisionTests(unittest.TestCase):
    def test_a_stopped_proxy_is_started_without_a_window(self) -> None:
        calls: list[str] = []
        core = CoreStore(service_handlers=_service_handlers({"state": "stopped"}, calls))
        events: list[dict[str, object]] = []
        unsubscribe = core.subscribe(events.append)
        self.addCleanup(unsubscribe)

        self.assertTrue(core.supervise_service_once())

        self.assertEqual(["start"], calls)
        self.assertEqual("running", core.snapshot()["service"]["state"])
        self.assertIn(
            "starting",
            [event["snapshot"]["service"]["state"] for event in events],
            "windows must see the automatic start as a transition, not as the old failure",
        )

    def test_an_explicit_stop_is_never_undone(self) -> None:
        calls: list[str] = []
        core = CoreStore(service_handlers=_service_handlers({"state": "stopped"}, calls))
        core.dispatch({"type": "service.stop"})

        self.assertFalse(core.supervise_service_once())
        self.assertNotIn("start", calls)

    def test_the_stop_decision_survives_a_replacement_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "core-state.json"
            calls: list[str] = []
            first = CoreStore(
                metadata_path=metadata,
                service_handlers=_service_handlers({"state": "stopped"}, calls),
            )
            first.dispatch({"type": "service.stop"})

            replacement = CoreStore(
                metadata_path=metadata,
                service_handlers=_service_handlers({"state": "stopped"}, calls),
            )
            self.assertFalse(replacement.supervise_service_once())
            self.assertNotIn("start", calls)

    def test_an_unhealthy_proxy_is_replaced_after_its_patience(self) -> None:
        calls: list[str] = []
        core = CoreStore(service_handlers=_service_handlers({"state": "unhealthy"}, calls))

        with mock.patch("young_router.core.service.time.monotonic", return_value=1000.0):
            self.assertFalse(core.supervise_service_once())
        self.assertEqual([], calls, "one missed probe is not a wedged proxy")

        with mock.patch(
            "young_router.core.service.time.monotonic",
            return_value=1000.0 + SERVICE_RECOVERY_PATIENCE_SECONDS + 1,
        ):
            self.assertTrue(core.supervise_service_once())
        self.assertEqual(["restart"], calls)
        self.assertEqual("running", core.snapshot()["service"]["state"])

    def test_a_foreign_listener_is_never_touched(self) -> None:
        calls: list[str] = []
        core = CoreStore(service_handlers=_service_handlers({"state": "unknown"}, calls))

        with mock.patch(
            "young_router.core.service.time.monotonic",
            return_value=1000.0 + SERVICE_RECOVERY_PATIENCE_SECONDS + 1,
        ):
            self.assertFalse(core.supervise_service_once())

        self.assertEqual([], calls)

    def test_a_running_probe_resets_the_patience(self) -> None:
        calls: list[str] = []
        statuses = [{"state": "unhealthy"}, {"state": "running", "pid": 1, "port": 49173}]
        probes = iter(statuses)

        def probe(operation: str) -> dict[str, object]:
            return dict(next(probes))

        core = CoreStore(service_handlers={"health": probe, "restart": lambda op: {"state": "running"}})
        with mock.patch("young_router.core.service.time.monotonic", return_value=1000.0):
            self.assertFalse(core.supervise_service_once())
        with mock.patch("young_router.core.service.time.monotonic", return_value=2000.0):
            self.assertFalse(core.supervise_service_once())
        self.assertIsNone(core._service_supervision_unhealthy_since)
        self.assertEqual([], calls)

    def test_a_failed_recovery_waits_on_the_retry_ladder(self) -> None:
        attempts: list[str] = []

        def handler(operation: str) -> dict[str, object]:
            if operation in {"health", "status"}:
                return {"state": "stopped"}
            attempts.append(operation)
            raise RuntimeError("LiteLLM service could not start")

        core = CoreStore(service_handlers={"health": handler, "status": handler, "start": handler})
        first = 1000.0
        with mock.patch("young_router.core.service.time.monotonic", return_value=first):
            self.assertTrue(core.supervise_service_once())
        self.assertEqual(["start"], attempts)
        self.assertEqual("stopped", core.snapshot()["service"]["state"])

        delay = SERVICE_RECOVERY_RETRY_SECONDS[0]
        with mock.patch("young_router.core.service.time.monotonic", return_value=first + delay - 1):
            self.assertFalse(core.supervise_service_once())
        self.assertEqual(["start"], attempts, "the ladder must not spin on a failing start")

        with mock.patch("young_router.core.service.time.monotonic", return_value=first + delay + 1):
            self.assertTrue(core.supervise_service_once())
        self.assertEqual(["start", "start"], attempts)

    def test_a_supervised_start_announces_its_transition(self) -> None:
        seen: list[str] = []

        def handler(operation: str) -> dict[str, object]:
            if operation == "status":
                return {"state": "stopped"}
            seen.append(core.snapshot()["service"]["state"])
            return {"state": "running", "pid": 2, "port": 49173}

        core = CoreStore(service_handlers={"status": handler, "start": handler})
        self.assertTrue(core.supervise_service_once())

        self.assertEqual(["starting"], seen)

    def test_shutdown_leaves_no_pass_running(self) -> None:
        calls: list[str] = []
        core = CoreStore(service_handlers=_service_handlers({"state": "stopped"}, calls))

        core.shutdown(stopped_by="app_exit")

        self.assertFalse(core.supervise_service_once())
        self.assertNotIn("start", calls)


class ServiceSupervisorLoopTests(unittest.TestCase):
    def test_a_host_with_no_window_can_ask_for_an_immediate_first_pass(self) -> None:
        """The head start belongs to a window, not to every machine.

        The loop waits five seconds before its first pass so the app's own
        launch is not raced by a second start.  A unit that stated
        ``--manage-service`` has no window and therefore no other starter, so
        that wait is five seconds of a shut port: a replace on 2026-11-07 00:17
        spent exactly that inside a 31 s outage.  The store passes the
        caller's statement through instead of deciding it itself, because only
        the entry point knows whether a window exists.
        """

        core = CoreStore()

        default = core.service_supervisor()
        self.assertEqual(5.0, default._initial_delay)

        other = CoreStore().service_supervisor(initial_delay=0.0)
        self.assertEqual(0.0, other._initial_delay)

    def test_the_loop_keeps_passing_and_stops_on_request(self) -> None:
        passes: list[int] = []

        def supervise() -> None:
            passes.append(len(passes))

        loop = ServiceSupervisor(supervise=supervise, interval=1.0, initial_delay=0.0)
        loop.start()
        deadline = time.monotonic() + 5.0
        while len(passes) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        loop.stop()

        self.assertGreaterEqual(len(passes), 2)

    def test_a_failing_pass_never_stops_the_loop(self) -> None:
        passes: list[int] = []

        def supervise() -> None:
            passes.append(len(passes))
            raise RuntimeError("probe failed")

        loop = ServiceSupervisor(supervise=supervise, interval=1.0, initial_delay=0.0)
        loop.start()
        deadline = time.monotonic() + 5.0
        while len(passes) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        loop.stop()

        self.assertGreaterEqual(len(passes), 2)


if __name__ == "__main__":
    unittest.main()
