from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ROOT / "mac_menu" / "Sources" / "SettingsWindowPresentation.swift",
    ROOT / "mac_menu" / "Sources" / "LogWindowController.swift",
]


HARNESS = r'''
import Cocoa
import Foundation

func fail(_ value: String) -> Never {
    fputs(value + "\n", stderr)
    exit(1)
}

func descendants(of root: NSView) -> [NSView] {
    root.subviews + root.subviews.flatMap(descendants)
}

let root = CommandLine.arguments[1]
let longRoute = "model=test / provider=relay / upstream=openai/" + String(repeating: "wide-route-segment-", count: 24)
let fixtureDate = ISO8601DateFormatter().date(from: "2026-07-20T20:30:00Z")!
let manager = FileManager.default
try manager.createDirectory(atPath: root, withIntermediateDirectories: true)
try manager.createDirectory(
    atPath: (root as NSString).appendingPathComponent(".litellm-runtime"),
    withIntermediateDirectories: true
)
let serviceLog = (root as NSString).appendingPathComponent("menu-server.log")
try "\u{001B}[92m[2026-07-20T20:13:44Z] INFO live service record\u{001B}[0m\n04:13:45 INFO next record\nlegacy unprefixed service record\nlitellm_route_trace {\"timestamp\":\"2026-07-20T20:13:48Z\",\"event\":\"selected_deployment\",\"model_group\":\"test\"}\n".write(
    toFile: serviceLog,
    atomically: true,
    encoding: .utf8
)
try manager.setAttributes([.modificationDate: fixtureDate], ofItemAtPath: serviceLog)
let menuLog = (root as NSString).appendingPathComponent("menu-actions.log")
let configLog = (root as NSString).appendingPathComponent("config-watch.log")
try "[2026-07-20T20:13:46Z] menu action\n".write(toFile: menuLog, atomically: true, encoding: .utf8)
try "[04:13:47] config changed\n".write(toFile: configLog, atomically: true, encoding: .utf8)
try manager.setAttributes([.modificationDate: fixtureDate], ofItemAtPath: menuLog)
try manager.setAttributes([.modificationDate: fixtureDate], ofItemAtPath: configLog)
try "{\"status\":\"success\",\"model_group\":\"test\",\"ts\":\"2026-07-20T20:13:44Z\",\"route_key\":\"\(longRoute)\"}\n".write(
    toFile: (root as NSString).appendingPathComponent("recent-requests.jsonl"), atomically: true, encoding: .utf8
)
try "{\"cooldowns\":{\"test\":{\"model_group\":\"test\",\"provider\":\"relay\",\"upstream_model\":\"test-model\",\"failures\":2,\"last_failure_at\":1595276029,\"cooldown_until\":0}}}\n".write(
    toFile: (root as NSString).appendingPathComponent(".litellm-runtime/deployment-cooldowns.json"), atomically: true, encoding: .utf8
)

_ = NSApplication.shared
TimeZone.ReferenceType.default = TimeZone(secondsFromGMT: 8 * 60 * 60)!
let logs = LogWindowController(runtimeRoot: root, bundleRoot: CommandLine.arguments[2])
logs.show(initialTab: .service)
RunLoop.current.run(until: Date().addingTimeInterval(1.2))
guard let window = logs.window,
      let content = window.contentView else {
    fail("logs window was not created")
}
let tabs = descendants(of: content).compactMap { $0 as? NSTabView }.first
guard tabs?.tabViewItems.map(\.label) == ["Requests", "Service", "Menu", "Config Watch", "Route Trace", "Recovery", "Online Usage"] else {
    fail("logs window tabs are incomplete")
}
guard let tabView = tabs else {
    fail("logs tab view was not created")
}

func renderedText(for label: String, containing expected: String, wait: TimeInterval = 0.25) -> NSTextView {
    guard let item = tabView.tabViewItems.first(where: { $0.label == label }) else {
        fail("missing \(label) tab")
    }
    tabView.selectTabViewItem(item)
    RunLoop.current.run(until: Date().addingTimeInterval(wait))
    guard let textView = descendants(of: content).compactMap({ $0 as? NSTextView }).first(where: {
        $0.string.contains(expected)
    }) else {
        fail("\(label) did not render \(expected)")
    }
    return textView
}

let activeText = descendants(of: content).compactMap { $0 as? NSTextView }.first { $0.string.contains("live service record") }
guard let activeText,
      activeText.string.contains("live service record"),
      activeText.string.contains("2026-07-21 04:13:44"),
      activeText.string.contains("2026-07-21 04:13:45"),
      activeText.string.contains("2026-07-21 04:30:00"),
      !activeText.string.contains("\n04:13:45"),
      !activeText.string.contains("—"),
      activeText.string.contains("LOCAL TIME (+0800)"),
      activeText.string.contains("SOURCE"),
      activeText.string.contains("STATUS"),
      activeText.string.contains("DETAIL"),
      !activeText.string.contains("\u{001B}["),
      activeText.textColor == NSColor.labelColor,
      activeText.textStorage?.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? NSColor == NSColor.labelColor,
      activeText.drawsBackground else {
    fail("service records were loaded but not rendered visibly")
}
guard let activeScrollView = activeText.enclosingScrollView,
      activeText.superview?.bounds.width ?? 0 > 0,
      activeText.superview?.bounds.height ?? 0 > 0,
      activeScrollView.contentView.bounds.width > 0,
      activeScrollView.contentView.bounds.height > 0,
      activeText.visibleRect.width > 0,
      activeText.visibleRect.height > 0 else {
    fail("service records were loaded but the log viewport has no visible area")
}
let serviceItem = tabs?.tabViewItems.first(where: { $0.label == "Service" })
tabs?.selectTabViewItem(serviceItem)
RunLoop.current.run(until: Date().addingTimeInterval(0.2))
guard activeText.string.contains("next record"),
      activeScrollView.contentView.bounds.width > 0,
      activeScrollView.contentView.bounds.height > 0 else {
    fail("service tab lost rendered records after selecting its tab")
}

let requestsText = renderedText(for: "Requests", containing: longRoute)
guard let requestsScroll = requestsText.enclosingScrollView,
      requestsScroll.hasHorizontalScroller,
      requestsText.isHorizontallyResizable,
      requestsText.textContainer?.widthTracksTextView == false,
      requestsText.string.contains(longRoute),
      !requestsText.string.contains("…"),
      requestsText.frame.width > requestsScroll.contentView.bounds.width else {
    fail("long request rows should retain their full text through a horizontal scroller")
}

for (label, expected) in [
    ("Requests", "2026-07-21 04:13:44"),
    ("Menu", "2026-07-21 04:13:46"),
    ("Config Watch", "2026-07-21 04:13:47"),
    ("Route Trace", "2026-07-21 04:13:48"),
    ("Recovery", "2020-07-21 04:13:49"),
] {
    let textView = renderedText(for: label, containing: expected)
    guard textView.string.contains("LOCAL TIME (+0800)") else {
        fail("\(label) did not use the unified local-time table")
    }
}
let onlineUsage = renderedText(for: "Online Usage", containing: "LOCAL TIME (+0800)", wait: 1.0)
guard onlineUsage.string.contains("SOURCE"),
      onlineUsage.string.contains("STATUS"),
      onlineUsage.string.contains("DETAIL") else {
    fail("Online Usage did not use the unified table")
}
window.close()
print("log-window-ok")
'''


@unittest.skipUnless(
    subprocess.run(["which", "swiftc"], capture_output=True).returncode == 0,
    "Log window harness requires swiftc.",
)
class LogWindowSwiftTests(unittest.TestCase):
    def test_tabs_and_service_text_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source = temp / "main.swift"
            source.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            binary = temp / "log-window"
            compiled = subprocess.run(
                [
                    "swiftc",
                    *(str(path) for path in SOURCES),
                    str(source),
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
                [str(binary), str(temp / "runtime"), str(ROOT)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "log-window-ok")


if __name__ == "__main__":
    unittest.main()
