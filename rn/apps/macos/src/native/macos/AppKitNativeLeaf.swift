import AppKit
import Foundation
import ServiceManagement

@objc(AppKitNativeLeaf)
@objcMembers public final class AppKitNativeLeaf: NSObject, NSMenuDelegate, NSWindowDelegate {
    private struct RouteWindowLayout {
        let contentSize: NSSize
        let minSize: NSSize
        let maxSize: NSSize?
    }

    private struct MenuAction {
        let id: String
        let title: String
        let enabled: Bool
        let checked: Bool
    }

    private static let unconstrainedWindowSize = NSSize(
        width: CGFloat.greatestFiniteMagnitude,
        height: CGFloat.greatestFiniteMagnitude
    )

    // Keep the menu-bar shell anchored to the pre-RN AppKit app. The strings
    // are stable action IDs (plus the two presentation markers), not labels.
    private static let legacyStatusMenuOrder = [
        "status", "separator",
        "toggle-autostart", "separator",
        "open-providers-models", "open-runtime-settings", "open-codex-settings", "open-configuration-package", "separator",
        "webdav-status", "webdav-toggle", "open-webdav-settings", "separator",
        "open-recovery", "open-logs", "separator",
        "show-version", "quit",
    ]
    private static let legacyFooterMenuActionIDs: Set<String> = ["show-version", "quit"]
    private static let supplementalMenuActionIDs = [
        "open-claude-settings",
        "service-start", "service-stop", "service-restart", "service-reload", "service-health",
    ]
    private static let languageMenuActionIDs = [
        "set-language-system", "set-language-en", "set-language-zh-Hans",
    ]

    public static let shared = AppKitNativeLeaf()
    /// Injected by the host that owns ``CoreIPCServer``.  The callback turns
    /// a native URL into a Core file capability before the token reaches RN.
    var fileCapabilityRegistrar: ((URL, String) -> String?)?
    /// The RN bridge installs this to route native menu and deep-link actions
    /// without ever serializing a local path or Core credential into JS.
    var menuActionHandler: ((String) -> Void)? {
        didSet {
            guard let menuActionHandler else { return }
            pendingActions.forEach(menuActionHandler)
            pendingActions.removeAll()
        }
    }
    private let statusItem: NSStatusItem
    private weak var hostWindow: NSWindow?
    private weak var previousHostWindowDelegate: NSWindowDelegate?
    private var activeRoute: String?
    private var approvedCloseInProgress = false
    private var statusTitle = "LiteLLM Menu"
    private var menuActions: [MenuAction] = []
    private var pendingActions: [String] = []
    private var strings: [String: String] = [
        "appTitle": "LiteLLM Menu", "serviceUnavailable": "service unavailable",
        "cancel": "Cancel", "set": "Set", "clear": "Clear", "stage": "Stage", "find": "Find", "findNext": "Find Next",
        "edit": "Edit", "undo": "Undo", "redo": "Redo", "cut": "Cut", "copy": "Copy",
        "paste": "Paste", "selectAll": "Select All", "settings": "Settings…",
        "reload": "Reload", "closeWindow": "Close Window", "version": "Version",
        "build": "build", "ok": "OK", "invalidText": "The document contains invalid text.",
        "menuQuit": "Quit LiteLLM Menu",
        "routeHome": "LiteLLM Menu", "routeProvidersModels": "Providers & Models",
        "routeCodexSettings": "Codex Settings", "routeClaudeSettings": "Claude Settings",
        "routeRuntimeSettings": "Runtime Settings", "routeConfigurationPackage": "Configuration Package",
        "routeWebdavSettings": "WebDAV Settings", "routeLogs": "Logs",
    ]

    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: 32)
        super.init()
        statusItem.button?.title = "LL"
        statusItem.button?.image = nil
        statusItem.button?.imagePosition = .noImage
        statusItem.button?.setAccessibilityLabel("LiteLLM Menu")
        statusItem.menu = makeMenu()
    }

    public override func responds(to selector: Selector!) -> Bool {
        super.responds(to: selector) || previousHostWindowDelegate?.responds(to: selector) == true
    }

    public override func forwardingTarget(for selector: Selector!) -> Any? {
        if previousHostWindowDelegate?.responds(to: selector) == true {
            return previousHostWindowDelegate
        }
        return super.forwardingTarget(for: selector)
    }

    func setStatus(title: String, running: Bool) {
        statusTitle = title
        statusItem.length = 32
        statusItem.button?.title = "LL"
        statusItem.button?.toolTip = running
            ? localized("appTitle", fallback: "LiteLLM Menu")
            : "\(localized("appTitle", fallback: "LiteLLM Menu")) — \(localized("serviceUnavailable", fallback: "service unavailable"))"
        statusItem.menu?.item(withTag: 1)?.title = statusTitle
    }

    func setLocalization(_ values: [String: String]) {
        for (key, value) in values where !value.isEmpty { strings[key] = value }
        ensureSystemEditMenu(updateExisting: true)
        updateApplicationMenuTitles()
        statusItem.menu = makeMenu(actions: menuActions)
        if let activeRoute,
           let title = routeWindowTitle(activeRoute),
           let window = hostWindow {
            configure(window, for: activeRoute, title: title)
        }
    }

    func setMenuActions(_ actions: [[String: Any]]) {
        menuActions = actions.compactMap { action -> MenuAction? in
            guard let id = action["id"] as? String,
                  let title = action["title"] as? String,
                  !id.isEmpty,
                  !title.isEmpty
            else {
                return nil
            }
            return MenuAction(
                id: id,
                title: title,
                enabled: action["enabled"] as? Bool ?? true,
                checked: action["checked"] as? Bool ?? false
            )
        }
        statusItem.menu = makeMenu(actions: menuActions)
    }

    func open(route: String, title: String) {
        // The legacy app was menu-bar first. "home" exists only as a routing
        // target for RN, not as a dashboard window.
        guard route != "home" else {
            activeRoute = nil
            hideHostWindow()
            return
        }

        // React owns every settings route so state, validation, and actions
        // stay shared with Windows. Fabric component views below that surface
        // supply AppKit controls, focus behavior, and system appearance.
        activeRoute = route
        guard let window = hostWindow ?? reactHostWindow() else { return }
        hostWindow = window
        installWindowDelegate(on: window)
        configure(window, for: route, title: title)
        NSApp.setActivationPolicy(.regular)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func open(route: String) {
        guard let title = routeWindowTitle(route) else { return }
        open(route: route, title: title)
    }

    func close(route: String? = nil) {
        if let route {
            if activeRoute == route {
                activeRoute = nil
                approvedCloseInProgress = true
                defer { approvedCloseInProgress = false }
                hideHostWindow()
            }
        } else {
            activeRoute = nil
            approvedCloseInProgress = true
            defer { approvedCloseInProgress = false }
            hideHostWindow()
        }
    }

    func setWindowContentSize(width: Double, height: Double) -> Bool {
        let minimumContentExtent = 128.0
        let maximumContentExtent = 8_192.0
        guard width.isFinite,
              height.isFinite,
              width >= minimumContentExtent,
              height >= minimumContentExtent,
              width <= maximumContentExtent,
              height <= maximumContentExtent,
              let window = hostWindow ?? reactHostWindow()
        else {
            return false
        }

        hostWindow = window
        window.setContentSize(NSSize(width: width, height: height))
        return true
    }

    /// RCTAppDelegate creates the React host window during launch. Keep it
    /// alive for bridge initialization, but do not present it until a concrete
    /// legacy menu route asks for it.
    public func hideHostWindowAtLaunch(_ window: NSWindow?) {
        if let window {
            hostWindow = window
            installWindowDelegate(on: window)
        }
        hideHostWindow()
    }

    public func windowShouldClose(_ sender: NSWindow) -> Bool {
        if approvedCloseInProgress {
            return previousHostWindowDelegate?.windowShouldClose?(sender) ?? true
        }
        guard let activeRoute else {
            return previousHostWindowDelegate?.windowShouldClose?(sender) ?? true
        }
        requestClose(route: activeRoute)
        return false
    }

    func chooseImportFile(purpose: String = "import") -> String? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        return panel.runModal() == .OK ? registerSelection(panel.url, purpose: purpose) : nil
    }

    func chooseExportFile(suggestedName: String = "litellm-menu-configuration.json") -> String? {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedName
        return panel.runModal() == .OK ? registerSelection(panel.url, purpose: "export") : nil
    }

    func confirm(title: String, message: String, confirmTitle: String) -> Bool {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: confirmTitle)
        alert.addButton(withTitle: localized("cancel", fallback: "Cancel"))
        return alert.runModal() == .alertFirstButtonReturn
    }

    func chooseModelsToAdd(models: [String], providerName: String, keyName: String) -> [String]? {
        let candidates = models
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !candidates.isEmpty else { return [] }

        let contentWidth: CGFloat = 620
        let rowHeight: CGFloat = 28
        let listHeight = min(480, max(220, CGFloat(candidates.count) * rowHeight + 2))
        let controller = NativeModelChooserController(models: candidates, width: contentWidth - 36)
        let panel = makeModelChooserPanel(
            providerName: providerName,
            keyName: keyName,
            modelCount: candidates.count,
            contentWidth: contentWidth,
            listHeight: listHeight,
            controller: controller
        )
        defer { panel.close() }

        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        guard NSApp.runModal(for: panel) == .OK else { return nil }
        return controller.selectedModels
    }

    private func makeModelChooserPanel(
        providerName: String,
        keyName: String,
        modelCount: Int,
        contentWidth: CGFloat,
        listHeight: CGFloat,
        controller: NativeModelChooserController
    ) -> NSPanel {
        let contentHeight: CGFloat = 132 + listHeight + 52
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: contentWidth, height: contentHeight),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "Choose Models to Add"
        panel.minSize = NSSize(width: 520, height: 340)
        panel.isReleasedWhenClosed = false
        panel.delegate = controller
        controller.modalWindow = panel

        let content = NSView()
        panel.contentView = content
        let titleLabel = NSTextField(labelWithString: "Choose models to add")
        titleLabel.font = NSFont.systemFont(ofSize: 16, weight: .semibold)
        let subtitleLabel = NSTextField(labelWithString: "Provider: \(providerName)    Key: \(keyName)")
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.lineBreakMode = .byTruncatingMiddle
        let searchField = NSSearchField()
        searchField.placeholderString = "Search models"
        searchField.sendsSearchStringImmediately = true
        searchField.sendsWholeSearchString = false

        let selectionControls = NSStackView()
        selectionControls.orientation = .horizontal
        selectionControls.alignment = .centerY
        selectionControls.spacing = 8
        let selectAllButton = modelChooserButton(title: "All", toolTip: "Select all visible models")
        selectAllButton.target = controller
        selectAllButton.action = #selector(NativeModelChooserController.selectAllAction(_:))
        let invertButton = modelChooserButton(title: "Invert", toolTip: "Invert visible model selection")
        invertButton.target = controller
        invertButton.action = #selector(NativeModelChooserController.invertSelectionAction(_:))
        selectionControls.addArrangedSubview(selectAllButton)
        selectionControls.addArrangedSubview(invertButton)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        selectionControls.addArrangedSubview(spacer)
        let resultCountLabel = NSTextField(labelWithString: "")
        resultCountLabel.textColor = .secondaryLabelColor
        resultCountLabel.alignment = .right
        resultCountLabel.usesSingleLineMode = true
        resultCountLabel.setContentHuggingPriority(.required, for: .horizontal)
        resultCountLabel.setContentCompressionResistancePriority(.required, for: .horizontal)
        selectionControls.addArrangedSubview(resultCountLabel)

        let scroll = NativeModelChooserScrollView()
        scroll.wantsLayer = true
        scroll.borderType = .bezelBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = false
        scroll.hasHorizontalScroller = false
        scroll.usesPredominantAxisScrolling = true
        scroll.verticalScrollElasticity = .none
        scroll.documentView = controller.listView
        controller.listView.frame = NSRect(x: 0, y: 0, width: contentWidth - 36, height: max(listHeight, CGFloat(modelCount) * controller.listView.rowHeight))
        controller.listView.autoresizingMask = [.width]

        let cancelButton = NSButton(title: "Cancel", target: controller, action: #selector(NativeModelChooserController.cancelAction(_:)))
        cancelButton.bezelStyle = .rounded
        cancelButton.keyEquivalent = "\u{1b}"
        let addButton = modelChooserButton(title: "+", toolTip: "Add selected models")
        addButton.target = controller
        addButton.action = #selector(NativeModelChooserController.addSelectedAction(_:))
        addButton.keyEquivalent = "\r"

        controller.configureControls(
            searchField: searchField,
            scrollView: scroll,
            resultCountLabel: resultCountLabel,
            selectAllButton: selectAllButton,
            invertSelectionButton: invertButton,
            addButton: addButton,
            minimumListHeight: listHeight
        )

        for view in [titleLabel, subtitleLabel, searchField, selectionControls, scroll, cancelButton, addButton] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        NSLayoutConstraint.activate([
            titleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            titleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            titleLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),
            subtitleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            subtitleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            subtitleLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 4),
            searchField.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            searchField.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            searchField.topAnchor.constraint(equalTo: subtitleLabel.bottomAnchor, constant: 12),
            searchField.heightAnchor.constraint(equalToConstant: 28),
            selectionControls.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            selectionControls.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            selectionControls.topAnchor.constraint(equalTo: searchField.bottomAnchor, constant: 8),
            selectionControls.heightAnchor.constraint(equalToConstant: 28),
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            scroll.topAnchor.constraint(equalTo: selectionControls.bottomAnchor, constant: 8),
            scroll.bottomAnchor.constraint(equalTo: cancelButton.topAnchor, constant: -16),
            addButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            addButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
            cancelButton.trailingAnchor.constraint(equalTo: addButton.leadingAnchor, constant: -8),
            cancelButton.centerYAnchor.constraint(equalTo: addButton.centerYAnchor),
        ])
        panel.initialFirstResponder = searchField
        panel.center()
        return panel
    }

    private func modelChooserButton(title: String, toolTip: String) -> NSButton {
        let button = NSButton(title: title, target: nil, action: nil)
        button.bezelStyle = .rounded
        button.toolTip = toolTip
        button.setAccessibilityLabel(toolTip)
        return button
    }

    func localizedText(_ key: String, fallback: String) -> String {
        localized(key, fallback: fallback)
    }

    func editText(content: String, language: String, title: String) -> String? {
        let editor = NativeTextEditor(frame: NSRect(x: 0, y: 0, width: 620, height: 500))
        editor.textView.string = content
        editor.textView.setAccessibilityLabel(title)

        let split = NativeSplitView(frame: NSRect(x: 0, y: 0, width: 760, height: 500))
        let sidebar = NSView(frame: NSRect(x: 0, y: 0, width: 128, height: 500))
        let selector = NativeSegmentedControl(frame: NSRect(x: 8, y: 460, width: 112, height: 28))
        selector.segmentCount = 1
        selector.setLabel(language.uppercased(), forSegment: 0)
        selector.selectedSegment = 0
        selector.setAccessibilityLabel(language.uppercased())
        sidebar.addSubview(selector)
        split.addArrangedSubview(sidebar)
        split.addArrangedSubview(editor)
        split.setPosition(128, ofDividerAt: 0)

        let alert = NSAlert()
        alert.messageText = title
        alert.accessoryView = split
        alert.addButton(withTitle: localized("stage", fallback: "Stage"))
        alert.addButton(withTitle: localized("cancel", fallback: "Cancel"))
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        return editor.textView.string
    }

    public func setShortcuts(_ shortcuts: [String: String]) {
        ensureSystemEditMenu()
        guard let mainMenu = NSApp.mainMenu else { return }
        if mainMenu.items.contains(where: {
            $0.representedObject as? String == "open-settings" ||
            $0.submenu?.items.contains(where: { $0.representedObject as? String == "open-settings" }) == true
        }) { return }
        let applicationMenu: NSMenu
        if let existing = mainMenu.items.first?.submenu {
            applicationMenu = existing
        } else {
            applicationMenu = NSMenu(title: "LiteLLM Menu")
            let appRoot = NSMenuItem(title: "LiteLLM Menu", action: nil, keyEquivalent: "")
            appRoot.submenu = applicationMenu
            mainMenu.insertItem(appRoot, at: 0)
        }
        if shortcuts["openMenu"]?.lowercased().contains("cmd+,") == true {
            let item = applicationMenu.addItem(withTitle: localized("settings", fallback: "Settings…"), action: #selector(openRuntime), keyEquivalent: ",")
            item.keyEquivalentModifierMask = [.command]
            item.target = self
            item.representedObject = "open-settings"
        }
        if shortcuts["reload"]?.lowercased().contains("cmd+r") == true {
            let item = applicationMenu.addItem(withTitle: localized("reload", fallback: "Reload"), action: #selector(reloadFromShortcut), keyEquivalent: "r")
            item.keyEquivalentModifierMask = [.command]
            item.target = self
            item.representedObject = "native-reload"
        }
        if shortcuts["closeWindow"]?.lowercased().contains("esc") == true {
            let item = applicationMenu.addItem(withTitle: localized("closeWindow", fallback: "Close Window"), action: #selector(closeFromShortcut), keyEquivalent: "\u{1b}")
            item.target = self
            item.representedObject = "native-close-window"
        }
        if !applicationMenu.items.contains(where: { $0.action == #selector(quit) || $0.action == #selector(NSApplication.terminate(_:)) }) {
            applicationMenu.addItem(.separator())
            let quitItem = applicationMenu.addItem(withTitle: localized("menuQuit", fallback: "Quit LiteLLM Menu"), action: #selector(quit), keyEquivalent: "q")
            quitItem.keyEquivalentModifierMask = [.command]
            quitItem.target = self
            quitItem.representedObject = "native-quit"
        }
    }

    public func setLaunchAtLogin(_ enabled: Bool) -> Bool {
        guard #available(macOS 13.0, *) else { return false }
        do {
            let status = SMAppService.mainApp.status
            if enabled {
                if status == .enabled { return true }
                try SMAppService.mainApp.register()
            } else if status != .notRegistered && status != .notFound {
                try SMAppService.mainApp.unregister()
            }
            return true
        } catch {
            return false
        }
    }

    func systemLocale() -> String {
        Locale.preferredLanguages.first ?? Locale.current.identifier
    }

    func showVersion() {
        let info = Bundle.main.infoDictionary ?? [:]
        let version = info["CFBundleShortVersionString"] as? String ?? "?"
        let build = info["CFBundleVersion"] as? String ?? "?"
        let alert = NSAlert()
        alert.messageText = localized("appTitle", fallback: "LiteLLM Menu")
        alert.informativeText = "\(localized("version", fallback: "Version")) \(version) (\(localized("build", fallback: "build")) \(build))"
        alert.addButton(withTitle: localized("ok", fallback: "OK"))
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    private func registerSelection(_ url: URL?, purpose: String) -> String? {
        guard let url else { return nil }
        return fileCapabilityRegistrar?(url, purpose)
    }

    private func makeMenu(actions: [MenuAction] = []) -> NSMenu {
        let menu = NSMenu()
        menu.delegate = self
        var actionMap: [String: MenuAction] = [:]
        for action in actions {
            actionMap[action.id] = action
        }
        var consumed = Set<String>()

        for marker in Self.legacyStatusMenuOrder where !Self.legacyFooterMenuActionIDs.contains(marker) {
            switch marker {
            case "status":
                let status = menu.addItem(withTitle: statusTitle, action: nil, keyEquivalent: "")
                status.tag = 1
                status.isEnabled = false
            case "separator":
                if menu.items.last?.isSeparatorItem == false {
                    menu.addItem(.separator())
                }
            case "webdav-status":
                let webDAVStatus = menu.addItem(
                    withTitle: webDAVStatusTitle(from: actionMap),
                    action: nil,
                    keyEquivalent: ""
                )
                webDAVStatus.isEnabled = false
            case let id where Self.legacyFooterMenuActionIDs.contains(id):
                addLegacyMenuItem(id, from: actionMap, to: menu, consumed: &consumed)
            case let id:
                addLegacyMenuItem(id, from: actionMap, to: menu, consumed: &consumed)
            }
        }

        // These routes and lifecycle controls do not exist in the legacy UI.
        // Keep them in one conservative tail group instead of changing the
        // established AppKit ordering above.
        let supplemental = Self.supplementalMenuActionIDs.filter { actionMap[$0] != nil }
        if !supplemental.isEmpty {
            if menu.items.last?.isSeparatorItem == false { menu.addItem(.separator()) }
            addMenuItems(supplemental, from: actionMap, to: menu, consumed: &consumed)
        }

        if let languageRoot = actionMap["language-menu"] {
            let languageActions = Self.languageMenuActionIDs.compactMap { actionMap[$0] }
            if !languageActions.isEmpty {
                if menu.items.last?.isSeparatorItem == false { menu.addItem(.separator()) }
                addLanguageMenu(
                    languageRoot,
                    choices: languageActions,
                    to: menu,
                    consumed: &consumed
                )
            }
        }

        for action in actions where !consumed.contains(action.id) && !Self.legacyFooterMenuActionIDs.contains(action.id) {
            addMenuItem(action.id, title: action.title, enabled: action.enabled, to: menu)
            consumed.insert(action.id)
        }

        if menu.items.last?.isSeparatorItem == false { menu.addItem(.separator()) }
        for marker in Self.legacyStatusMenuOrder where Self.legacyFooterMenuActionIDs.contains(marker) {
            addLegacyMenuItem(marker, from: actionMap, to: menu, consumed: &consumed)
        }
        return menu
    }

    private func addLegacyMenuItem(
        _ id: String,
        from actions: [String: MenuAction],
        to menu: NSMenu,
        consumed: inout Set<String>
    ) {
        if let action = actions[id] {
            addMenuItem(id, title: legacyMenuTitle(for: id, fallback: action.title), enabled: action.enabled, to: menu)
            consumed.insert(id)
            return
        }

        guard let fallback = legacyMenuFallback(for: id) else { return }
        addMenuItem(id, title: fallback, to: menu, keyEquivalent: legacyKeyEquivalent(for: id))
    }

    private func legacyMenuTitle(for id: String, fallback: String) -> String {
        switch id {
        case "open-providers-models": return localized("routeProvidersModels", fallback: fallback) + "…"
        case "open-runtime-settings": return localized("routeRuntimeSettings", fallback: fallback) + "…"
        case "open-codex-settings": return localized("routeCodexSettings", fallback: fallback) + "…"
        case "open-configuration-package": return "Import / Export Config…"
        case "open-webdav-settings": return "WebDAV Sync Settings…"
        case "open-logs": return fallback
        case "quit": return localized("menuQuit", fallback: fallback)
        default: return fallback
        }
    }

    private func legacyMenuFallback(for id: String) -> String? {
        switch id {
        case "toggle-autostart": return "Auto Start at Login"
        case "open-providers-models": return localized("routeProvidersModels", fallback: "Providers & Models") + "…"
        case "open-runtime-settings": return localized("routeRuntimeSettings", fallback: "Runtime Settings") + "…"
        case "open-codex-settings": return localized("routeCodexSettings", fallback: "Codex Settings") + "…"
        case "open-configuration-package": return "Import / Export Config…"
        case "webdav-toggle": return "Enable WebDAV Sync"
        case "open-webdav-settings": return "WebDAV Sync Settings…"
        case "open-recovery": return "Recovery"
        case "open-logs": return "View Logs"
        case "show-version": return localized("version", fallback: "Version")
        case "quit": return localized("menuQuit", fallback: "Quit LiteLLM Menu")
        default: return nil
        }
    }

    private func legacyKeyEquivalent(for id: String) -> String {
        switch id {
        case "quit": return "q"
        default: return ""
        }
    }

    private func webDAVStatusTitle(from actions: [String: MenuAction]) -> String {
        guard let action = actions["webdav-toggle"] else { return "WebDAV: Checking…" }
        return action.title.localizedCaseInsensitiveContains("disable")
            ? "WebDAV: Enabled"
            : "WebDAV: Disabled"
    }

    private func addMenuItems(
        _ ids: [String],
        from actions: [String: MenuAction],
        to menu: NSMenu,
        consumed: inout Set<String>
    ) {
        for id in ids {
            guard let action = actions[id] else { continue }
            addMenuItem(id, title: action.title, enabled: action.enabled, to: menu)
            consumed.insert(id)
        }
    }

    private func addLanguageMenu(
        _ root: MenuAction,
        choices: [MenuAction],
        to menu: NSMenu,
        consumed: inout Set<String>
    ) {
        let rootItem = NSMenuItem(title: root.title, action: nil, keyEquivalent: "")
        rootItem.isEnabled = root.enabled
        let submenu = NSMenu(title: root.title)
        for choice in choices {
            let item = NSMenuItem(title: choice.title, action: #selector(menuAction(_:)), keyEquivalent: "")
            item.representedObject = choice.id
            item.isEnabled = choice.enabled
            item.state = choice.checked ? .on : .off
            item.target = self
            submenu.addItem(item)
            consumed.insert(choice.id)
        }
        rootItem.submenu = submenu
        menu.addItem(rootItem)
        consumed.insert(root.id)
    }

    private func addSeparatorIfNeeded(to menu: NSMenu, after ids: [String], actions: [String: MenuAction]) {
        guard ids.contains(where: { actions[$0] != nil }), menu.items.last?.isSeparatorItem == false else { return }
        menu.addItem(.separator())
    }

    private func addMenuItem(
        _ id: String,
        title: String,
        enabled: Bool = true,
        to menu: NSMenu,
        keyEquivalent: String = ""
    ) {
        let item = NSMenuItem(title: title, action: #selector(menuAction(_:)), keyEquivalent: keyEquivalent)
        item.keyEquivalentModifierMask = keyEquivalent.isEmpty ? [] : [.command]
        item.representedObject = id
        item.isEnabled = enabled
        item.target = self
        menu.addItem(item)
    }

    private func ensureSystemEditMenu(updateExisting: Bool = false) {
        let mainMenu = NSApp.mainMenu ?? NSMenu(title: "Main")
        if NSApp.mainMenu == nil { NSApp.mainMenu = mainMenu }
        if let existing = mainMenu.items.first(where: { $0.representedObject as? String == "native-edit-menu" }) {
            if !updateExisting { return }
            mainMenu.removeItem(existing)
        }

        let editRoot = NSMenuItem(title: localized("edit", fallback: "Edit"), action: nil, keyEquivalent: "")
        editRoot.representedObject = "native-edit-menu"
        let editMenu = NSMenu(title: localized("edit", fallback: "Edit"))
        editMenu.addItem(withTitle: localized("undo", fallback: "Undo"), action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: localized("redo", fallback: "Redo"), action: Selector(("redo:")), keyEquivalent: "Z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: localized("cut", fallback: "Cut"), action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: localized("copy", fallback: "Copy"), action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: localized("paste", fallback: "Paste"), action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: localized("selectAll", fallback: "Select All"), action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenu.addItem(.separator())
        let find = editMenu.addItem(
            withTitle: localized("find", fallback: "Find"),
            action: #selector(NSTextView.performFindPanelAction(_:)),
            keyEquivalent: "f"
        )
        find.tag = NSTextFinder.Action.showFindInterface.rawValue
        let findNext = editMenu.addItem(
            withTitle: localized("findNext", fallback: "Find Next"),
            action: #selector(NSTextView.performFindPanelAction(_:)),
            keyEquivalent: "g"
        )
        findNext.tag = NSTextFinder.Action.nextMatch.rawValue
        editRoot.submenu = editMenu
        mainMenu.addItem(editRoot)
    }

    private func updateApplicationMenuTitles() {
        guard let applicationMenu = NSApp.mainMenu?.items.first?.submenu else { return }
        for item in applicationMenu.items {
            switch item.representedObject as? String {
            case "open-settings": item.title = localized("settings", fallback: "Settings…")
            case "native-reload": item.title = localized("reload", fallback: "Reload")
            case "native-close-window": item.title = localized("closeWindow", fallback: "Close Window")
            case "native-quit": item.title = localized("menuQuit", fallback: "Quit LiteLLM Menu")
            default: break
            }
        }
    }

    private func openNamedRoute(_ route: String) {
        guard let title = routeWindowTitle(route) else { return }
        open(route: route, title: title)
        emitAction("open-\(route)")
    }

    public func openRouteFromDeepLink(_ route: String, logTab: String?) {
        guard let title = routeWindowTitle(route) else { return }
        guard logTab == nil || (route == "logs" && isAllowedLogTab(logTab!)) else { return }
        open(route: route, title: title)
        if let logTab {
            emitAction("open-logs?tab=\(logTab)")
        } else {
            emitAction("open-\(route)")
        }
    }

    private func reactHostWindow() -> NSWindow? {
        NSApp.windows.first { candidate in
            candidate.level == .normal && candidate.contentViewController != nil
        }
    }

    private func installWindowDelegate(on window: NSWindow) {
        guard window.delegate !== self else { return }
        previousHostWindowDelegate = window.delegate
        window.delegate = self
    }

    private func requestClose(route: String?) {
        guard let route, route == activeRoute else { return }
        emitAction("request-close-\(route)")
    }

    private func configure(_ window: NSWindow, for route: String, title: String) {
        let layout = routeWindowLayout(for: route)
        window.title = title
        window.minSize = layout.minSize
        window.maxSize = layout.maxSize ?? Self.unconstrainedWindowSize
        window.setContentSize(layout.contentSize)
        window.collectionBehavior = [.fullScreenPrimary]
        window.level = .normal
    }

    private func hideHostWindow() {
        guard let window = hostWindow ?? reactHostWindow() else { return }
        hostWindow = window
        window.orderOut(nil)
        NSApp.setActivationPolicy(.accessory)
    }

    private func routeWindowLayout(for route: String) -> RouteWindowLayout {
        switch route {
        case "providers-models":
            return RouteWindowLayout(
                contentSize: NSSize(width: 1052, height: 600),
                minSize: NSSize(width: 1052, height: 560),
                maxSize: nil
            )
        case "codex-settings", "claude-settings":
            return RouteWindowLayout(
                contentSize: NSSize(width: 1120, height: 680),
                minSize: NSSize(width: 1020, height: 620),
                maxSize: nil
            )
        case "runtime-settings":
            return RouteWindowLayout(
                contentSize: NSSize(width: 1080, height: 620),
                minSize: NSSize(width: 760, height: 500),
                maxSize: NSSize(width: 1160, height: CGFloat.greatestFiniteMagnitude)
            )
        case "configuration-package":
            return RouteWindowLayout(
                contentSize: NSSize(width: 420, height: 208),
                minSize: NSSize(width: 420, height: 132),
                maxSize: NSSize(width: 420, height: 208)
            )
        case "webdav-settings":
            return RouteWindowLayout(
                contentSize: NSSize(width: 680, height: 386),
                minSize: NSSize(width: 680, height: 386),
                maxSize: NSSize(width: 680, height: 386)
            )
        case "logs":
            return RouteWindowLayout(
                contentSize: NSSize(width: 900, height: 580),
                minSize: NSSize(width: 640, height: 420),
                maxSize: nil
            )
        default:
            return RouteWindowLayout(
                contentSize: NSSize(width: 1052, height: 600),
                minSize: NSSize(width: 760, height: 500),
                maxSize: nil
            )
        }
    }

    private func emitAction(_ action: String) {
        if let menuActionHandler {
            menuActionHandler(action)
        } else {
            pendingActions.append(action)
        }
    }

    private func routeTitle(_ route: String) -> String? {
        switch route {
        case "home": return localized("routeHome", fallback: "LiteLLM Menu")
        case "providers-models": return localized("routeProvidersModels", fallback: "Providers & Models")
        case "codex-settings": return localized("routeCodexSettings", fallback: "Codex Settings")
        case "claude-settings": return localized("routeClaudeSettings", fallback: "Claude Settings")
        case "runtime-settings": return localized("routeRuntimeSettings", fallback: "Runtime Settings")
        case "configuration-package": return localized("routeConfigurationPackage", fallback: "Configuration Package")
        case "webdav-settings": return localized("routeWebdavSettings", fallback: "WebDAV Settings")
        case "logs": return localized("routeLogs", fallback: "Logs")
        default: return nil
        }
    }

    /// Keep the native window title in sync with the shared route localization.
    /// AppKit owns the title bar, but React owns the language preference.
    private func routeWindowTitle(_ route: String) -> String? {
        switch route {
        case "home": return localized("routeHome", fallback: "LiteLLM Menu")
        case "providers-models": return "LiteLLM " + localized("routeProvidersModels", fallback: "Providers & Models")
        case "codex-settings": return localized("routeCodexSettings", fallback: "Codex Settings")
        case "claude-settings": return localized("routeClaudeSettings", fallback: "Claude Settings")
        case "runtime-settings": return localized("routeRuntimeSettings", fallback: "Runtime Settings")
        case "configuration-package": return localized("routeConfigurationPackage", fallback: "Configuration Package")
        case "webdav-settings": return localized("routeWebdavSettings", fallback: "WebDAV Settings")
        case "logs": return "LiteLLM " + localized("routeLogs", fallback: "Logs")
        default: return nil
        }
    }

    private func isAllowedLogTab(_ tab: String) -> Bool {
        ["requests", "service", "menu", "route-trace", "recovery", "online-usage"].contains(tab)
    }

    private func localized(_ key: String, fallback: String) -> String {
        strings[key].flatMap { $0.isEmpty ? nil : $0 } ?? fallback
    }

    @objc private func openProviders() { openNamedRoute("providers-models") }
    @objc private func openCodex() { openNamedRoute("codex-settings") }
    @objc private func openClaude() { openNamedRoute("claude-settings") }
    @objc private func openRuntime() { openNamedRoute("runtime-settings") }
    @objc private func openPackage() { openNamedRoute("configuration-package") }
    @objc private func openWebDAV() { openNamedRoute("webdav-settings") }
    @objc private func openLogs() { openNamedRoute("logs") }
    @objc private func reloadFromShortcut() { emitAction("service-reload") }
    @objc private func closeFromShortcut() { requestClose(route: activeRoute) }
    @objc private func menuAction(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        switch id {
        case "open-providers-models": openProviders()
        case "open-codex-settings": openCodex()
        case "open-claude-settings": openClaude()
        case "open-runtime-settings": openRuntime()
        case "open-configuration-package": openPackage()
        case "open-webdav-settings": openWebDAV()
        case "open-logs": openLogs()
        case "toggle-autostart":
            emitAction(id)
        case "show-version": showVersion()
        case "quit": quit()
        default: emitAction(id)
        }
    }
    func requestQuit() {
        DispatchQueue.global(qos: .userInitiated).async {
            CoreIPCBridge.shared.stop()
            DispatchQueue.main.async {
                NSApp.terminate(nil)
            }
        }
    }

    @objc private func quit() { requestQuit() }
}

final class NativeSplitView: NSSplitView {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        isVertical = true
        dividerStyle = .thin
        autosaveName = "LiteLLMMenu.SettingsSplitView"
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
    }
}

final class NativeTextEditor: NSScrollView {
    let textView: NSTextView

    override init(frame frameRect: NSRect) {
        textView = NSTextView(frame: .zero)
        super.init(frame: frameRect)
        hasVerticalScroller = true
        hasHorizontalScroller = true
        autohidesScrollers = false
        borderType = .bezelBorder
        textView.isRichText = false
        textView.allowsUndo = true
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = true
        textView.autoresizingMask = [.width, .height]
        textView.frame = bounds
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.usesFindPanel = true
        textView.textContainer?.widthTracksTextView = false
        textView.textContainer?.containerSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        documentView = textView
    }

    required init?(coder: NSCoder) {
        textView = NSTextView(frame: .zero)
        super.init(coder: coder)
    }
}

final class NativeSegmentedControl: NSSegmentedControl {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        segmentStyle = .texturedRounded
        trackingMode = .selectOne
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
    }
}

private final class NativeModelChooserScrollView: NSScrollView {
    override func scrollWheel(with event: NSEvent) {
        guard event.hasPreciseScrollingDeltas, let documentView else {
            super.scrollWheel(with: event)
            return
        }
        var origin = contentView.bounds.origin
        let maxY = max(0, documentView.frame.height - contentView.bounds.height)
        origin.y = min(maxY, max(0, origin.y - event.scrollingDeltaY * 4))
        origin.x = 0
        contentView.scroll(to: origin)
        reflectScrolledClipView(contentView)
    }
}

private final class NativeModelChooserListView: NSView {
    private struct Row { let title: String; var selected: Bool }
    private var rows: [Row]
    private var visibleRowIndexes: [Int]
    private var searchQuery = ""
    private var minimumDocumentHeight: CGFloat = 0
    var stateDidChange: (() -> Void)?
    let rowHeight: CGFloat = 28

    override class var isCompatibleWithResponsiveScrolling: Bool { true }
    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }

    init(models: [String], width: CGFloat) {
        rows = models.map { Row(title: $0, selected: false) }
        visibleRowIndexes = Array(models.indices)
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: max(rowHeight, CGFloat(models.count) * rowHeight)))
        rebuildRows()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func setMinimumDocumentHeight(_ height: CGFloat) { minimumDocumentHeight = max(0, height); updateDocumentHeight() }
    func setSearchQuery(_ query: String) {
        searchQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let terms = searchQuery.split(whereSeparator: { $0.isWhitespace }).map { String($0).folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: .current) }
        visibleRowIndexes = terms.isEmpty ? Array(rows.indices) : rows.indices.filter { index in
            let title = rows[index].title.folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: .current)
            return terms.allSatisfy(title.contains)
        }
        updateDocumentHeight()
        rebuildRows()
        stateDidChange?()
    }
    func selectAll() { for index in visibleRowIndexes { rows[index].selected = true }; rebuildRows(); stateDidChange?() }
    func invertSelection() { for index in visibleRowIndexes { rows[index].selected.toggle() }; rebuildRows(); stateDidChange?() }
    private func updateDocumentHeight() { setFrameSize(NSSize(width: frame.width, height: max(minimumDocumentHeight, CGFloat(max(1, visibleRowIndexes.count)) * rowHeight))) }
    private func rebuildRows() {
        subviews.forEach { $0.removeFromSuperview() }
        if visibleRowIndexes.isEmpty {
            let label = NSTextField(labelWithString: searchQuery.isEmpty ? "No models available" : "No matching models")
            label.textColor = .secondaryLabelColor
            label.alignment = .center
            label.frame = NSRect(x: 16, y: max(20, floor((bounds.height - 20) / 2)), width: max(0, bounds.width - 32), height: 20)
            label.autoresizingMask = [.width]
            addSubview(label)
            return
        }
        for (visibleIndex, rowIndex) in visibleRowIndexes.enumerated() {
            let checkbox = NSButton(checkboxWithTitle: rows[rowIndex].title, target: self, action: #selector(toggleRow(_:)))
            checkbox.state = rows[rowIndex].selected ? .on : .off
            checkbox.tag = rowIndex
            checkbox.font = NSFont.systemFont(ofSize: 14)
            checkbox.lineBreakMode = .byTruncatingMiddle
            checkbox.toolTip = rows[rowIndex].title
            checkbox.setAccessibilityLabel(rows[rowIndex].title)
            checkbox.frame = NSRect(x: 10, y: CGFloat(visibleIndex) * rowHeight + 2, width: max(0, bounds.width - 20), height: rowHeight - 4)
            checkbox.autoresizingMask = [.width]
            addSubview(checkbox)
        }
    }
    @objc private func toggleRow(_ sender: NSButton) {
        guard sender.tag >= 0, sender.tag < rows.count else { return }
        rows[sender.tag].selected = sender.state == .on
        stateDidChange?()
    }
    var selectedModels: [String] { rows.filter(\.selected).map(\.title) }
    var totalCount: Int { rows.count }
    var visibleCount: Int { visibleRowIndexes.count }
    var selectedCount: Int { rows.filter(\.selected).count }
    var hasActiveSearch: Bool { !searchQuery.isEmpty }
}

private final class NativeModelChooserController: NSObject, NSWindowDelegate, NSSearchFieldDelegate {
    private var didStopModal = false
    weak var modalWindow: NSWindow?
    weak var searchField: NSSearchField?
    weak var scrollView: NSScrollView?
    weak var resultCountLabel: NSTextField?
    weak var selectAllButton: NSButton?
    weak var invertSelectionButton: NSButton?
    weak var addButton: NSButton?
    let listView: NativeModelChooserListView

    init(models: [String], width: CGFloat) {
        listView = NativeModelChooserListView(models: models, width: width)
        super.init()
        listView.stateDidChange = { [weak self] in self?.refreshControls() }
    }

    func configureControls(searchField: NSSearchField, scrollView: NSScrollView, resultCountLabel: NSTextField, selectAllButton: NSButton, invertSelectionButton: NSButton, addButton: NSButton, minimumListHeight: CGFloat) {
        self.searchField = searchField
        self.scrollView = scrollView
        self.resultCountLabel = resultCountLabel
        self.selectAllButton = selectAllButton
        self.invertSelectionButton = invertSelectionButton
        self.addButton = addButton
        searchField.delegate = self
        listView.setMinimumDocumentHeight(minimumListHeight)
        refreshControls()
    }
    func controlTextDidChange(_ obj: Notification) {
        guard let field = obj.object as? NSSearchField, field === searchField else { return }
        listView.setSearchQuery(field.stringValue)
        scrollView?.contentView.scroll(to: .zero)
    }
    private func refreshControls() {
        var summary = listView.hasActiveSearch ? "\(listView.visibleCount) of \(listView.totalCount) models" : "\(listView.totalCount) models"
        if listView.selectedCount > 0 { summary += "  |  \(listView.selectedCount) selected" }
        resultCountLabel?.stringValue = summary
        selectAllButton?.isEnabled = listView.visibleCount > 0
        invertSelectionButton?.isEnabled = listView.visibleCount > 0
        addButton?.isEnabled = listView.selectedCount > 0
    }
    @objc func selectAllAction(_ sender: Any?) { listView.selectAll() }
    @objc func invertSelectionAction(_ sender: Any?) { listView.invertSelection() }
    @objc func addSelectedAction(_ sender: Any?) { stopModal(with: .OK) }
    @objc func cancelAction(_ sender: Any?) { stopModal(with: .cancel) }
    func windowWillClose(_ notification: Notification) { stopModal(with: .cancel) }
    private func stopModal(with response: NSApplication.ModalResponse) {
        guard !didStopModal else { return }
        didStopModal = true
        NSApp.stopModal(withCode: response)
        modalWindow?.orderOut(nil)
    }
    var selectedModels: [String] { listView.selectedModels }
}
