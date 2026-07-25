from __future__ import annotations

import os
import stat
import subprocess
import sys
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


HARNESS = r"""
import Cocoa
import Foundation

func fail(_ message: String) -> Never {
    fputs("\(message)\n", stderr)
    exit(1)
}

func descendants(of root: NSView) -> [NSView] {
    root.subviews + root.subviews.flatMap(descendants)
}

func frame(_ view: NSView, in ancestor: NSView) -> NSRect {
    ancestor.convert(view.bounds, from: view)
}

func framesMatch(_ left: NSRect, _ right: NSRect, tolerance: CGFloat = 1) -> Bool {
    abs(left.minX - right.minX) <= tolerance
        && abs(left.minY - right.minY) <= tolerance
        && abs(left.width - right.width) <= tolerance
        && abs(left.height - right.height) <= tolerance
}

func assertSelectedDetailStartsAtViewportTop(
    _ controller: ModelConfigEditorController,
    section: NSView,
    label: String
) {
    guard let content = controller.window.contentView,
          let detailScroll = controller.detailScrollView else {
        fail("\(label): detail viewport is unavailable")
    }
    let clipFrame = frame(detailScroll.contentView, in: content)
    let sectionFrame = frame(section, in: content)
    guard abs(sectionFrame.maxY - clipFrame.maxY) <= 12 else {
        fail(
            "\(label): selected detail starts away from the viewport top: "
            + "section=\(sectionFrame), clip=\(clipFrame)"
        )
    }
}

func assertProviderEditorGeometry(
    _ controller: ModelConfigEditorController,
    section: NSView,
    label: String
) {
    guard let detailDocument = controller.detailDocumentView,
          let keyScroll = controller.providerKeyTableView.enclosingScrollView else {
        fail("\(label): provider detail controls were not retained")
    }

    let providerNameFrame = frame(controller.providerNameField, in: detailDocument)
    let providerBaseURLFrame = frame(controller.providerApiBaseField, in: detailDocument)
    let keyNameFrame = frame(controller.providerKeyNameField, in: detailDocument)
    let keyValueFrame = frame(controller.providerApiKeyField, in: detailDocument)
    let sectionFrame = frame(section, in: detailDocument)
    let trailingInsets = [
        sectionFrame.maxX - providerNameFrame.maxX,
        sectionFrame.maxX - providerBaseURLFrame.maxX,
        sectionFrame.maxX - keyNameFrame.maxX,
        sectionFrame.maxX - keyValueFrame.maxX,
    ]
    guard abs(providerNameFrame.minX - providerBaseURLFrame.minX) < 1,
          providerBaseURLFrame.minY < providerNameFrame.minY,
          abs(keyNameFrame.minX - keyValueFrame.minX) < 1,
          providerNameFrame.minX < keyNameFrame.minX,
          trailingInsets.allSatisfy({ $0 >= 6 }),
          !keyScroll.hasHorizontalScroller else {
        fail(
            "\(label): provider fields are not ordered/aligned or the key list scrolls horizontally: "
            + "name=\(providerNameFrame), base=\(providerBaseURLFrame), "
            + "keyName=\(keyNameFrame), keyValue=\(keyValueFrame), "
            + "trailingInsets=\(trailingInsets), "
            + "hasHorizontalScroller=\(keyScroll.hasHorizontalScroller)"
        )
    }

    assertSelectedDetailStartsAtViewportTop(controller, section: section, label: label)
}

func assertActionRowsStartAtContentLeadingEdge(
    _ controller: ModelConfigEditorController,
    providerSection: NSView,
    modelSection: NSView,
    label: String
) {
    guard let detailDocument = controller.detailDocumentView else {
        fail("\(label): detail document is unavailable")
    }
    let providerSectionFrame = frame(providerSection, in: detailDocument)
    let modelSectionFrame = frame(modelSection, in: detailDocument)
    let providerEnabledFrame = frame(controller.providerEnabledCheckbox, in: detailDocument)
    let modelEnabledFrame = frame(controller.enabledCheckbox, in: detailDocument)
    guard providerEnabledFrame.minX - providerSectionFrame.minX < 24,
          modelEnabledFrame.minX - modelSectionFrame.minX < 24 else {
        fail(
            "\(label): action rows reserve an empty label column: "
            + "provider=\(providerEnabledFrame), model=\(modelEnabledFrame), "
            + "providerSection=\(providerSectionFrame), modelSection=\(modelSectionFrame)"
        )
    }
}

func assertRoutesWorkspaceGeometry(
    _ controller: ModelConfigEditorController,
    size: NSSize,
    label: String
) {
    controller.window.setContentSize(size)
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    controller.window.contentView?.layoutSubtreeIfNeeded()

    guard let content = controller.window.contentView,
          let editorWorkspaceStack = controller.editorWorkspaceStack,
          let modeWorkspaceColumn = controller.modeWorkspaceColumn,
          let modeWorkspaceHost = controller.modeWorkspaceHost,
          let providersWorkspace = controller.providersWorkspace,
          let routesWorkspace = controller.routesWorkspace,
          let routeScroll = controller.routeTableScrollView,
          let detailScroll = controller.detailScrollView,
          let modeToolbar = controller.viewModeControl.superview else {
        fail("\(label): routes workspace was not retained")
    }
    let contentFrame = frame(content, in: content)
    let editorWorkspaceFrame = frame(editorWorkspaceStack, in: content)
    let modeColumnFrame = frame(modeWorkspaceColumn, in: content)
    let modeHostFrame = frame(modeWorkspaceHost, in: content)
    let modeToolbarFrame = frame(modeToolbar, in: content)
    let providersFrame = frame(providersWorkspace, in: content)
    let routesFrame = frame(routesWorkspace, in: content)
    let detailFrame = frame(detailScroll, in: content)
    guard providersWorkspace.isHidden,
          !routesWorkspace.isHidden,
          abs(editorWorkspaceFrame.minX - contentFrame.minX - 16) < 1,
          abs(editorWorkspaceFrame.maxX - contentFrame.maxX + 16) < 1,
          abs(editorWorkspaceFrame.maxY - contentFrame.maxY + 16) < 1,
          modeWorkspaceColumn.superview === editorWorkspaceStack,
          editorWorkspaceStack.arrangedSubviews.contains(where: { $0 === modeWorkspaceColumn }),
          modeWorkspaceHost.superview === modeWorkspaceColumn,
          modeWorkspaceColumn.arrangedSubviews.contains(where: { $0 === modeWorkspaceHost }),
          modeToolbar.superview === modeWorkspaceColumn,
          modeWorkspaceColumn.arrangedSubviews.contains(where: { $0 === modeToolbar }),
          abs(modeColumnFrame.maxY - detailFrame.maxY) < 1,
          abs(modeHostFrame.minY - detailFrame.minY) < 1,
          detailFrame.maxY - modeHostFrame.maxY >= 41,
          modeToolbarFrame.minY - modeHostFrame.maxY >= 13,
          framesMatch(providersFrame, routesFrame),
          framesMatch(routesFrame, modeHostFrame),
          !routeScroll.hasHorizontalScroller,
          detailScroll.superview === editorWorkspaceStack,
          editorWorkspaceStack.arrangedSubviews.contains(where: { $0 === detailScroll }),
          detailFrame.minX > modeHostFrame.maxX,
          detailScroll.frame.width >= 340,
          controller.routeTableView.tableColumns.reduce(CGFloat.zero, { $0 + $1.width })
            <= routeScroll.contentView.bounds.width + 1,
          controller.routeTableView.tableColumns.map(\.title) == ["Model", "Order", "Provider / Key", "Upstream"] else {
        fail(
            "\(label): routes does not retain its non-scrolling table and shared editor: "
            + "routes=\(routesFrame), content=\(contentFrame), "
            + "workspace=\(editorWorkspaceFrame), column=\(modeColumnFrame), toolbar=\(modeToolbarFrame), host=\(modeHostFrame), detail=\(detailFrame), "
            + "hidden=\(routesWorkspace.isHidden), horizontal=\(routeScroll.hasHorizontalScroller), "
            + "detailParent=\(String(describing: detailScroll.superview)), "
            + "columns=\(controller.routeTableView.tableColumns.map(\.title))"
        )
    }
}

func assertRoutesUseSharedModelEditor(
    _ controller: ModelConfigEditorController,
    label: String
) {
    guard let editorWorkspaceStack = controller.editorWorkspaceStack,
          let modeWorkspaceHost = controller.modeWorkspaceHost,
          let detailScroll = controller.detailScrollView,
          controller.modelDetailView?.isHidden == false,
          controller.providerDetailView?.isHidden == true,
          detailScroll.superview === editorWorkspaceStack,
          controller.routesWorkspace?.superview === modeWorkspaceHost,
          controller.modelProviderPopupButton.numberOfItems == 2,
          controller.modelProviderPopupButton.titleOfSelectedItem == "primary",
          controller.modelBreadcrumbProviderButton.title == "primary",
          controller.modelBreadcrumbModelLabel.stringValue == "test-chat" else {
        fail("\(label): routes does not use the shared model editor and breadcrumb: detailParent=\(String(describing: controller.detailScrollView?.superview)), providerItems=\(controller.modelProviderPopupButton.numberOfItems), provider=\(controller.modelProviderPopupButton.titleOfSelectedItem ?? ""), breadcrumb=\(controller.modelBreadcrumbProviderButton.title) > \(controller.modelBreadcrumbModelLabel.stringValue)")
    }
}

func assertModelEditorGeometry(
    _ controller: ModelConfigEditorController,
    size: NSSize,
    label: String
) {
    controller.window.setContentSize(size)
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    controller.window.contentView?.layoutSubtreeIfNeeded()

    guard let content = controller.window.contentView,
          let footer = controller.editorFooterView,
          let detailScroll = controller.detailScrollView,
          let detailDocument = controller.detailDocumentView,
          let modelSection = controller.modelDetailView,
          let modelScroll = controller.modelTableScrollView,
          let routeScroll = controller.routeTableScrollView else {
        fail("\(label): model editor layout views were not retained")
    }

    let footerFrame = frame(footer, in: content)
    let detailFrame = frame(detailScroll, in: content)
    guard !detailFrame.intersects(footerFrame),
          detailFrame.minY >= footerFrame.maxY else {
        fail("\(label): detail viewport overlaps the fixed footer: detail=\(detailFrame), footer=\(footerFrame)")
    }

    let detailDocumentFrame = frame(detailDocument, in: detailDocument)
    let modelSectionFrame = frame(modelSection, in: detailDocument)
    guard modelSectionFrame.minX >= detailDocumentFrame.minX - 1,
          modelSectionFrame.maxX <= detailDocumentFrame.maxX + 1,
          modelSectionFrame.minY >= detailDocumentFrame.minY - 1,
          modelSectionFrame.maxY <= detailDocumentFrame.maxY + 1 else {
        fail("\(label): model detail section escapes its scrolling document: section=\(modelSectionFrame), document=\(detailDocumentFrame)")
    }

    let controls: [(String, NSView)] = [
        ("Balance", controller.modelBillingStatusLabel),
        ("Usage", controller.modelUsageStatusLabel),
        ("Multiplier", controller.modelMultiplierStatusLabel),
        ("Public model", controller.modelNameField),
        ("Provider", controller.modelProviderPopupButton),
        ("API key", controller.modelApiKeyPopupButton),
        ("Upstream", controller.upstreamModelField),
        ("Order", controller.orderField),
        ("API order", controller.upstreamApiModeStackView),
    ]
for (name, control) in controls {
        let documentFrame = frame(control, in: detailDocument)
        guard documentFrame.minX >= detailDocumentFrame.minX - 1,
              documentFrame.maxX <= detailDocumentFrame.maxX + 1,
              documentFrame.height > 0 else {
            fail("\(label): \(name) is outside the detail document: control=\(documentFrame), document=\(detailDocumentFrame)")
        }
    }

    let orderFrame = frame(controller.orderField, in: detailDocument)
    let apiOrderFrame = frame(controller.upstreamApiModeStackView, in: detailDocument)
    guard !orderFrame.intersects(apiOrderFrame) else {
        fail("\(label): multi-line API order controls overlap the order field: order=\(orderFrame), apiOrder=\(apiOrderFrame)")
    }

    func assertVisibleControlsStayAboveFooter(_ position: String) {
        let clip = detailScroll.contentView
        let clipFrame = frame(clip, in: content)
        guard !clipFrame.intersects(footerFrame) else {
            fail("\(label) \(position): detail clip view overlaps the footer: clip=\(clipFrame), footer=\(footerFrame)")
        }
        for (name, control) in controls {
            let controlFrame = frame(control, in: content)
            let visibleFrame = controlFrame.intersection(clipFrame)
            guard visibleFrame.isNull || !visibleFrame.intersects(footerFrame) else {
                fail("\(label) \(position): visible \(name) overlaps the footer: control=\(controlFrame), visible=\(visibleFrame), clip=\(clipFrame), footer=\(footerFrame)")
            }
        }
    }

    detailScroll.contentView.scroll(to: .zero)
    detailScroll.reflectScrolledClipView(detailScroll.contentView)
    controller.window.contentView?.layoutSubtreeIfNeeded()
    assertVisibleControlsStayAboveFooter("at top")

    let bottom = max(0, detailDocument.bounds.height - detailScroll.contentView.bounds.height)
    detailScroll.contentView.scroll(to: NSPoint(x: 0, y: bottom))
    detailScroll.reflectScrolledClipView(detailScroll.contentView)
    controller.window.contentView?.layoutSubtreeIfNeeded()
    assertVisibleControlsStayAboveFooter("at bottom")

    let modelColumnsFit = controller.modelTableView.tableColumns.reduce(CGFloat.zero) { $0 + $1.width }
        <= modelScroll.contentView.bounds.width + 1
    guard modelColumnsFit, !modelScroll.hasHorizontalScroller else {
        fail(
            "\(label): model table shows an unnecessary horizontal scrollbar: "
            + "columns=\(controller.modelTableView.tableColumns.map(\.width)), "
            + "content=\(modelScroll.contentView.bounds.width), horizontal=\(modelScroll.hasHorizontalScroller)"
        )
    }
    _ = routeScroll
}

let root = CommandLine.arguments[1]
let bundleRoot = CommandLine.arguments[2]
let controller = ModelConfigEditorController(
    root: root,
    bundleRoot: bundleRoot,
    environment: ProcessInfo.processInfo.environment,
    onSaved: { _ in },
    onClose: {}
)

_ = NSApplication.shared
controller.buildWindow()
guard controller.window != nil, controller.window.contentView != nil else {
    fail("model editor window did not build")
}

controller.window.makeKeyAndOrderFront(nil)
RunLoop.current.run(until: Date().addingTimeInterval(0.1))
controller.window.contentView?.layoutSubtreeIfNeeded()
guard let providerPane = controller.providerPane,
      let modelsRoutesPane = controller.modelsRoutesPane,
      let detailScroll = controller.detailScrollView,
      let providersContentStack = controller.providersContentStack,
      let editorWorkspaceStack = controller.editorWorkspaceStack,
      let modeWorkspaceColumn = controller.modeWorkspaceColumn,
      let modeWorkspaceHost = controller.modeWorkspaceHost,
      let providersWorkspace = controller.providersWorkspace,
      let routesWorkspace = controller.routesWorkspace else {
    fail("model editor layout panes were not retained")
}
guard abs(providerPane.frame.width - 196) < 1 else {
    fail("provider pane did not retain its compact width: \(providerPane.frame.width)")
}
guard modelsRoutesPane.frame.width >= 460,
      providerPane.frame.width < modelsRoutesPane.frame.width else {
    fail("provider pane is not narrower than the models pane")
}
guard controller.window.frame.width < 1320,
      controller.window.frame.height < 720,
      controller.window.minSize.width == 1052,
      controller.window.minSize.height == 560 else {
    fail(
        "model editor window is not compact or does not preserve the three-pane form: "
        + "frame=\(controller.window.frame.size), min=\(controller.window.minSize)"
    )
}
guard let contentView = controller.window.contentView else {
    fail("model editor window has no content view")
}
guard detailScroll.frame.width >= 340,
      modeWorkspaceHost.frame.width <= 681,
      providerPane.frame.width < modelsRoutesPane.frame.width,
      detailScroll.superview === editorWorkspaceStack,
      modeWorkspaceColumn.superview === editorWorkspaceStack,
      modeWorkspaceHost.superview === modeWorkspaceColumn,
      controller.viewModeControl.superview?.superview === modeWorkspaceColumn,
      providersWorkspace.superview === modeWorkspaceHost,
      routesWorkspace.superview === modeWorkspaceHost,
      detailScroll.frame.maxY - modeWorkspaceHost.frame.maxY >= 41,
      abs(detailScroll.frame.minY - modeWorkspaceHost.frame.minY) < 1,
      abs(providersContentStack.frame.width - modeWorkspaceHost.bounds.width) < 1,
      framesMatch(providersWorkspace.frame, routesWorkspace.frame) else {
    fail(
        "editor panes did not retain their intended three-pane layout: "
        + "detail \(detailScroll.frame.width), provider \(providerPane.frame.width), "
        + "models \(modelsRoutesPane.frame.width), content \(contentView.frame.width), "
        + "modeHost \(modeWorkspaceHost.frame), providers \(providersWorkspace.frame), "
        + "routes \(routesWorkspace.frame), detail \(detailScroll.frame)"
    )
}
guard controller.providerTableView.tableColumns.last?.headerCell is TrailingSeparatorlessTableHeaderCell,
      controller.modelTableView.tableColumns.last?.headerCell is TrailingSeparatorlessTableHeaderCell,
      controller.routeTableView.tableColumns.last?.headerCell is TrailingSeparatorlessTableHeaderCell,
      !controller.providerTableView.allowsColumnReordering,
      !controller.modelTableView.allowsColumnReordering,
      !controller.routeTableView.allowsColumnReordering else {
    fail("editor tables retained a redundant trailing header separator or allow the custom trailing cell to move")
}
var syntheticModel = EditableModel.blank()
syntheticModel.provider = "primary"
syntheticModel.modelName = "test-chat"
syntheticModel.litellmModel = "openai/test-chat"
syntheticModel.apiKeyName = "default"
syntheticModel.apiKey = "replace-me"
syntheticModel.deploymentToken = "deployment-a"
var backupModel = EditableModel.blank()
backupModel.provider = "backup"
backupModel.modelName = "test-chat"
backupModel.litellmModel = "openai/backup-chat"
backupModel.apiKeyName = "backup-key"
backupModel.apiKey = "replace-backup"
backupModel.deploymentToken = "deployment-b"
controller.providers = [
    EditableProvider(
        name: "primary",
        enabled: true,
        apiBase: "https://primary.example.test/v1",
        apiKey: "replace-me",
        apiKeys: [EditableProviderKey(name: "default", value: "replace-me")],
        models: [syntheticModel],
        extra: [:]
    ),
    EditableProvider(
        name: "backup",
        enabled: true,
        apiBase: "https://backup.example.test/v1",
        apiKey: "replace-backup",
        apiKeys: [EditableProviderKey(name: "backup-key", value: "replace-backup")],
        models: [backupModel],
        extra: [:]
    ),
]
let loadedPayload = ConfigEditorLoadPayload(
    providers: controller.providers,
    revision: nil,
    document: nil
)
controller.applyLoadedConfiguration(loadedPayload)
guard controller.selectedProviderIndex == 0,
      controller.selectedModelIndex == nil,
      controller.modelDetailView?.isHidden == true,
      !controller.hasPendingChanges,
      !controller.applyButton.isEnabled else {
    fail("loading configuration must populate the first provider without auto-selecting a model")
}
guard let selectedProviderSection = controller.providerDetailView else {
    fail("provider detail section was not retained")
}
assertProviderEditorGeometry(
    controller,
    section: selectedProviderSection,
    label: "provider selection"
)
controller.providerNameField.stringValue = "primary-temporary"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.providerNameField))
guard controller.hasPendingChanges, controller.applyButton.isEnabled else {
    fail("editing a provider did not enable Apply")
}
controller.providerNameField.stringValue = "primary"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.providerNameField))
guard !controller.hasPendingChanges, !controller.applyButton.isEnabled else {
    fail("reverting a provider edit did not clear the pending configuration state")
}
controller.providerApiKeyField.stringValue = "replace-live"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.providerApiKeyField))
guard controller.providers[0].apiKeys[0].value == "replace-live",
      controller.modelCandidateApiKeyPopupButton.itemArray.contains(where: { $0.title.contains("replace-live") }) else {
    fail("editing an API key did not immediately refresh the Fetch key picker")
}
controller.providerNameField.stringValue = ""
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.providerNameField))
guard controller.uniqueProviderName(for: "backup", excluding: 0) == "backup (2)" else {
    fail("provider URL name derivation did not disambiguate duplicate names")
}
controller.providerApiBaseField.stringValue = "https://a"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.providerApiBaseField))
controller.providerApiBaseField.stringValue = "https://api.aaa.bbb/v1"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.providerApiBaseField))
guard controller.providers[0].name == "aaa",
      controller.providers[0].models[0].provider == "aaa" else {
    fail("editing a provider URL did not derive its primary-domain name")
}
controller.controlTextDidEndEditing(Notification(name: NSControl.textDidEndEditingNotification, object: controller.providerApiBaseField))
controller.providerApiBaseField.stringValue = "https://abc.com/v1"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.providerApiBaseField))
guard controller.providers[0].name == "aaa",
      controller.providers[0].apiBase == "https://abc.com/v1" else {
    fail("editing a provider URL overwrote a generated provider name")
}
controller.providerNameField.stringValue = "primary-live"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.providerNameField))
guard controller.providers[0].name == "primary-live",
      controller.providerEditorTitleLabel.stringValue == "Provider: primary-live",
      (controller.tableView(
          controller.providerTableView,
          viewFor: controller.providerTableView.tableColumns[0],
          row: 0
      ) as? NSTableCellView)?.textField?.stringValue == "primary-live" else {
    fail("editing a provider name did not immediately refresh the provider table")
}
controller.providerNameField.stringValue = "primary"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.providerNameField))
controller.showModel(providerIndex: 0, modelIndex: 0)
RunLoop.current.run(until: Date().addingTimeInterval(0.1))
controller.window.contentView?.layoutSubtreeIfNeeded()
guard let selectedModelSection = controller.modelDetailView else {
    fail("model detail section was not retained")
}
assertSelectedDetailStartsAtViewportTop(
    controller,
    section: selectedModelSection,
    label: "model selection"
)
assertActionRowsStartAtContentLeadingEdge(
    controller,
    providerSection: selectedProviderSection,
    modelSection: selectedModelSection,
    label: "detail action rows"
)
let fullProbeSummary = "Uncertain: HTTP 503: type=api_error with complete diagnostic text"
let fullProbeDetail = "HTTP 503\ntype=api_error\nThe complete upstream diagnostic remains available on hover."
controller.modelProbePresentations[ModelConfigEditorController.ModelProbeKey(
    providerID: controller.providers[0].editorID,
    modelID: controller.providers[0].models[0].editorID
)] = ModelConfigEditorController.ModelProbePresentation(
    state: .inconclusive,
    summary: fullProbeSummary,
    detail: fullProbeDetail
)
controller.renderSelectedModelProbePresentation(for: controller.selectedModelProbeKey())
guard controller.modelProbeStatusLabel.stringValue == fullProbeSummary,
      controller.modelProbeStatusLabel.toolTip?.contains(fullProbeSummary) == true,
      controller.modelProbeStatusLabel.toolTip?.contains(fullProbeDetail) == true else {
    fail("truncated probe status does not expose its complete summary and detail in the tooltip")
}
assertModelEditorGeometry(controller, size: controller.window.contentLayoutRect.size, label: "default")
assertModelEditorGeometry(controller, size: controller.window.minSize, label: "minimum")

guard let providerSection = controller.providerDetailView,
      let providerNameLabel = descendants(of: providerSection)
          .compactMap({ $0 as? NSTextField })
          .first(where: { $0.stringValue == "Provider name" }),
      providerNameLabel.alignment == .left else {
    fail("provider form labels are not left aligned")
}
guard let modelSection = controller.modelDetailView,
      modelSection.subviews.compactMap({ $0 as? NSStackView }).contains(where: { $0.arrangedSubviews.count > 7 }),
      controller.modelBreadcrumbProviderButton.title == "primary",
      controller.modelBreadcrumbModelLabel.stringValue == "test-chat",
      let detailPane = modelSection.superview else {
    fail("model settings hierarchy is incomplete")
}
let labelControls: [(String, NSView)] = [
    ("Balance", controller.modelBillingStatusLabel),
    ("Usage", controller.modelUsageStatusLabel),
    ("Multiplier", controller.modelMultiplierStatusLabel),
    ("Public model", controller.modelNameField),
    ("Provider", controller.modelProviderPopupButton),
    ("API key", controller.modelApiKeyPopupButton),
    ("Upstream", controller.upstreamModelField),
    ("Order", controller.orderField),
    ("API order", controller.upstreamApiModeStackView),
]
let sectionLabels = descendants(of: modelSection).compactMap { $0 as? NSTextField }
let labelPositions = labelControls.compactMap { title, _ in
    sectionLabels.first(where: { $0.stringValue == title }).map { label in
        modelSection.convert(label.bounds, from: label).minX
    }
}
let controlPositions = labelControls.map { _, control in
    modelSection.convert(control.bounds, from: control).minX
}
guard labelPositions.count == labelControls.count,
      let firstLabelPosition = labelPositions.first,
      let firstControlPosition = controlPositions.first,
      labelPositions.allSatisfy({ abs($0 - firstLabelPosition) < 1 }),
      controlPositions.allSatisfy({ abs($0 - firstControlPosition) < 1 }) else {
    fail(
        "model detail rows do not share fixed left-aligned label and control columns: "
        + "labels=\(labelPositions), controls=\(controlPositions), detail=\(detailPane.frame)"
    )
}
controller.modelNameField.stringValue = "test-chat-live"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.modelNameField))
guard controller.providers[0].models[0].modelName == "test-chat-live",
      controller.modelBreadcrumbModelLabel.stringValue == "test-chat-live",
      controller.routeRows().contains(where: { $0.publicModel == "test-chat-live" }) else {
    fail("editing a model name did not immediately refresh the breadcrumb and routes")
}
controller.modelNameField.stringValue = "test-chat"
controller.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: controller.modelNameField))
guard let contentBeforeModeSwitch = controller.window.contentView,
      let modeHostBeforeSwitch = controller.modeWorkspaceHost,
      let detailBeforeSwitch = controller.detailScrollView,
      let footerBeforeSwitch = controller.editorFooterView else {
    fail("stable editor chrome was not retained before switching modes")
}
let stableWindowFrame = controller.window.frame
let stableModeControlFrame = frame(controller.viewModeControl, in: contentBeforeModeSwitch)
let stableModeHostFrame = frame(modeHostBeforeSwitch, in: contentBeforeModeSwitch)
let stableDetailFrame = frame(detailBeforeSwitch, in: contentBeforeModeSwitch)
let stableFooterFrame = frame(footerBeforeSwitch, in: contentBeforeModeSwitch)

func assertStableEditorChrome(_ label: String) {
    guard let content = controller.window.contentView,
          let modeHost = controller.modeWorkspaceHost,
          let detail = controller.detailScrollView,
          let footer = controller.editorFooterView,
          framesMatch(controller.window.frame, stableWindowFrame),
          framesMatch(frame(controller.viewModeControl, in: content), stableModeControlFrame),
          framesMatch(frame(modeHost, in: content), stableModeHostFrame),
          framesMatch(frame(detail, in: content), stableDetailFrame),
          framesMatch(frame(footer, in: content), stableFooterFrame) else {
        fail(
            "\(label): switching modes moved the editor chrome: "
            + "window=\(controller.window.frame), mode=\(frame(controller.viewModeControl, in: controller.window.contentView!)), "
            + "host=\(String(describing: controller.modeWorkspaceHost?.frame)), "
            + "detail=\(String(describing: controller.detailScrollView?.frame)), "
            + "footer=\(String(describing: controller.editorFooterView?.frame))"
        )
    }
}

controller.viewMode = .routes
controller.applyEditorViewMode()
controller.window.contentView?.layoutSubtreeIfNeeded()
assertStableEditorChrome("Providers to Routes")
controller.viewMode = .providers
controller.applyEditorViewMode()
controller.window.contentView?.layoutSubtreeIfNeeded()
assertStableEditorChrome("Routes back to Providers")
controller.viewMode = .routes
controller.applyEditorViewMode()
controller.window.contentView?.layoutSubtreeIfNeeded()
assertStableEditorChrome("Providers back to Routes")
assertRoutesWorkspaceGeometry(controller, size: controller.window.contentLayoutRect.size, label: "routes default")
assertRoutesWorkspaceGeometry(controller, size: controller.window.minSize, label: "routes minimum")
let initialRouteRows = controller.routeTableRows()
guard initialRouteRows.count == 2,
      initialRouteRows.allSatisfy({ $0.publicModel == "test-chat" }),
      controller.routeStartsModelGroup(atTableRow: 0),
      !controller.routeStartsModelGroup(atTableRow: 1),
      controller.routeTableView.numberOfRows == 2,
      !controller.tableView(controller.routeTableView, isGroupRow: 0),
      controller.tableView(controller.routeTableView, shouldSelectRow: 0) else {
    fail("routes do not combine the model name with its first deployment row: count=\(initialRouteRows.count), table=\(controller.routeTableView.numberOfRows), first=\(controller.routeStartsModelGroup(atTableRow: 0)), second=\(controller.routeStartsModelGroup(atTableRow: 1)), selected=\(controller.routeTableView.selectedRow), route=\(String(describing: controller.selectedRouteRow()))")
}
guard controller.selectedRouteRow()?.providerIndex == 0,
      controller.routeTableView.selectedRow >= 0 else {
    fail("routes did not keep the selected deployment after merging the model title: selected=\(controller.routeTableView.selectedRow), route=\(String(describing: controller.selectedRouteRow()))")
}
guard let secondRouteIdentity = controller.modelSelectionIdentity(providerIndex: 1, modelIndex: 0) else {
    fail("routes could not create a stable identity for the second deployment")
}
controller.pendingRouteSelectionIdentity = secondRouteIdentity
controller.reloadRouteTable()
guard controller.selectedRouteIdentity == secondRouteIdentity,
      controller.selectedRouteRow()?.providerIndex == 1,
      controller.selectedRouteRow()?.modelIndex == 0,
      controller.pendingRouteSelectionIdentity == nil else {
    fail("routes reload did not restore the clicked deployment by stable identity")
}
controller.pendingRouteSelectionIdentity = controller.modelSelectionIdentity(providerIndex: 0, modelIndex: 0)
controller.reloadRouteTable()
guard controller.selectedRouteRow()?.providerIndex == 0,
      controller.selectedRouteRow()?.modelIndex == 0 else {
    fail("routes could not restore the original deployment after identity selection")
}
assertRoutesUseSharedModelEditor(controller, label: "routes shared model editor")
controller.modelNameField.stringValue = "test-chat-routed"
controller.upstreamModelField.stringValue = "upstream-routed"
controller.orderField.stringValue = "-0.5"
controller.commitModelEditor()
RunLoop.current.run(until: Date().addingTimeInterval(0.05))
guard controller.providers[0].models[0].modelName == "test-chat-routed",
      controller.providers[0].models[0].litellmModel == "openai/upstream-routed",
      controller.providers[0].models[0].order == "-0.5",
      controller.routeTableRows().first(where: { $0.publicModel == "test-chat-routed" })?.order
        == Decimal(string: "-0.5", locale: Locale(identifier: "en_US_POSIX")) else {
    fail("routes shared model editor did not persist model edits")
}
guard let fractionalRouteRow = controller.routeTableRows().firstIndex(where: {
          $0.publicModel == "test-chat-routed" && $0.order == Decimal(string: "-0.5", locale: Locale(identifier: "en_US_POSIX"))
      }),
      let orderCell = controller.tableView(
          controller.routeTableView,
          viewFor: controller.routeTableView.tableColumn(withIdentifier: controller.routeOrderColumnIdentifier),
          row: fractionalRouteRow
      ) as? NSTableCellView,
      orderCell.textField?.stringValue == "-0.5" else {
    fail("routes Order cell does not display its signed fractional value")
}
controller.modelBreadcrumbProviderClicked(controller.modelBreadcrumbProviderButton)
RunLoop.current.run(until: Date().addingTimeInterval(0.05))
controller.window.contentView?.layoutSubtreeIfNeeded()
guard controller.viewMode == .routes,
      controller.detailScrollView?.superview === controller.editorWorkspaceStack,
      controller.routesWorkspace?.superview === controller.modeWorkspaceHost,
      controller.routesWorkspace?.isHidden == false,
      controller.providersWorkspace?.isHidden == true,
      controller.providerDetailView?.isHidden == false,
      controller.modelDetailView?.isHidden == true,
      controller.providerNameField.stringValue == "primary",
      controller.providerEditorTitleLabel.stringValue == "Provider: primary",
      controller.providerReturnToModelButton.title == "Back to model test-chat-routed",
      controller.providerReturnToModelButton.isHidden == false,
      controller.providerReturnToModelButton.isEnabled,
      controller.selectedRouteRow()?.providerIndex == 0,
      controller.selectedRouteRow()?.modelIndex == 0 else {
    fail("model breadcrumb did not open the provider detail with a distinct return link in the existing Routes workspace")
}
controller.providerReturnToModelClicked(controller.providerReturnToModelButton)
RunLoop.current.run(until: Date().addingTimeInterval(0.05))
guard controller.viewMode == .routes,
      controller.detailScrollView?.superview === controller.editorWorkspaceStack,
      controller.routesWorkspace?.superview === controller.modeWorkspaceHost,
      controller.providerDetailView?.isHidden == true,
      controller.modelDetailView?.isHidden == false,
      controller.modelBreadcrumbProviderButton.title == "primary",
      controller.modelBreadcrumbModelLabel.stringValue == "test-chat-routed",
      controller.selectedRouteRow()?.providerIndex == 0,
      controller.selectedRouteRow()?.modelIndex == 0 else {
    fail("provider return link did not return to the existing model detail")
}
controller.modelProviderPopupButton.selectItem(at: 1)
controller.modelProviderSelectionChanged(controller.modelProviderPopupButton)
RunLoop.current.run(until: Date().addingTimeInterval(0.1))
guard controller.providers[0].models.isEmpty,
      controller.providers[1].models.count == 2,
      controller.providers[1].models[1].provider == "backup",
      controller.providers[1].models[1].apiKeyName == "backup-key",
      controller.modelProviderPopupButton.titleOfSelectedItem == "backup",
      controller.selectedRouteRow()?.providerIndex == 1,
      controller.selectedRouteRow()?.modelIndex == 1 else {
    fail("shared model provider picker did not move and reselect the deployment")
}
controller.window.orderOut(nil)

controller.viewMode = .providers
controller.applyEditorViewMode()
controller.window.contentView?.layoutSubtreeIfNeeded()
guard controller.modelTableView.tableColumns.map(\.title) == ["Model", "Upstream", "Balance", "API key / Order"],
      let balanceColumn = controller.modelTableView.tableColumns.first(where: { $0.identifier == controller.modelBillingColumnIdentifier }),
      balanceColumn.width == 112,
      balanceColumn.minWidth == 112,
      balanceColumn.maxWidth == 112 else {
    fail("models table does not use the stable upstream and compact balance columns")
}

let oversizedIntegerBalance = ProviderBillingAmount(
    kind: "balance",
    value: 9_223_372_036_854_775_808.0,
    unit: "USD"
)
let editorBalance = controller.billingAmountText(oversizedIntegerBalance)
guard editorBalance.contains("9223372036854775808.00 USD") else {
    fail("editor did not safely format an out-of-range integer balance: \(editorBalance)")
}

let unsupportedBilling = ProviderBillingModel(
    name: "test-chat",
    upstreamModel: "openai/test-chat",
    deploymentID: "deployment-a",
    status: "unsupported",
    detail: "No billing endpoint",
    source: nil,
    balance: nil,
    usage: nil,
    multiplier: ProviderBillingMultiplier(status: "unavailable", value: nil, detail: "N/A")
)
controller.providerBilling = ProviderBillingPayload(
    generatedAt: "2026-07-20T12:34:56Z",
    providers: [ProviderBillingProvider(name: "backup", status: "unsupported", accounts: [], models: [unsupportedBilling])],
    summary: ProviderBillingSummary(providers: 1, models: 1, availableModels: 0, unavailableModels: 1),
    status: nil,
    detail: nil
)
guard controller.modelBillingSummary(provider: controller.providers[1], model: controller.providers[1].models[1]) == "N/A" else {
    fail("unsupported billing was not condensed to N/A")
}

let validBalanceBilling = ProviderBillingModel(
    name: "test-chat",
    upstreamModel: "openai/test-chat",
    deploymentID: "deployment-a",
    status: "ok",
    detail: "",
    source: nil,
    balance: ProviderBillingAmount(kind: "balance", value: 4_940.04, unit: "USD"),
    usage: nil,
    multiplier: ProviderBillingMultiplier(status: "ok", value: 0.15, detail: "")
)
controller.providerBilling?.providers[0].models = [validBalanceBilling]
guard controller.modelBillingSummary(provider: controller.providers[1], model: controller.providers[1].models[1]) == "4.94K USD" else {
    fail("balance cell was not compacted to the fixed table width")
}
guard controller.modelBillingTooltip(provider: controller.providers[1], model: controller.providers[1].models[1])?.contains("Updated:") == true else {
    fail("billing tooltip does not expose its update timestamp")
}

controller.modelTableView.reloadData()
controller.modelTableView.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
let billingWidthsBeforeRefresh = controller.modelTableView.tableColumns.map(\.width)
let billingSelectionBeforeRefresh = controller.modelTableView.selectedRow
let billingScrollView = controller.modelTableView.enclosingScrollView
billingScrollView?.contentView.setBoundsOrigin(NSPoint(x: 0, y: 0))
let billingScrollBeforeRefresh = billingScrollView?.contentView.bounds.origin ?? .zero
controller.reloadModelBillingColumnPreservingViewport()
let billingScrollAfterRefresh = billingScrollView?.contentView.bounds.origin ?? .zero
guard controller.modelTableView.selectedRow == billingSelectionBeforeRefresh,
      controller.modelTableView.tableColumns.map(\.width) == billingWidthsBeforeRefresh,
      abs(billingScrollAfterRefresh.x - billingScrollBeforeRefresh.x) < 0.1,
      abs(billingScrollAfterRefresh.y - billingScrollBeforeRefresh.y) < 0.1 else {
    fail("billing-only refresh changed model selection, column geometry, or scroll position")
}
controller.providers[1].enabled = false
controller.providers[1].models[1].modelEnabled = false
guard controller.modelBillingSummary(provider: controller.providers[1], model: controller.providers[1].models[1]) == "4.94K USD" else {
    fail("disabled model lost its billing result")
}
let invalidBalanceBilling = ProviderBillingModel(
    name: "test-chat",
    upstreamModel: "openai/test-chat",
    deploymentID: "deployment-a",
    status: "ok",
    detail: "",
    source: nil,
    balance: ProviderBillingAmount(kind: "remaining_quota", value: -1, unit: "quota"),
    usage: nil,
    multiplier: ProviderBillingMultiplier(status: "unavailable", value: nil, detail: "N/A")
)
controller.providerBilling?.providers[0].models = [invalidBalanceBilling]
guard controller.modelBillingSummary(provider: controller.providers[1], model: controller.providers[1].models[1]) == "N/A" else {
    fail("an invalid balance was shown as a meaningful account value")
}

let expectedSuccessBytes = 2 * 1_024 * 1_024
do {
    let output = try controller.runHelper(arguments: ["load"], timeoutSeconds: 5)
    guard output.count == expectedSuccessBytes else {
        fail("large helper output had \(output.count) bytes instead of \(expectedSuccessBytes)")
    }
} catch {
    fail("large helper output deadlocked or failed: \(error.localizedDescription)")
}

do {
    _ = try controller.runHelper(arguments: ["save"], timeoutSeconds: 5)
    fail("failing helper unexpectedly succeeded")
} catch let error as ConfigEditorError {
    let message = error.message
    guard message.utf8.count <= 80 * 1_024 else {
        fail("helper failure diagnostics were not bounded: \(message.utf8.count) bytes")
    }
    guard message.contains("Earlier control output truncated") else {
        fail("helper failure diagnostics did not report truncation")
    }
} catch {
    fail("failing helper returned the wrong error type: \(error)")
}

let timeoutStarted = Date()
do {
    _ = try controller.runHelper(arguments: ["timeout"], timeoutSeconds: 1)
    fail("timed out helper unexpectedly succeeded")
} catch let error as ConfigEditorError {
    guard error.message.localizedCaseInsensitiveContains("timed out") else {
        fail("timed out helper did not report a timeout: \(error.message)")
    }
} catch {
    fail("timed out helper returned the wrong error type: \(error)")
}
guard Date().timeIntervalSince(timeoutStarted) < 5 else {
    fail("timed out helper did not terminate promptly")
}

print("model-editor-persistence-ok")
"""


HELPER = r"""
import sys
import time

command = sys.argv[-1]
if command == "load":
    sys.stdout.buffer.write(b"o" * (2 * 1024 * 1024))
    sys.stdout.flush()
elif command == "save":
    sys.stderr.buffer.write(b"e" * (256 * 1024))
    sys.stderr.flush()
    raise SystemExit(7)
elif command == "timeout":
    time.sleep(60)
else:
    raise SystemExit(64)
"""


@unittest.skipUnless(
    sys.platform == "darwin" and subprocess.run(["which", "swiftc"], capture_output=True).returncode == 0,
    "Model editor persistence harness requires macOS and swiftc.",
)
class ModelEditorPersistenceSwiftTests(unittest.TestCase):
    def test_helper_drains_large_output_bounds_errors_and_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime = temp / "runtime"
            runtime.mkdir()
            bundle = temp / "bundle"
            python_bin = bundle / "runtime" / "bin" / "python"
            python_bin.parent.mkdir(parents=True)
            python_bin.symlink_to(Path(sys.executable))
            editor = bundle / "config_editor.py"
            editor.write_text(textwrap.dedent(HELPER), encoding="utf-8")
            editor.chmod(editor.stat().st_mode | stat.S_IXUSR)

            harness = temp / "main.swift"
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            binary = temp / "model-editor-persistence-harness"
            compiled = subprocess.run(
                [
                    "swiftc",
                    *(str(path) for path in SOURCES),
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
                [str(binary), str(runtime), str(bundle)],
                cwd=ROOT,
                env=os.environ.copy(),
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "model-editor-persistence-ok")


if __name__ == "__main__":
    unittest.main()
