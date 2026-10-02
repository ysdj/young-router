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

from young_router.proxy import proxy_idle


class _FakeServerState:
    def __init__(self) -> None:
        self.default_headers: list[tuple[bytes, bytes]] = []
        self.connections: list[object] = []
        self.tasks: list[object] = []


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


if __name__ == "__main__":
    unittest.main()
