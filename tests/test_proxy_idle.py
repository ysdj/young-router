"""The managed proxy must idle without a ten-hertz timer in every process."""

from __future__ import annotations

import asyncio
import inspect
import signal
import types
import unittest
from unittest import mock

import uvicorn.server
from uvicorn.supervisors import multiprocess as uvicorn_multiprocess

from young_router import lifecycle
from young_router.proxy import proxy_idle


class _FakeServerState:
    def __init__(self) -> None:
        self.default_headers: list[tuple[bytes, bytes]] = []
        self.connections: list[object] = []
        self.tasks: list[object] = []


class _FakeTransport:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.aborted = False
        self.connection = connection

    def abort(self) -> None:
        # A real transport reports ``connection_lost`` back to the protocol,
        # which is what removes the connection from the server's set.  Doing it
        # here keeps the test's wait loop honest: the connection leaves only
        # because ``abort`` was called, not because the fake gave up.
        self.aborted = True
        self.connection.on_connection_lost()


class _FakeConnection:
    """The connection slices uvicorn's shutdown touches."""

    def __init__(self, *, cycle: object, state: "_FakeServerState | None" = None) -> None:
        self.cycle = cycle
        self.transport = _FakeTransport(self)
        self.lost = False
        self.state = state

    def on_connection_lost(self) -> None:
        self.lost = True
        # uvicorn drops the connection from the server's set in its own
        # ``connection_lost``; the wait loop ends for that reason and no other.
        if self.state is not None and self in self.state.connections:
            self.state.connections.remove(self)

    def shutdown(self) -> None:
        # uvicorn calls this on every connection before the drain wait; the
        # test's own transport is closed by the patch under test.
        pass


class _FakeServeLoopServer:
    """The slices of ``uvicorn.server.Server`` that the serve loop touches."""

    def __init__(self, *, stop_after: int | None = None) -> None:
        self.should_exit = False
        self.force_exit = False
        self._captured_signals: list[int] = []
        self.last_notified = 0.0
        self.limit_max_requests = None
        self.config = types.SimpleNamespace(
            date_header=True,
            encoded_headers=[],
            callback_notify=None,
        )
        self.server_state = _FakeServerState()
        self.counters: list[int] = []
        self.dates: list[bytes] = []
        self._stop_after = stop_after

    async def on_tick(self, counter: int) -> bool:
        """Run upstream's own tick so the cadence under test is uvicorn's."""

        self.counters.append(counter)
        if self._stop_after is not None and len(self.counters) >= self._stop_after:
            self.should_exit = True
        should_exit = await uvicorn.server.Server.on_tick(self, counter)
        for name, value in self.server_state.default_headers:
            if name == b"date":
                self.dates.append(value)
        return should_exit


class UvicornIdleTickTests(unittest.TestCase):
    def setUp(self) -> None:
        proxy_idle.install_proxy_idle_patches()

    def test_the_patch_replaces_a_tenth_of_a_second_tick(self) -> None:
        """The counter unit stays uvicorn's, so its duties cannot drift."""

        installed = uvicorn.server.Server.main_loop
        self.assertTrue(getattr(installed, proxy_idle._IDLE_TICK_PATCH_ATTR, False))
        upstream_loop = inspect.getsource(installed._original_main_loop)
        upstream_tick = inspect.getsource(uvicorn.server.Server.on_tick)

        # uvicorn's own unit is a tenth of a second, and it rewrites the Date
        # header whenever the counter is divisible by ten: once a second.
        self.assertIn("asyncio.sleep(0.1)", upstream_loop)
        self.assertIn("counter % 10 == 0", upstream_tick)
        self.assertEqual(proxy_idle.IDLE_TICK_SECONDS, 1.0)
        # One idle tick therefore covers ten of uvicorn's own wakeups and still
        # lands on the counter value that refreshes the Date header.
        self.assertEqual(
            proxy_idle._TICK_COUNTER_STEP,
            int(round(proxy_idle.IDLE_TICK_SECONDS / 0.1)),
        )

    def test_idle_tick_keeps_every_second_of_upstream_work(self) -> None:
        server = _FakeServeLoopServer(stop_after=4)
        with mock.patch.object(proxy_idle, "_idle_tick_seconds", return_value=0.0):
            asyncio.run(uvicorn.server.Server.main_loop(server))

        # One tick per second of uvicorn's own counting, and every tick lands
        # on a Date refresh: the same once-a-second header, ten times fewer
        # timer wakeups.
        self.assertEqual(server.counters, [0, 10, 20, 30])
        self.assertEqual(len(server.dates), len(server.counters))
        self.assertEqual(len(set(server.dates)), 1)

    def test_stop_signal_ends_the_serve_loop_without_waiting_out_the_tick(self) -> None:
        async def scenario() -> None:
            server = _FakeServeLoopServer()
            task = asyncio.create_task(uvicorn.server.Server.main_loop(server))
            while not server.counters:
                await asyncio.sleep(0.005)
            # A launched stop reaches the patched handler while the loop waits.
            uvicorn.server.Server.handle_exit(server, signal.SIGTERM, None)
            await asyncio.wait_for(task, timeout=proxy_idle.IDLE_TICK_SECONDS / 2)

        asyncio.run(scenario())

    def test_install_is_idempotent_and_skips_a_foreign_uvicorn(self) -> None:
        installed = uvicorn.server.Server.main_loop
        proxy_idle._install_uvicorn_idle_tick_patch()
        self.assertIs(uvicorn.server.Server.main_loop, installed)

        self.addCleanup(setattr, uvicorn.server.Server, "main_loop", installed)
        del uvicorn.server.Server.main_loop
        proxy_idle._install_uvicorn_idle_tick_patch()
        self.assertFalse(hasattr(uvicorn.server.Server, "main_loop"))


class UvicornSupervisorHealthcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        proxy_idle.install_proxy_idle_patches()
        self.clock = [100.0]

    def worker(self) -> uvicorn_multiprocess.Process:
        process = uvicorn_multiprocess.Process.__new__(uvicorn_multiprocess.Process)
        process.config = types.SimpleNamespace(timeout_worker_healthcheck=5)
        process.process = mock.Mock(is_alive=mock.Mock(return_value=True))
        process.ping = mock.Mock(return_value=True)
        return process

    def test_liveness_stays_per_pass_while_the_ping_waits_for_its_interval(self) -> None:
        process = self.worker()
        with mock.patch.object(proxy_idle.time, "monotonic", side_effect=lambda: self.clock[0]):
            self.assertTrue(process.is_alive(timeout=5))
            self.assertEqual(process.ping.call_count, 1)

            # The operating system is asked on every supervisor pass, and the
            # inter-process round trip is skipped until its interval is up.
            self.clock[0] = 100.5
            self.assertTrue(process.is_alive(timeout=5))
            self.assertEqual(process.ping.call_count, 1)

            self.clock[0] = 100.0 + proxy_idle.SUPERVISOR_HEALTHCHECK_INTERVAL_SECONDS - 0.1
            self.assertTrue(process.is_alive(timeout=5))
            self.assertEqual(process.ping.call_count, 1)

            self.clock[0] = 100.0 + proxy_idle.SUPERVISOR_HEALTHCHECK_INTERVAL_SECONDS + 0.1
            self.assertTrue(process.is_alive(timeout=5))
            self.assertEqual(process.ping.call_count, 2)

        # A worker that is gone is still reported on the very next pass.
        process.process.is_alive.return_value = False
        self.assertFalse(process.is_alive(timeout=5))
        self.assertEqual(process.ping.call_count, 2)

    def test_a_ping_that_fails_still_reports_a_hung_worker(self) -> None:
        process = self.worker()
        process.ping.return_value = False
        with mock.patch.object(proxy_idle.time, "monotonic", side_effect=lambda: self.clock[0]):
            self.assertFalse(process.is_alive(timeout=5))

    def test_a_worker_being_replaced_is_not_asked_to_answer(self) -> None:
        # The supervisor polls every worker until it has exited.  A worker that
        # has already stopped answering blocks that poll for
        # ``timeout_worker_healthcheck`` -- five seconds by default -- on each
        # pass, which is what made a quit's teardown swing between a second and
        # a minute.  Once the supervisor has begun stopping, the only question
        # left is whether the process object has exited, and that needs nobody's
        # cooperation to answer.
        process = self.worker()
        setattr(process, proxy_idle._TEARDOWN_ATTR, True)
        with mock.patch.object(proxy_idle.time, "monotonic", side_effect=lambda: self.clock[0]):
            self.assertTrue(process.is_alive(timeout=5))
        self.assertEqual(
            process.ping.call_count,
            0,
            "a worker on its way out must not be waited on",
        )

    def test_stopping_the_supervisor_marks_every_worker(self) -> None:
        # The mark is what the branch above reads, so it has to be set at the
        # moment the supervisor signals -- before the liveness poll runs.
        workers = [self.worker(), self.worker()]
        supervisor = uvicorn_multiprocess.Multiprocess.__new__(
            uvicorn_multiprocess.Multiprocess
        )
        supervisor.processes = workers
        supervisor.terminate_all()

        for worker in workers:
            self.assertTrue(getattr(worker, proxy_idle._TEARDOWN_ATTR, False))


if __name__ == "__main__":
    unittest.main()


class UvicornShutdownMarkerTests(unittest.TestCase):
    """The proxy's own teardown must be visible to the routing classifiers."""

    def setUp(self) -> None:
        from young_router.proxy import base

        self.base = base
        base._install_uvicorn_shutdown_marker_patch()
        self.server = self._server()

    def _server(self):
        base = self.base
        server = uvicorn.server.Server.__new__(uvicorn.server.Server)
        server.servers = []
        server.server_state = _FakeServerState()
        server.config = types.SimpleNamespace(timeout_graceful_shutdown=None)
        server.force_exit = False

        observed: list[bool] = []

        class Lifespan:
            async def shutdown(self) -> None:
                # uvicorn closes the connections first and awaits the
                # in-flight requests here, so the marker has to be up already.
                observed.append(base._proxy_shutdown_in_progress())

        server.lifespan = Lifespan()
        server.observed = observed

        async def wait_tasks_to_complete() -> None:
            # uvicorn's own wait polls until no connection or task remains.  It
            # is reproduced here so a test that seeds a connection exercises the
            # real interaction: the loop ends only because the connection the
            # patch closed left the set, exactly as it does under uvicorn.  The
            # bound is the test's, not uvicorn's: a connection the patch is not
            # meant to touch stays in the set, and this is what stands in for
            # the drain budget that would eventually cancel it.
            for _ in range(5):
                if not server.server_state.connections and not server.server_state.tasks:
                    return
                await asyncio.sleep(0)

        server._wait_tasks_to_complete = wait_tasks_to_complete
        return server

    def test_a_replaced_worker_marks_its_own_shutdown(self) -> None:
        # A worker exits because the app is being replaced, so every stream it
        # was serving is cut by this teardown rather than by anything upstream.
        # Marking it is what stops the cut from being classified -- and reported
        # to the user -- as an upstream failure.
        self.assertFalse(self.base._proxy_shutdown_in_progress())
        asyncio.run(uvicorn.server.Server.shutdown(self.server))
        self.assertEqual(
            self.server.observed,
            [True],
            "a cut by this worker's own teardown must be marked as one",
        )
        self.assertFalse(self.base._proxy_shutdown_in_progress())

    def test_the_marker_survives_a_failing_teardown(self) -> None:
        async def boom() -> None:
            raise RuntimeError("lifespan shutdown failed")

        self.server.lifespan = types.SimpleNamespace(shutdown=boom)
        with self.assertRaises(RuntimeError):
            asyncio.run(uvicorn.server.Server.shutdown(self.server))
        self.assertFalse(self.base._proxy_shutdown_in_progress())

    def test_the_installer_is_idempotent(self) -> None:
        original = uvicorn.server.Server.shutdown
        self.base._install_uvicorn_shutdown_marker_patch()
        self.assertIs(uvicorn.server.Server.shutdown, original)


    def test_a_replaced_worker_does_not_wait_out_the_full_drain(self) -> None:
        # The master's own drain stays long on purpose: a proxy the Core is
        # replacing deliberately must be given the whole window to finish the
        # turns it already accepted.  This is the worker's own shutdown, reached
        # when the app itself is being replaced, and both clients reconnect on
        # their own -- so it does not wait at all, and the open turn is cut
        # through the end-of-response path rather than by cancelling the task
        # (which would close the socket with no terminal, which is exactly what
        # the client renders as ``upstream_route_failure``).
        server = self._server()
        waited: list[bool] = []

        async def record():
            waited.append(True)

        server._wait_tasks_to_complete = record
        asyncio.run(uvicorn.server.Server.shutdown(server))

        self.assertEqual(
            waited,
            [],
            "a replaced app must not wait on an open turn: both clients reconnect",
        )
        self.assertEqual(
            lifecycle.app_quit_grace_seconds(),
            0.0,
            "and the bound it is given must be zero",
        )
        # Zero cannot be the value uvicorn is handed: ``asyncio.wait_for``
        # raises without running its coroutine when the timeout is zero, which
        # would skip this patch entirely instead of skipping the wait.
        self.assertGreater(
            self.base._MIN_SHUTDOWN_PATCH_GRACE_SECONDS,
            0.0,
            "the patched timeout must stay above zero for the patch to run",
        )
        self.assertLess(
            lifecycle.app_quit_grace_seconds(),
            lifecycle.proxy_drain_grace_seconds(),
            "a replaced app must not sit out a full proxy drain",
        )
        self.assertFalse(
            self.base._proxy_shutdown_in_progress(),
            "the cut must be marked as this worker's own teardown",
        )


    def test_a_connection_with_no_request_is_closed_at_shutdown(self) -> None:
        # A keep-alive socket whose peer walked away stays in
        # ``server_state.connections`` until its transport reports
        # ``connection_lost``, and uvicorn's drain waits for that set to empty.
        # With nothing in flight there is nothing to drain, so the transport is
        # closed outright instead of holding the last worker for the drain
        # budget -- which is what made a quit with no active turn take tens of
        # seconds.
        closed = _FakeConnection(cycle=None, state=self.server.server_state)
        self.server.server_state.connections = [closed]

        asyncio.run(uvicorn.server.Server.shutdown(self.server))

        self.assertTrue(closed.transport.aborted)

    def test_a_connection_with_a_completed_response_is_closed_at_shutdown(self) -> None:
        # A recycled keep-alive connection keeps its last cycle; once that
        # response is complete the socket again holds no work for the drain.
        recycled = _FakeConnection(
            cycle=types.SimpleNamespace(response_complete=True),
            state=self.server.server_state,
        )
        self.server.server_state.connections = [recycled]

        asyncio.run(uvicorn.server.Server.shutdown(self.server))

        self.assertTrue(recycled.transport.aborted)

    def test_a_connection_mid_response_is_left_to_drain(self) -> None:
        # The boundary in the other direction: a turn still being written must
        # keep its transport, or the user's answer is cut.
        streaming = _FakeConnection(
            cycle=types.SimpleNamespace(response_complete=False),
            state=self.server.server_state,
        )
        self.server.server_state.connections = [streaming]

        asyncio.run(uvicorn.server.Server.shutdown(self.server))

        self.assertFalse(streaming.transport.aborted)
