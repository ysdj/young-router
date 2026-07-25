from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "mac_menu" / "Sources").glob("*.swift"))


HARNESS = r"""
import Cocoa
import Foundation

let delegate = AppDelegate()
let group = DispatchGroup()
let resultLock = NSLock()
var result: (Int32, String)?

group.enter()
DispatchQueue.global(qos: .userInitiated).async {
    let value = delegate.lifecycleControl("start")
    resultLock.lock()
    result = value
    resultLock.unlock()
    group.leave()
}

Thread.sleep(forTimeInterval: 0.4)
delegate.cancelLifecycleControl()
guard group.wait(timeout: .now() + 5) == .success else {
    fputs("lifecycle control did not cancel within five seconds\n", stderr)
    exit(1)
}

resultLock.lock()
let exitCode = result?.0
resultLock.unlock()
guard exitCode != nil, exitCode != 0 else {
    fputs("cancelled lifecycle control unexpectedly succeeded\n", stderr)
    exit(1)
}
print("cancelled")
"""


class AppLifecycleSwiftTests(unittest.TestCase):
    def test_quit_waits_for_service_stop_and_watcher_unload(self) -> None:
        source = (ROOT / "mac_menu" / "Sources" / "AppDelegateCore.swift").read_text(
            encoding="utf-8"
        )
        self.assertIn('self.control("stop", timeoutSeconds: 12)', source)
        self.assertIn('self.control("config-watch-disable", timeoutSeconds: 8)', source)
        self.assertIn('sender.reply(toApplicationShouldTerminate: false)', source)
        self.assertIn('sender.reply(toApplicationShouldTerminate: true)', source)
        self.assertIn('self.resumeLifecycleControl()', source)
        self.assertIn('self.startStatusRefreshTimer()', source)

    def test_headless_isolated_test_hides_status_item_and_failure_alerts(self) -> None:
        core = (ROOT / "mac_menu" / "Sources" / "AppDelegateCore.swift").read_text(
            encoding="utf-8"
        )
        status = (ROOT / "mac_menu" / "Sources" / "AppDelegateStatus.swift").read_text(
            encoding="utf-8"
        )
        self.assertIn('environment["LITELLM_MENU_TEST_HEADLESS"] == "1"', core)
        self.assertIn("statusItem.isVisible = false", core)
        self.assertIn("if !self.isHeadlessIsolatedTest", core)
        self.assertIn("showFailureAlert: !isHeadlessIsolatedTest", status)

    def test_open_app_recovers_any_non_running_service_state(self) -> None:
        source = (ROOT / "mac_menu" / "Sources" / "AppDelegateControl.swift").read_text(
            encoding="utf-8"
        )
        recovery = source.split("func displayedServiceState", 1)[1].split(
            "func readAutoStartState", 1
        )[0]
        self.assertIn("case .stopped, .unhealthy:", recovery)
        self.assertIn("scheduleUnexpectedServiceRecovery()", recovery)

    def test_lifecycle_control_cancels_a_blocked_start_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime = temp / "runtime"
            runtime.mkdir()
            service = temp / "service.sh"
            service.write_text(
                "#!/bin/bash\nset -euo pipefail\n[[ \"${1:-}\" == start ]] || exit 64\n/bin/sleep 60\n",
                encoding="utf-8",
            )
            service.chmod(service.stat().st_mode | stat.S_IXUSR)
            harness = temp / "main.swift"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            binary = temp / "lifecycle-harness"

            compiled = subprocess.run(
                [
                    "swiftc",
                    *(str(path) for path in SOURCES if path.name != "main.swift"),
                    str(harness),
                    "-o",
                    str(binary),
                    "-framework",
                    "Cocoa",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)

            env = os.environ.copy()
            env.update(
                {
                    "LITELLM_TEMPLATE_ROOT": str(temp),
                    "LITELLM_RUNTIME_ROOT": str(runtime),
                }
            )
            result = subprocess.run(
                [str(binary)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "cancelled")


if __name__ == "__main__":
    unittest.main()
