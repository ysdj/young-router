from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted(
    path
    for path in (ROOT / "mac_menu" / "Sources").glob("*.swift")
    if path.name != "main.swift"
)


HARNESS = r'''
import Cocoa

func fail(_ value: String) -> Never {
    fputs(value + "\n", stderr)
    exit(1)
}

func descendants(of root: NSView) -> [NSView] {
    root.subviews + root.subviews.flatMap(descendants)
}

func frame(_ view: NSView, in ancestor: NSView) -> NSRect {
    ancestor.convert(view.bounds, from: view)
}

func approximatelyEqual(_ left: CGFloat, _ right: CGFloat, tolerance: CGFloat = 1) -> Bool {
    abs(left - right) <= tolerance
}

func structuredFormScroll(in pane: NSView) -> NSScrollView? {
    descendants(of: pane).compactMap { $0 as? NSScrollView }.first {
        $0.borderType == .bezelBorder && $0.documentView is FlippedDocumentView
    }
}

func assertStructuredFormGrid(
    window: NSWindow,
    content: NSView,
    size: NSSize,
    label: String
) {
    window.setContentSize(size)
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    content.layoutSubtreeIfNeeded()

    let actualSize = window.contentRect(forFrameRect: window.frame).size
    guard approximatelyEqual(actualSize.width, size.width),
          approximatelyEqual(actualSize.height, size.height) else {
        fail("\(label): Codex settings did not retain its requested content size: \(actualSize)")
    }

    guard let split = descendants(of: content).compactMap({ $0 as? NSSplitView }).first,
          split.arrangedSubviews.count == 2,
          let structuredScroll = structuredFormScroll(in: split.arrangedSubviews[0]),
          let document = structuredScroll.documentView,
          let form = document.subviews.compactMap({ $0 as? NSStackView }).first else {
        fail("\(label): Codex settings layout is missing its split panes or structured form")
    }

    let panes = split.arrangedSubviews.map { frame($0, in: content) }
    guard panes[0].width >= 400,
          panes[1].width >= 500 else {
        fail("\(label): Codex settings panes are too narrow: \(panes)")
    }

    let formFrame = frame(form, in: document)
    guard formFrame.minX >= 16,
          formFrame.minY >= 14,
          formFrame.maxX <= document.bounds.width - 16 + 1 else {
        fail("\(label): structured form does not retain its content inset: \(formFrame)")
    }

    let labelsByTitle = Dictionary(
        uniqueKeysWithValues: descendants(of: document)
            .compactMap { $0 as? NSTextField }
            .filter { ["Deployment", "Model", "Review model", "Model provider", "Endpoint URL", "API key", "Credential store", "Forced login", "Reasoning effort", "Plan reasoning", "Personality", "Service tier"].contains($0.stringValue) }
            .map { ($0.stringValue, $0) }
    )
    let requestedLabels = ["Deployment", "Model", "Review model", "Model provider", "Endpoint URL", "API key", "Credential store", "Forced login", "Reasoning effort", "Plan reasoning", "Personality", "Service tier"]
    guard requestedLabels.allSatisfy({ labelsByTitle[$0] != nil }) else {
        fail("\(label): expected structured form labels are missing")
    }

    let controls = descendants(of: document).filter {
        ($0 as? NSTextField)?.isEditable == true || $0 is NSPopUpButton
    }
    let pairs = requestedLabels.compactMap { title -> (NSTextField, NSView)? in
        guard let formLabel = labelsByTitle[title] else { return nil }
        let labelFrame = frame(formLabel, in: document)
        let candidates = controls.filter { control in
            let controlFrame = frame(control, in: document)
            return controlFrame.minX >= labelFrame.maxX - 1
                && approximatelyEqual(controlFrame.midY, labelFrame.midY, tolerance: 1)
        }
        guard let control = candidates.min(by: {
            frame($0, in: document).minX < frame($1, in: document).minX
        }) else {
            return nil
        }
        return (formLabel, control)
    }
    guard pairs.count == requestedLabels.count else {
        fail("\(label): every structured form label must have a control in its grid row")
    }

    let labelFrames = pairs.map { frame($0.0, in: document) }
    let controlFrames = pairs.map { frame($0.1, in: document) }
    guard let firstLabel = labelFrames.first,
          let firstControl = controlFrames.first,
          labelFrames.allSatisfy({ approximatelyEqual($0.minX, firstLabel.minX) && approximatelyEqual($0.width, firstLabel.width) }),
          controlFrames.allSatisfy({ approximatelyEqual($0.minX, firstControl.minX) && approximatelyEqual($0.maxX, firstControl.maxX) }),
          pairs.allSatisfy({ $0.0.alignment == .right }) else {
        fail("\(label): labels and controls do not share one explicit form grid: labels=\(labelFrames), controls=\(controlFrames)")
    }

    let sectionTitles = ["LiteLLM deployment", "Direct model connection", "Behavior", "Features", "Permissions", "Custom providers", "MCP & plugins", "Advanced"]
    let sectionHeadings = descendants(of: document).compactMap { $0 as? NSTextField }.filter {
        sectionTitles.contains($0.stringValue)
    }
    let sectionFrames = sectionHeadings.map { frame($0, in: document) }
    guard let firstSectionFrame = sectionFrames.first,
          sectionHeadings.count == sectionTitles.count,
          sectionHeadings.allSatisfy({ $0.alignment == .left }),
          sectionFrames.allSatisfy({ approximatelyEqual($0.minX, firstSectionFrame.minX) && approximatelyEqual($0.width, firstSectionFrame.width) }),
          abs(firstSectionFrame.minX - formFrame.minX) <= 3,
          abs(firstSectionFrame.maxX - formFrame.maxX) <= 3 else {
        fail("\(label): section headings do not share the structured form bounds: \(sectionFrames)")
    }
}

_ = NSApplication.shared
let dialog = CodexConfigDialogController(
    root: CommandLine.arguments[1],
    bundleRoot: CommandLine.arguments[2],
    environment: ProcessInfo.processInfo.environment,
    onApplied: {},
    onClose: {}
)
dialog.showWindow()
RunLoop.current.run(until: Date().addingTimeInterval(0.2))
guard let window = NSApp.windows.first(where: { $0.title == "Codex Settings" }),
      window.minSize.width == 1020,
      window.minSize.height == 620,
      let content = window.contentView else {
    fail("Codex settings window was not created with compact minimum dimensions")
}
guard let screen = window.screen ?? NSScreen.main else {
    fail("Codex settings window did not resolve a display for initial placement")
}
let visibleFrame = screen.visibleFrame
let windowFrame = window.frame
guard approximatelyEqual(windowFrame.midX, visibleFrame.midX),
      approximatelyEqual(windowFrame.midY, visibleFrame.midY) else {
    fail("Codex settings must open centered on its visible screen: window=\(windowFrame), visible=\(visibleFrame)")
}
window.orderOut(nil)
window.setFrameOrigin(NSPoint(x: visibleFrame.maxX - windowFrame.width, y: visibleFrame.minY))
dialog.showWindow()
RunLoop.current.run(until: Date().addingTimeInterval(0.05))
guard let reopenedScreen = window.screen ?? NSScreen.main else {
    fail("Codex settings did not resolve a display when reopened")
}
let reopenedVisibleFrame = reopenedScreen.visibleFrame
let reopenedFrame = window.frame
guard approximatelyEqual(reopenedFrame.midX, reopenedVisibleFrame.midX),
      approximatelyEqual(reopenedFrame.midY, reopenedVisibleFrame.midY) else {
    fail("Codex settings must recenter each time an unopened dialog is shown: window=\(reopenedFrame), visible=\(reopenedVisibleFrame)")
}
assertStructuredFormGrid(window: window, content: content, size: NSSize(width: 1120, height: 680), label: "default")
assertStructuredFormGrid(window: window, content: content, size: window.minSize, label: "minimum")
content.layoutSubtreeIfNeeded()
let textViews = descendants(of: content).compactMap { $0 as? NSTextView }
let editableTextViews = textViews.filter {
    $0.identifier == NSUserInterfaceItemIdentifier("CodexRawConfigText")
        || $0.identifier == NSUserInterfaceItemIdentifier("CodexRawAuthText")
}
guard editableTextViews.count == 2,
      editableTextViews.allSatisfy(\.isEditable),
      editableTextViews.allSatisfy({ $0.font != nil }),
      editableTextViews.allSatisfy({ $0.textContainerInset.width >= 12 && $0.textContainerInset.height >= 10 }) else {
    fail("Codex settings does not expose both identified editable raw file text areas")
}
let buttons = descendants(of: content).compactMap { $0 as? NSButton }
guard let close = buttons.first(where: { $0.title == "Close" }),
      let apply = buttons.first(where: { $0.title == "Apply" }),
      close.frame.minX < apply.frame.minX else {
    fail("Close must be immediately to the left of Apply")
}
guard descendants(of: content).contains(where: { ($0 as? NSSplitView) != nil }) else {
    fail("Codex settings must use a two-pane split view")
}
let labels = descendants(of: content).compactMap { $0 as? NSTextField }
let formLabelTitles = Set(["Deployment", "Endpoint URL", "Service tier"])
let formLabels = labels.filter { formLabelTitles.contains($0.stringValue) }
guard formLabels.count == formLabelTitles.count else {
    fail("Codex form labels are missing")
}
for label in formLabels {
    guard label.alignment == .right, label.frame.width <= 140 else {
        fail("Codex form label failed layout: \(label.stringValue) alignment=\(label.alignment.rawValue) width=\(label.frame.width)")
    }
}
guard formLabels.allSatisfy({ $0.alignment == .right }) else {
    fail("Codex form labels must stay compact and right aligned to the controls")
}
let borderedScrollViews = descendants(of: content).compactMap { $0 as? NSScrollView }
    .filter { $0.borderType == .bezelBorder }
guard borderedScrollViews.contains(where: { scroll in
    guard let document = scroll.documentView,
          let form = document.subviews.compactMap({ $0 as? NSStackView }).first else {
        return false
    }
    let frame = form.convert(form.bounds, to: document)
    return frame.minX >= 16 && frame.minY >= 14
}) else {
    fail("Codex structured settings must retain inset content inside its bordered scroll view")
}
let sectionTitles = labels.map(\.stringValue)
guard let liteLLM = sectionTitles.firstIndex(of: "LiteLLM deployment"),
      let direct = sectionTitles.firstIndex(of: "Direct model connection"),
      liteLLM < direct else {
    fail("LiteLLM deployment and direct-model settings must be separate sections")
}
guard labels.contains(where: { $0.stringValue == "Built-in OpenAI-compatible endpoint. URL uses openai_base_url; API key uses Codex's selected credential store." }) else {
    fail("Direct connection must explain the built-in OpenAI endpoint semantics")
}
guard let permissionMode = descendants(of: content).compactMap({ $0 as? NSSegmentedControl }).first,
      let approvalLabel = labels.first(where: { $0.stringValue == "Approval policy" }),
      let approvalControl = descendants(of: content).compactMap({ $0 as? NSPopUpButton }).first(where: {
          let controlFrame = frame($0, in: content)
          let labelFrame = frame(approvalLabel, in: content)
          return approximatelyEqual(controlFrame.midY, labelFrame.midY)
      }) else {
    fail("Permission controls are missing")
}
permissionMode.selectedSegment = 1
permissionMode.performClick(nil)
RunLoop.current.run(until: Date().addingTimeInterval(0.05))
guard approvalControl.isEnabled else {
    fail("Approval policy must remain editable with a permission profile")
}
let visibleEditableFields = descendants(of: content).compactMap { $0 as? NSTextField }
    .filter { $0.isEditable && $0.placeholderString == "(Empty)" }
guard !visibleEditableFields.isEmpty,
      !descendants(of: content).contains(where: { $0 is NSSecureTextField }) else {
    fail("Codex API keys must use visible plain text fields and unset fields must show (Empty)")
}
window.close()
print("codex-config-dialog-ok")
'''


@unittest.skipUnless(
    subprocess.run(["which", "swiftc"], capture_output=True).returncode == 0,
    "Codex config UI harness requires swiftc.",
)
class CodexConfigDialogSwiftTests(unittest.TestCase):
    def test_window_has_structured_and_raw_panes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source = temp / "main.swift"
            source.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            binary = temp / "codex-config-dialog"
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
            self.assertEqual(result.stdout.strip(), "codex-config-dialog-ok")

    def test_source_uses_minimal_immediate_structured_patches(self) -> None:
        source = (ROOT / "mac_menu" / "Sources" / "CodexConfigDialog.swift").read_text(
            encoding="utf-8"
        )
        self.assertIn("scheduleStructuredSync(for: field)", source)
        self.assertIn("DispatchQueue.main.asyncAfter(deadline: .now() + 0.15", source)
        self.assertIn("private func patch(for control: NSView?)", source)
        self.assertIn("private func listPatch()", source)
        self.assertNotIn("currentStructured()", source)
        self.assertIn('configurePopup(mcpTransportPopup, items: ["stdio", "http"])', source)


if __name__ == "__main__":
    unittest.main()
