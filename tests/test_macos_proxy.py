from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from young_router.proxy import macos_proxy


ROOT = Path(__file__).resolve().parents[1]


class MacOSProxyLauncherTests(unittest.TestCase):
    def test_worker_config_preserves_config_only_cli_behavior(self) -> None:
        config = macos_proxy._worker_config(Path("/private/runtime/config.yaml"))

        self.assertEqual("/private/runtime/config.yaml", config["config"])
        self.assertIs(config["telemetry"], False)
        self.assertIsNone(config["request_timeout"])
        self.assertIs(config["drop_params"], False)
        self.assertIs(config["add_function_to_prompt"], False)

    def test_system_proxy_snapshot_prefers_environment_settings(self) -> None:
        with mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_environment",
            return_value={"https": "http://environment-proxy.example:8080"},
        ), mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_macosx_sysconf",
        ) as get_system_proxies, mock.patch.object(
            macos_proxy.urllib.request,
            "_get_proxy_settings",
        ) as get_system_settings:
            snapshot = macos_proxy._system_proxy_snapshot()

        self.assertEqual(
            {
                "source": "environment",
                "proxies": {"https": "http://environment-proxy.example:8080"},
            },
            snapshot,
        )
        get_system_proxies.assert_not_called()
        get_system_settings.assert_not_called()

    def test_system_proxy_snapshot_caches_macos_settings_before_fork(self) -> None:
        with mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_environment",
            return_value={},
        ), mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_macosx_sysconf",
            return_value={"https": "http://system-proxy.example:8080"},
        ), mock.patch.object(
            macos_proxy.urllib.request,
            "_get_proxy_settings",
            return_value={"exclude_simple": True, "exceptions": ["*.example.test"]},
        ):
            snapshot = macos_proxy._system_proxy_snapshot()

        self.assertEqual("macos", snapshot["source"])
        self.assertEqual(
            {"https": "http://system-proxy.example:8080"},
            snapshot["proxies"],
        )
        self.assertEqual(
            {"exclude_simple": True, "exceptions": ["*.example.test"]},
            snapshot["settings"],
        )

    def test_a_posix_host_that_is_not_macos_reads_only_the_environment(self) -> None:
        """SystemConfiguration exists on one platform; asking elsewhere raises.

        The launcher runs on every POSIX host now, and ``getproxies_macosx_sysconf``
        is a name only the macOS runtime has.  An empty environment therefore
        reads as "no proxy", which is the same answer the lookup switch gives --
        instead of the launcher dying before its first worker on a name that was
        never there.
        """

        with mock.patch.object(
            macos_proxy.sys, "platform", "linux"
        ), mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_environment",
            return_value={},
        ), mock.patch.object(
            macos_proxy.urllib.request,
            "getproxies_macosx_sysconf",
        ) as get_system_proxies:
            snapshot = macos_proxy._system_proxy_snapshot()

        self.assertEqual({"source": "environment", "proxies": {}}, snapshot)
        get_system_proxies.assert_not_called()

    def test_run_preloads_proxy_once_and_starts_requested_workers(self) -> None:
        uvicorn = SimpleNamespace(run=mock.Mock())
        uvicorn_subprocess = SimpleNamespace(spawn=None)
        forkserver = object()
        settle = mock.Mock()
        # The order of these steps is the launcher's startup budget: the
        # forkserver's preload is the complete LiteLLM runtime, and the app
        # modules the launcher then imports reach that same runtime.  Starting
        # the preload first is what lets the two imports overlap.
        order: list[str] = []

        class LauncherImport:
            """Stub module that notes when the launcher's import reaches it."""

            def __init__(self, name: str, value: object) -> None:
                self._name = name
                self._value = value

            def __getattr__(self, item: str) -> object:
                if item.startswith("__"):
                    raise AttributeError(item)
                order.append(self._name)
                return self._value

        # Stub the launcher's own modules so the assertions below pin the
        # default WebSocket frame limit and the settle call without importing
        # the real modules in this unit test.
        state_module = LauncherImport("state", settle)
        settings_module = LauncherImport("launcher_settings", lambda: 67108864)

        def prime_forkserver() -> None:
            order.append("prime")

        def import_module(name: str) -> object:
            if name == "uvicorn":
                return uvicorn
            if name == "uvicorn._subprocess":
                return uvicorn_subprocess
            raise AssertionError(name)

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(
            sys.modules,
            {
                "young_router.proxy.state": state_module,
                "young_router.proxy.launcher_settings": settings_module,
                # The launcher's late imports are stubbed for the same reason:
                # they are real modules that own real state files, and this test
                # is about the launch order, not about what they contain.
                "young_router.proxy.concurrency": SimpleNamespace(reset_leases=mock.Mock()),
            },
        ), mock.patch.object(
            multiprocessing, "set_forkserver_preload", create=True
        ) as set_preload, mock.patch.object(
            multiprocessing, "get_context", return_value=forkserver
        ) as get_context, mock.patch.object(
            macos_proxy.importlib, "import_module", side_effect=import_module
        ), mock.patch.object(
            macos_proxy, "_prime_forkserver", side_effect=prime_forkserver
        ) as prime, mock.patch.object(
            macos_proxy,
            "_system_proxy_snapshot",
            return_value={"source": "macos", "proxies": {}, "settings": {}},
        ):
            result = macos_proxy.run(
                [
                    "--config",
                    "/private/runtime/config.yaml",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "4000",
                    "--workers",
                    "16",
                ]
            )
            worker_config = json.loads(os.environ["WORKER_CONFIG"])
            proxy_snapshot = json.loads(os.environ[macos_proxy.SYSTEM_PROXY_SNAPSHOT_ENV])

        self.assertEqual(0, result)
        self.assertEqual("/private/runtime/config.yaml", worker_config["config"])
        self.assertIs(worker_config["telemetry"], False)
        self.assertEqual(
            {"source": "macos", "proxies": {}, "settings": {}},
            proxy_snapshot,
        )
        set_preload.assert_called_once_with([macos_proxy.PROXY_MODULE])
        get_context.assert_called_once_with("forkserver")
        prime.assert_called_once_with()
        # The preload starts before the launcher's own imports, and those
        # imports are the two modules that reach no further than the standard
        # library, which is what keeps the runtime out of the launcher's own
        # startup budget.
        self.assertEqual(["prime", "launcher_settings", "state"], order[:3])
        self.assertIs(forkserver, uvicorn_subprocess.spawn)
        # A new proxy process owns no in-flight requests, so it closes rows an
        # earlier process left unfinished before the workers start serving.
        settle.assert_called_once_with()
        uvicorn.run.assert_called_once_with(
            macos_proxy.PROXY_APP,
            host="127.0.0.1",
            port=4000,
            workers=16,
            loop="uvloop",
            ws_max_size=67108864,
            timeout_graceful_shutdown=14400.0,
        )

    def test_launcher_modules_do_not_import_the_workers_runtime(self) -> None:
        """The launcher's own imports must stay out of the workers' runtime.

        Its startup budget is the forkserver's preload; importing that same
        runtime in the launcher as well is what made a restart pay for it
        twice.
        """

        script = (
            "import sys, young_router.proxy.launcher_settings, young_router.proxy.state; "
            "print('litellm' in sys.modules)"
        )
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual("False", result.stdout.strip(), result.stderr[-400:])


class WorkerHandoverGuardTests(unittest.TestCase):
    """The handover that used to die with CPython's fd-acknowledgement error.

    A process-group ``SIGTERM`` aimed at the proxy kills the preloading
    forkserver silently while uvicorn only queues that signal for the master, so
    the first worker handover failed with ``did not receive acknowledgement of
    fd`` -- a sentence that names neither the process nor the cause, and that
    took the whole start with it.
    """

    def install_guard(self, handover: object, *, attempts: int = 3) -> "macos_proxy._WorkerHandoverGuard":
        # The supervisor calls the module-level name, not the class attribute.
        patcher = mock.patch.object(multiprocessing.forkserver, "connect_to_new_process", handover)
        patcher.start()
        self.addCleanup(patcher.stop)
        delay = mock.patch.object(macos_proxy, "WORKER_SPAWN_RETRY_SECONDS", 0)
        delay.start()
        self.addCleanup(delay.stop)
        guard = macos_proxy._WorkerHandoverGuard(attempts=attempts)
        guard.install()
        return guard

    def test_handover_is_retried_when_the_forkserver_is_gone(self) -> None:
        calls: list[int] = []

        def handover(_fds: object) -> str:
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("did not receive acknowledgement of fd")
            return "worker-pipes"

        guard = self.install_guard(handover)

        self.assertEqual(
            "worker-pipes",
            multiprocessing.forkserver.connect_to_new_process([]),
        )
        self.assertEqual(3, len(calls))
        self.assertFalse(guard.stop_requested)

    def test_handover_failure_names_the_forkserver_it_lost(self) -> None:
        def handover(_fds: object) -> str:
            raise RuntimeError("did not receive acknowledgement of fd")

        with mock.patch.object(macos_proxy, "_forkserver_pid", return_value=None):
            self.install_guard(handover, attempts=2)
            with self.assertRaises(RuntimeError) as raised:
                multiprocessing.forkserver.connect_to_new_process([])

        message = str(raised.exception)
        # The cause has to be readable in ``server.log``: which process went
        # away, how many handovers were spent, and CPython's own sentence last.
        self.assertIn("the proxy's forkserver", message)
        self.assertIn("before it forked a worker", message)
        self.assertIn("2 handover attempts", message)
        self.assertIn("did not receive acknowledgement of fd", message)

    def test_handover_is_cancelled_when_the_proxy_was_told_to_stop(self) -> None:
        calls: list[int] = []

        def handover(_fds: object) -> str:
            calls.append(1)
            raise RuntimeError("did not receive acknowledgement of fd")

        guard = self.install_guard(handover)
        guard._stop_requested = True

        with mock.patch.object(macos_proxy, "_forkserver_pid", return_value=None):
            with self.assertRaises(macos_proxy.ProxyStartCancelled):
                multiprocessing.forkserver.connect_to_new_process([])

        # A planned teardown is the answer, not a startup failure: no retry, and
        # the launcher exits 0 instead of raising through uvicorn.
        self.assertEqual(1, len(calls))

    def test_handover_does_not_retry_a_forkserver_that_refused_the_work(self) -> None:
        calls: list[int] = []

        def handover(_fds: object) -> str:
            calls.append(1)
            raise RuntimeError("too many fds")

        self.install_guard(handover)

        with self.assertRaises(RuntimeError):
            multiprocessing.forkserver.connect_to_new_process([])
        self.assertEqual(1, len(calls))

    def test_run_reports_a_cancelled_start_as_a_clean_exit(self) -> None:
        uvicorn = SimpleNamespace(run=mock.Mock(side_effect=macos_proxy.ProxyStartCancelled("its Core exited")))
        uvicorn_subprocess = SimpleNamespace(spawn=None)

        def import_module(name: str) -> object:
            if name == "uvicorn":
                return uvicorn
            if name == "uvicorn._subprocess":
                return uvicorn_subprocess
            raise AssertionError(name)

        captured = io.StringIO()
        with mock.patch.dict(
            sys.modules,
            {
                # Only what this test observes: the settle call, and the frame
                # limit the launcher passes to uvicorn.  The rest of the
                # launcher's own modules are imported for real -- they are
                # standard library only -- because a stand-in has to answer every
                # name they read, and one that answers only some of them fails
                # inside a module this test never named.
                "young_router.proxy.state": SimpleNamespace(settle_stale_recent_requests=mock.Mock()),
                "young_router.proxy.launcher_settings": SimpleNamespace(_websocket_max_frame_bytes=lambda: 1),
                "young_router.proxy.concurrency": SimpleNamespace(reset_leases=mock.Mock()),
            },
        ), mock.patch.object(multiprocessing, "set_forkserver_preload", create=True), mock.patch.object(
            multiprocessing, "get_context", return_value=object()
        ), mock.patch.object(
            macos_proxy.importlib, "import_module", side_effect=import_module
        ), mock.patch.object(
            macos_proxy, "_prime_forkserver"
        ), mock.patch.object(
            macos_proxy, "_install_worker_handover_guard"
        ), mock.patch.object(
            macos_proxy,
            "_system_proxy_snapshot",
            return_value={"source": "macos", "proxies": {}, "settings": {}},
        ), contextlib.redirect_stderr(captured):
            result = macos_proxy.run(
                ["--config", "/private/runtime/config.yaml", "--host", "127.0.0.1", "--port", "4000", "--workers", "2"]
            )

        self.assertEqual(0, result)
        # The service log says a stop was the reason, instead of a traceback
        # about a file descriptor nobody can attribute.
        self.assertIn("proxy start cancelled: its Core exited", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
