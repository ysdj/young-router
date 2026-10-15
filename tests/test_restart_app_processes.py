from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "app-processes.sh"
RESTART = ROOT / "scripts" / "restart-app.sh"


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
            except ProcessLookupError:
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

    # -- the boundary the script keeps -----------------------------------

    def test_restart_selects_its_pids_from_the_bundle(self) -> None:
        script = RESTART.read_text(encoding="utf-8")

        self.assertIn('STOPPING_PIDS="$("$APP_PROCESSES" tree "$APP"', script)
        # No unanchored name pattern may decide what a stop waits on or
        # signals: it cannot tell this app's processes from a foreign command
        # line that mentions it.
        self.assertNotIn("$PATTERN", script)


if __name__ == "__main__":
    unittest.main()
