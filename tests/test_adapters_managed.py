"""The Core-less worker startup a proxy-only host runs.

The interesting behaviour is not "a worker starts" but the three rules that keep
one worker from costing the gateway: start only what the configuration names,
survive a package this host never staged, and never print what the fragment
holds (those values are bearers).
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from young_router.adapters import managed


class _Runtime:
    def __init__(self, fragment: dict[str, str], calls: list[bool]) -> None:
        self._fragment = fragment
        self._calls = calls

    def environment(self, *, autostart: bool = False) -> dict[str, str]:
        self._calls.append(autostart)
        return dict(self._fragment)

    def stop(self) -> None:
        pass


class _Worker:
    """One adapter module, shaped the way the registry expects."""

    def __init__(
        self,
        name: str,
        reference: str,
        *,
        available: bool = True,
        raise_on_start: bool = False,
    ) -> None:
        self.__name__ = name
        self.API_BASE_ENV = {"provider": reference}
        self._available = available
        self._raise = raise_on_start
        self.calls: list[bool] = []

    def available(self) -> bool:
        return self._available

    def managed_runtime(self, runtime_root: Path | str | None = None) -> _Runtime:
        if self._raise:
            raise RuntimeError("worker refused to start")
        return _Runtime({f"{self.API_BASE_ENV['provider']}": "http://127.0.0.1:1"}, self.calls)


class ManagedWorkersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "config.yaml"

    def _write(self, text: str) -> None:
        self.config.write_text(text, encoding="utf-8")

    def test_starts_only_the_worker_the_config_names(self) -> None:
        named = _Worker("named", "NAMED_BASE")
        other = _Worker("other", "OTHER_BASE")
        self._write("model_list:\n  - litellm_params:\n      api_base: os.environ/NAMED_BASE\n")

        with mock.patch.object(managed, "managed_worker_modules", return_value=[named, other]):
            fragment = managed.environment(self.config)

        self.assertEqual(fragment, {"NAMED_BASE": "http://127.0.0.1:1"})
        self.assertEqual(named.calls, [True], "the gateway path must autostart")
        self.assertEqual(other.calls, [], "an unnamed worker must not be started")

    def test_an_unstaged_package_costs_only_its_own_routes(self) -> None:
        missing = _Worker("missing", "MISSING_BASE", available=False)
        present = _Worker("present", "PRESENT_BASE")
        self._write("a: os.environ/MISSING_BASE\nb: os.environ/PRESENT_BASE\n")

        with mock.patch.object(managed, "managed_worker_modules", return_value=[missing, present]):
            fragment = managed.environment(self.config)

        self.assertEqual(fragment, {"PRESENT_BASE": "http://127.0.0.1:1"})
        self.assertEqual(missing.calls, [])

    def test_a_worker_that_refuses_to_start_does_not_stop_the_next_one(self) -> None:
        broken = _Worker("broken", "BROKEN_BASE", raise_on_start=True)
        healthy = _Worker("healthy", "HEALTHY_BASE")
        self._write("a: os.environ/BROKEN_BASE\nb: os.environ/HEALTHY_BASE\n")

        with mock.patch.object(managed, "managed_worker_modules", return_value=[broken, healthy]):
            fragment = managed.environment(self.config)

        self.assertEqual(fragment, {"HEALTHY_BASE": "http://127.0.0.1:1"})

    def test_a_status_read_never_spawns_one(self) -> None:
        worker = _Worker("worker", "SOME_BASE")
        self._write("a: os.environ/SOME_BASE\n")

        with mock.patch.object(managed, "managed_worker_modules", return_value=[worker]):
            managed.environment(self.config, autostart=False)

        self.assertEqual(worker.calls, [False])

    def test_main_runs_the_command_with_the_fragment_and_never_prints_values(self) -> None:
        worker = _Worker("worker", "SOME_BASE")
        self._write("a: os.environ/SOME_BASE\n")
        process = mock.Mock()
        process.wait.return_value = 0
        stderr = io.StringIO()
        with mock.patch.object(managed, "managed_worker_modules", return_value=[worker]), \
             mock.patch.object(managed.subprocess, "Popen", return_value=process) as popen, \
             mock.patch("sys.stderr", stderr):
            code = managed.main(["--config", str(self.config), "--", "proxy", "--flag"])

        self.assertEqual(code, 0)
        self.assertEqual(popen.call_args.args[0], ["proxy", "--flag"])
        self.assertEqual(popen.call_args.kwargs["env"]["SOME_BASE"], "http://127.0.0.1:1")
        self.assertNotIn("127.0.0.1:1", stderr.getvalue(), "a bearer must never reach the log")

    def test_the_running_fragment_reaches_the_child_environment(self) -> None:
        worker = _Worker("worker", "SOME_BASE")
        self._write("a: os.environ/SOME_BASE\n")

        with mock.patch.object(managed, "managed_worker_modules", return_value=[worker]), \
             mock.patch.object(managed.subprocess, "Popen") as popen, \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("sys.stderr", io.StringIO()):
            popen.return_value.wait.return_value = 0
            managed.main(["--config", str(self.config), "--", "proxy"])

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["SOME_BASE"], "http://127.0.0.1:1")

    def test_worker_supervision_continues_until_the_proxy_exits(self) -> None:
        marker = Path(self.directory.name) / "restarted"
        worker = _Worker("worker", "SOME_BASE")
        self._write("a: os.environ/SOME_BASE\n")

        class Runtime(_Runtime):
            def environment(self, *, autostart: bool = False) -> dict[str, str]:
                def supervise() -> None:
                    time.sleep(0.1)
                    marker.write_text("restarted", encoding="utf-8")

                threading.Thread(target=supervise, daemon=True).start()
                return super().environment(autostart=autostart)

        runtime = Runtime({"SOME_BASE": "http://127.0.0.1:1"}, worker.calls)
        script = (
            "import pathlib, sys, time\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "deadline = time.monotonic() + 5\n"
            "while not path.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
            "sys.exit(0 if path.exists() else 1)\n"
        )
        with mock.patch.object(managed, "managed_worker_modules", return_value=[worker]), \
             mock.patch.object(worker, "managed_runtime", return_value=runtime), \
             mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, managed.main(["--config", str(self.config), "--", sys.executable, "-c", script, str(marker)]))

    def test_no_command_answers_variable_names_only(self) -> None:
        worker = _Worker("worker", "SOME_BASE")
        self._write("a: os.environ/SOME_BASE\n")
        stderr = io.StringIO()

        with mock.patch.object(managed, "managed_worker_modules", return_value=[worker]), \
             mock.patch("sys.stderr", stderr):
            code = managed.main(["--config", str(self.config)])

        self.assertEqual(code, 0)
        self.assertIn("SOME_BASE", stderr.getvalue())
        self.assertNotIn("127.0.0.1:1", stderr.getvalue())

    def test_an_absent_config_degrades_to_no_workers(self) -> None:
        worker = _Worker("worker", "SOME_BASE")
        with mock.patch.object(managed, "managed_worker_modules", return_value=[worker]):
            fragment = managed.environment(Path(self.directory.name) / "absent.yaml")

        self.assertEqual(fragment, {})
        self.assertEqual(worker.calls, [])

    def test_a_worker_without_the_factory_is_skipped(self) -> None:
        class Bare:
            API_BASE_ENV = {"provider": "BARE_BASE"}

            def available(self) -> bool:
                return True

        self._write("a: os.environ/BARE_BASE\n")
        with mock.patch.object(managed, "managed_worker_modules", return_value=[Bare()]):
            self.assertEqual(managed.environment(self.config), {})


if __name__ == "__main__":
    unittest.main()
