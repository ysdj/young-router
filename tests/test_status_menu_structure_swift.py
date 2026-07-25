from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "mac_menu" / "Sources").glob("*.swift"))


HARNESS = r"""
import Cocoa

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
delegate.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
delegate.configureStatusButton()
delegate.buildMenu()

let recovering = AppDelegate.RouteRecoveryStatus(
    summary: "1 recovering / 2 cooldown",
    recovering: 1,
    cooldown: 2,
    overdue: 1,
    current: .init(
        status: "retry scheduled",
        activity: "needs attention",
        kind: "billing",
        title: "Credit or quota limit",
        detail: "The upstream reported insufficient balance.",
        attempt: 3,
        heartbeatAgeSeconds: 50,
        cooldownRemainingSeconds: 240
    )
)
delegate.renderStatusButton(recovering)
delegate.renderState(
    AppDelegate.MenuState(
        serviceState: .running,
        autoStartState: .disabled,
        routeRecoverySummary: recovering.summary,
        routeRecovery: recovering,
        webdavSyncEnabled: false,
        webdavLastStatus: AppDelegate.WebDAVLastStatus()
    )
)
guard !delegate.routeRecoveryStatusTooltip(recovering).localizedCaseInsensitiveContains("billing"),
      !delegate.routeRecoveryStatusTitle("billing retry").localizedCaseInsensitiveContains("billing")
else {
    fputs("recovery UI must not display billing\n", stderr)
    exit(1)
}
guard let statusButton = delegate.statusItem.button,
      statusButton.title == "LL",
      statusButton.attributedTitle.string == "LL",
      statusButton.image == nil
else {
    fputs("status item must remain a neutral LL label during recovery\n", stderr)
    exit(1)
}

guard let menu = delegate.statusItem.menu else {
    fputs("missing status menu\n", stderr)
    exit(1)
}

func verifyMenu(_ menu: NSMenu) -> Bool {
    for item in menu.items where !item.isSeparatorItem {
        if item.submenu != nil {
            return false
        }
    }
    return true
}

guard verifyMenu(menu) else {
    fputs("unexpected status-menu submenu\n", stderr)
    exit(1)
}

guard !menu.items.contains(where: { $0.title.localizedCaseInsensitiveContains("billing") }) else {
    fputs("status menu must not display billing\n", stderr)
    exit(1)
}

let forbiddenServiceControls = ["Start LiteLLM Service", "Stop LiteLLM Service", "Restart LiteLLM Service"]
guard !menu.items.contains(where: { forbiddenServiceControls.contains($0.title) }) else {
    fputs("status menu must not expose manual service controls\n", stderr)
    exit(1)
}

let requiredTitles = ["Import / Export Config...", "View Logs", "Recovery: 1 recovering / 2 cooldown"]
guard requiredTitles.allSatisfy({ required in menu.items.contains(where: { $0.title == required }) }) else {
    fputs("flat status menu is missing a required entry\n", stderr)
    exit(1)
}

print("flat-status-menu")
"""


class StatusMenuStructureSwiftTests(unittest.TestCase):
    def test_status_menu_has_no_submenus(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            harness = temp / "main.swift"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            binary = temp / "status-menu-harness"
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
            result = subprocess.run(
                [str(binary)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "flat-status-menu")

    def test_manual_service_controls_are_removed_from_sources(self) -> None:
        sources = "\n".join(path.read_text(encoding="utf-8") for path in SOURCES)
        for symbol in (
            "startMenuItem",
            "stopMenuItem",
            "restartServiceMenuItem",
            "startLiteLLMService",
            "stopLiteLLMService",
            "restartLiteLLMService",
        ):
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, sources)


if __name__ == "__main__":
    unittest.main()
