from __future__ import annotations

import os
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "app-processes.sh"
RESTART = ROOT / "scripts" / "restart-app.sh"
INSTALLER = ROOT / "scripts" / "build-and-install-macos.sh"


class AppProcessesTests(unittest.TestCase):
    """The stop side of a restart addresses the bundle's tree, never its name.

    A restart waits for the pids it captured before quitting the host, and the
    wait is bounded by a budget: a captured pid that will never exit spends
    that whole budget with the app down.  A command line that merely mentions
    the app is exactly such a pid, and so is a zombie, which the kernel still
    lists after its process has ended.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = Path(temp.name) / "Fake Router.app"
        host_directory = self.app / "Contents" / "MacOS"
        host_directory.mkdir(parents=True)
        # A symlink, not a copy: macOS refuses to execute a copied system
        # binary, and a shebang script would put its interpreter path first on
        # the command line, which is not how a bundle's host reads.
        self.host = host_directory / "FakeRouter"
        self.host.symlink_to("/bin/sh")

    # -- helpers ---------------------------------------------------------

    def helper(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/bash", str(HELPER), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def tree(self) -> list[int]:
        result = self.helper("tree", str(self.app))
        self.assertEqual(result.returncode, 0, result.stderr)
        return [int(line) for line in result.stdout.split()]

    def hosts(self) -> list[int]:
        result = self.helper("hosts", str(self.app))
        self.assertEqual(result.returncode, 0, result.stderr)
        return [int(line) for line in result.stdout.split()]

    def stop(self, *pids: int) -> subprocess.CompletedProcess:
        return self.helper("stop", *(str(pid) for pid in pids))

    def interpreter(self) -> Path:
        """The bundle's interpreter path, as the Core and the launcher use it.

        A symlink to the system shell, not a copy: what the helper reads is the
        command line, and a real interpreter would only add startup cost.
        """

        directory = self.app / "Contents" / "Resources" / "Core" / "runtime" / "python" / "bin"
        directory.mkdir(parents=True, exist_ok=True)
        python = directory / "python3.12"
        if not python.exists():
            python.symlink_to("/bin/sh")
        return python

    def alive(self, *pids: int) -> list[int]:
        result = self.helper("alive", *(str(pid) for pid in pids))
        self.assertEqual(result.returncode, 0, result.stderr)
        return [int(line) for line in result.stdout.split()]

    def start(self, *argv: str, executable: Path | None = None) -> subprocess.Popen:
        process = subprocess.Popen(
            list(argv),
            executable=str(executable) if executable else None,
            start_new_session=True,
        )

        def terminate() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Already stopped by the test itself: a zombie group leader with
                # nothing left in it answers EPERM, and there is nothing here to
                # signal.
                pass
            process.wait(timeout=10)

        self.addCleanup(terminate)
        return process

    def wait_for_child(self, parent: int) -> int:
        for _ in range(200):
            found = subprocess.run(
                ["pgrep", "-P", str(parent)], text=True, capture_output=True, check=False
            ).stdout.split()
            if found:
                return int(found[0])
            time.sleep(0.05)
        self.fail("the fake host never started a child")

    # -- the capture -----------------------------------------------------

    def test_tree_is_the_host_and_its_descendants(self) -> None:
        host = self.start(str(self.host), "-c", "sleep 300 & wait")
        child = self.wait_for_child(host.pid)

        pids = self.tree()

        self.assertIn(host.pid, pids)
        self.assertIn(child, pids)
        self.assertIn(child, self.alive(host.pid, child))

    def test_tree_ignores_a_process_named_after_the_app(self) -> None:
        host = self.start(str(self.host), "-c", "sleep 300 & wait")
        # argv[0] alone carries the app's name, the way another session's test
        # run under the bundle's interpreter or a probe shell does.
        decoy = self.start("Fake Router decoy", "300", executable=Path("/bin/sleep"))

        pids = self.tree()

        self.assertIn(host.pid, pids)
        self.assertNotIn(decoy.pid, pids)

    def test_hosts_is_the_host_without_its_descendants(self) -> None:
        host = self.start(str(self.host), "-c", "sleep 300 & wait")
        child = self.wait_for_child(host.pid)

        self.assertEqual(self.hosts(), [host.pid])
        self.assertIn(child, self.tree())

    def test_host_is_found_when_it_was_started_with_arguments(self) -> None:
        # The arguments are paths, so any rule that only accepts a command line
        # with no further slash after Contents/MacOS stops finding this app's
        # host -- and a stop that cannot find its host decides the app is not
        # running and starts a second instance beside it.
        host = self.start(str(self.host), "-c", "sleep 300 & wait")

        self.assertEqual(self.hosts(), [host.pid])

    def test_tree_is_empty_once_the_app_is_gone(self) -> None:
        host = self.start(str(self.host), "-c", "sleep 300 & wait")
        self.assertIn(host.pid, self.tree())

        os.killpg(host.pid, signal.SIGKILL)
        host.wait(timeout=10)
        for _ in range(200):
            if not self.tree():
                break
            time.sleep(0.05)

        self.assertEqual(self.tree(), [])

    # -- liveness --------------------------------------------------------

    def test_alive_ignores_a_zombie(self) -> None:
        pid = os.fork()
        if pid == 0:
            os._exit(0)

        def reap() -> None:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

        self.addCleanup(reap)
        state = ""
        for _ in range(200):
            state = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="], text=True, capture_output=True, check=False
            ).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.05)

        self.assertTrue(state.startswith("Z"), f"the child never became a zombie: {state!r}")
        self.assertEqual(self.alive(pid), [])

    def test_alive_ignores_a_pid_that_is_gone(self) -> None:
        host = self.start(str(self.host), "-c", "sleep 300 & wait")
        pid = host.pid
        os.killpg(pid, signal.SIGKILL)
        host.wait(timeout=10)

        self.assertEqual(self.alive(pid), [])

    # -- the stop --------------------------------------------------------

    def test_stop_kills_the_captured_pids_and_reports_nothing(self) -> None:
        host = self.start(str(self.host), "-c", "sleep 300 & wait")
        child = self.wait_for_child(host.pid)

        result = self.stop(host.pid, child)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [])
        self.assertEqual(self.alive(host.pid, child), [])

    def test_stop_is_at_peace_with_a_pid_that_is_already_gone(self) -> None:
        host = self.start(str(self.host), "-c", "sleep 300 & wait")
        pid = host.pid
        os.killpg(pid, signal.SIGKILL)
        host.wait(timeout=10)

        result = self.stop(pid)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [])

    # -- the boundary the script keeps -----------------------------------

    def test_restart_selects_its_pids_from_the_bundle(self) -> None:
        script = RESTART.read_text(encoding="utf-8")
        # Comments may name the pattern this replaced; no line of code may use
        # one.
        code = "\n".join(
            line for line in script.splitlines() if not line.lstrip().startswith("#")
        )

        self.assertIn('STOPPING_PIDS="$("$APP_PROCESSES" tree "$APP"', script)
        # No unanchored name pattern may decide what a stop waits on or
        # signals: it cannot tell this app's processes from a foreign command
        # line that mentions it.
        self.assertNotIn("$PATTERN", code)
        self.assertNotIn("pgrep", code)
        self.assertNotIn("pkill", code)

    def test_restart_stops_the_tree_in_one_shot(self) -> None:
        script = RESTART.read_text(encoding="utf-8")

        # One policy: the captured pids are handed to the helper's stop, which
        # kills them together, and nothing here waits for the app's own
        # teardown -- that wait was most of a restart.
        self.assertIn('"$APP_PROCESSES" stop $STOPPING_PIDS', script)
        self.assertNotIn("kill -TERM", script)
        self.assertNotIn("osascript", script)
        self.assertNotIn("GRACEFUL", script)
        self.assertNotIn("stopping_any()", script)

    def test_the_installer_uses_the_same_stop(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")

        self.assertIn('"$APP_PROCESSES" stop $bundle_pids', installer)
        self.assertIn("APP_PROCESSES=", installer)
        # The grace budget this used to carry is gone with the policy it served.
        self.assertNotIn("STOP_QUIT_GRACE_SECONDS", installer)
        self.assertNotIn("STOP_GRACE_POLLS", installer)
        self.assertNotIn("kill -TERM", installer)
        self.assertNotIn("osascript", installer)

    def test_restart_checks_the_port_once_instead_of_waiting_on_it(self) -> None:
        script = RESTART.read_text(encoding="utf-8")

        # Clients are pointed at the recorded port by hand, so a listener that
        # outlived the tree has to be noticed; it is reported, not waited out.
        self.assertIn("port_taken()", script)
        self.assertIn('-iTCP:"$recorded_port" -sTCP:LISTEN', script)
        self.assertIn("is still held; the new proxy will step forward", script)
        self.assertNotIn("gating_any()", script)


if __name__ == "__main__":
    unittest.main()
