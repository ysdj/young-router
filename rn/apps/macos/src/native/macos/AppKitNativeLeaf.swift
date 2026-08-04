import AppKit
import Foundation
import Security
import ServiceManagement
import WebKit

private enum NativeRelayOriginPolicy {
    static func allows(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), ["http", "https"].contains(scheme), let host = url.host?.lowercased() else {
            return false
        }
        if scheme == "https" { return true }
        let normalizedHost = host.trimmingCharacters(in: CharacterSet(charactersIn: "."))
        if normalizedHost == "localhost" || normalizedHost.hasSuffix(".localhost") { return true }
        return normalizedHost == "127.0.0.1" || normalizedHost == "::1" || normalizedHost == "0:0:0:0:0:0:0:1"
    }
}

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

        func matches(_ other: MenuAction) -> Bool {
            id == other.id && title == other.title && enabled == other.enabled && checked == other.checked
        }
    }

    private static let unconstrainedWindowSize = NSSize(
        width: CGFloat.greatestFiniteMagnitude,
        height: CGFloat.greatestFiniteMagnitude
    )
    private static let applicationIcon = NSImage(
        contentsOf: Bundle.main.url(forResource: "AppIcon", withExtension: "icns")!
    )!
    private static let statusBarIcon: NSImage = {
        let image = NSImage(size: NSSize(width: 22, height: 18))
        image.lockFocus()

        let scale: CGFloat = 1
        let transform = NSAffineTransform()
        transform.translateX(by: 22 * (1 - scale) / 2, yBy: 18 * (1 - scale) / 2)
        transform.scale(by: scale)
        transform.concat()

        let attributes: [NSAttributedString.Key: Any] = [
            .foregroundColor: NSColor.black,
        ]
        ("L" as NSString).draw(
            at: NSPoint(x: 2.5, y: -1),
            withAttributes: attributes.merging([.font: NSFont.systemFont(ofSize: 18, weight: .regular)]) { _, new in new }
        )
        ("L" as NSString).draw(
            at: NSPoint(x: 13, y: 2),
            withAttributes: attributes.merging([.font: NSFont.systemFont(ofSize: 13, weight: .regular)]) { _, new in new }
        )

        image.unlockFocus()
        image.isTemplate = true
        return image
    }()

    // Keep the menu-bar shell anchored to the pre-RN AppKit app. The strings
    // are stable action IDs (plus the two presentation markers), not labels.
    private static let statusMenuOrder = [
        "status", "separator",
        "toggle-autostart", "separator",
        "open-providers-models", "open-runtime-settings", "open-codex-settings", "open-relay-accounts", "separator",
        "webdav-status", "open-webdav-settings", "separator",
        "open-logs", "separator",
        "show-version", "quit",
    ]
    private static let footerMenuActionIDs: Set<String> = ["show-version", "quit"]
    // Recovery now has a dedicated tab inside Logs. Keep old shared clients
    // from adding a second menu item while they update their action list.
    private static let suppressedStatusMenuActionIDs: Set<String> = [
        "open-claude-settings", "open-recovery",
        "service-start", "service-stop", "service-restart", "service-reload", "service-health",
    ]
    private static let applicationMenuActionIDs = [
        "language-menu", "set-language-system", "set-language-en", "set-language-zh-Hans",
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
    private var routeWindowFactory: ((String, String?, NSWindow?) -> NSWindow?)?
    private var reactHostStarter: (() -> Void)?
    private var routeWindows: [String: NSWindow] = [:]
    private var approvedCloseRoutes: Set<String> = []
    // Retain the browser flow across the asynchronous React Native promise.
    private var activeRelayLoginController: NativeRelayLoginController?
    private var statusTitle = "LiteLLM Menu"
    private var statusRunning = false
    private var menuActions: [MenuAction] = []
    private var menuTracking = false
    private var menuNeedsRefresh = false
    private var pendingActions: [String] = []
    private var strings: [String: String] = [
        "appTitle": "LiteLLM Menu", "autoStart": "Auto Start at Login", "serviceUnavailable": "service unavailable",
        "cancel": "Cancel", "set": "Set", "clear": "Clear", "stage": "Stage", "find": "Find", "findNext": "Find Next",
        "edit": "Edit", "undo": "Undo", "redo": "Redo", "cut": "Cut", "copy": "Copy",
        "paste": "Paste", "selectAll": "Select All", "settings": "Settings…",
        "reload": "Reload", "closeWindow": "Close Window", "version": "Version",
        "build": "build", "ok": "OK", "invalidText": "The document contains invalid text.",
        "languageMenu": "Language", "languageSystem": "System", "languageEnglish": "English", "languageSimplifiedChinese": "简体中文",
        "menuQuit": "Quit LiteLLM Menu",
        "routeHome": "LiteLLM Menu", "routeProvidersModels": "Providers & Models",
        "routeRelayAccounts": "Relay Accounts",
        "routeCodexSettings": "Codex / Claude Settings", "routeClaudeSettings": "Claude Settings",
        "routeRuntimeSettings": "Runtime Settings",
        "routeWebdavSettings": "WebDAV Sync Settings", "routeLogs": "Logs",
        "modelChooserTitle": "Choose Models to Add", "modelChooserHeading": "Choose models to add",
        "modelChooserProvider": "Provider", "modelChooserKey": "Key", "modelChooserSearch": "Search models",
        "modelChooserAll": "All", "modelChooserSelectAllVisible": "Select all visible models",
        "modelChooserInvert": "Invert", "modelChooserInvertVisible": "Invert visible model selection",
        "modelChooserAddSelected": "Add Selected", "modelChooserCount": "{count} models",
        "modelChooserCountFiltered": "{visible} of {total} models", "modelChooserCountSelected": "{count} selected",
        "modelChooserEmpty": "No models available", "modelChooserNoMatches": "No matching models",
    ]
    override init() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        super.init()
        statusItem.button?.title = ""
        statusItem.button?.image = Self.statusBarIcon
        statusItem.button?.imagePosition = .imageOnly
        statusItem.button?.setAccessibilityLabel(Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "LiteLLM Menu")
        statusItem.menu = makeMenu()
    }

    public func setRouteWindowFactory(_ factory: @escaping (String, String?, NSWindow?) -> NSWindow?) {
        routeWindowFactory = factory
    }

    public func setReactHostStarter(_ starter: @escaping () -> Void) {
        reactHostStarter = starter
    }

    func setStatus(title: String, running: Bool) {
        guard title != statusTitle || running != statusRunning else { return }
        statusTitle = title
        statusRunning = running
        statusItem.length = NSStatusItem.squareLength
        statusItem.button?.title = ""
        statusItem.button?.image = Self.statusBarIcon
        statusItem.button?.toolTip = running
            ? localized("appTitle", fallback: "LiteLLM Menu")
            : "\(localized("appTitle", fallback: "LiteLLM Menu")) — \(localized("serviceUnavailable", fallback: "service unavailable"))"
        if let status = statusItem.menu?.item(withTag: 1) {
            status.title = statusTitle
            configureStatusMenuItem(status)
        }
    }

    func setLocalization(_ values: [String: String]) {
        for (key, value) in values where !value.isEmpty { strings[key] = value }
        ensureSystemEditMenu(updateExisting: true)
        updateApplicationMenuTitles()
        refreshStatusMenu()
        for (route, window) in routeWindows {
            if let title = routeWindowTitle(route) {
                // Snapshot-driven localization can run as often as live log
                // polling. Update only localized presentation here; applying
                // the full route geometry would undo a user's resize every poll.
                window.title = title
            }
        }
    }

    func setMenuActions(_ actions: [[String: Any]]) {
        let nextActions = actions.compactMap { action -> MenuAction? in
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
        guard nextActions.count != menuActions.count || zip(nextActions, menuActions).contains(where: { !$0.matches($1) }) else {
            return
        }
        menuActions = nextActions
        refreshStatusMenu()
        installLanguageMenuIfAvailable()
    }

    func open(route: String, title: String, initialLogTab: String? = nil) {
        // The legacy app was menu-bar first. "home" exists only as a routing
        // target for RN, not as a dashboard window.
        guard route != "home" else {
            hideHostWindow()
            return
        }

        // React owns every settings route so state, validation, and actions
        // stay shared with Windows. Fabric component views below that surface
        // supply AppKit controls, focus behavior, and system appearance.
        let windowRoute = canonicalRoute(route)
        ensureReactHostStarted()
        let window: NSWindow
        if let existing = routeWindows[windowRoute] {
            if let initialLogTab,
               let refreshed = routeWindowFactory?(route, initialLogTab, existing) {
                window = refreshed
                routeWindows[windowRoute] = window
                window.delegate = self
            } else {
                window = existing
            }
            window.title = title
        } else if let created = routeWindowFactory?(route, initialLogTab, nil) {
            window = created
            routeWindows[windowRoute] = window
            window.delegate = self
            window.isReleasedWhenClosed = false
            configure(window, for: windowRoute, title: title)
        } else {
            return
        }
        updateActivationPolicy()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func open(route: String) {
        guard let title = routeWindowTitle(route) else { return }
        open(route: route, title: title)
    }

    func close(route: String? = nil) {
        let selectedRoute = route.map(canonicalRoute)
            ?? NSApp.keyWindow.flatMap(routeForWindow)
        guard let selectedRoute, let window = routeWindows[selectedRoute] else { return }
        approvedCloseRoutes.insert(selectedRoute)
        defer { approvedCloseRoutes.remove(selectedRoute) }
        window.close()
        routeWindows.removeValue(forKey: selectedRoute)
        updateActivationPolicy()
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
              let window = activeWindow()
        else {
            return false
        }

        window.setContentSize(NSSize(width: width, height: height))
        return true
    }

    /// RCTAppDelegate creates the primary React host during launch. Keep it
    /// alive for menu/service state, but route content lives in independent
    /// React windows created by ``routeWindowFactory``.
    public func hideHostWindowAtLaunch(_ window: NSWindow?) {
        if let window {
            hostWindow = window
        }
        hideHostWindow()
    }

    public func windowShouldClose(_ sender: NSWindow) -> Bool {
        if let route = routeForWindow(sender) {
            if approvedCloseRoutes.contains(route) {
                return true
            }
            requestClose(route: route)
            return false
        }
        return true
    }

    public func windowWillClose(_ notification: Notification) {
        if let window = notification.object as? NSWindow,
           let route = routeForWindow(window) {
            routeWindows.removeValue(forKey: route)
            updateActivationPolicy()
            return
        }
    }

    func chooseImportFile(purpose: String = "import") -> String? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        return panel.runModal() == .OK ? registerSelection(panel.url, purpose: purpose) : nil
    }

    func chooseExportFile(suggestedName: String) -> String? {
        let trimmed = suggestedName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              trimmed.utf8.count <= 255,
              !trimmed.contains("/"),
              !trimmed.contains(":") else { return nil }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = trimmed
        panel.canCreateDirectories = true
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

    func showReadOnlyText(title: String, text: String, closeTitle: String) {
        let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 680, height: 420))
        scrollView.borderType = .bezelBorder
        scrollView.hasHorizontalScroller = true
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true

        let textView = NSTextView(frame: scrollView.contentView.bounds)
        textView.string = text
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = false
        textView.importsGraphics = false
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.textContainerInset = NSSize(width: 8, height: 8)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = true
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.containerSize = NSSize(
            width: CGFloat.greatestFiniteMagnitude,
            height: CGFloat.greatestFiniteMagnitude
        )
        textView.textContainer?.widthTracksTextView = false
        textView.setAccessibilityLabel(title)
        scrollView.documentView = textView

        let alert = NSAlert()
        alert.messageText = title
        alert.accessoryView = scrollView
        alert.addButton(withTitle: closeTitle)
        alert.buttons.first?.keyEquivalent = "\u{1b}"
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    func showActionMenu(title: String, items: [String], anchor: [String: NSNumber]) -> Int? {
        guard !title.isEmpty, title.utf8.count <= 160,
              !items.isEmpty, items.count <= 32,
              items.allSatisfy({ !$0.isEmpty && $0.utf8.count <= 240 }),
              let x = anchor["x"]?.doubleValue,
              let y = anchor["y"]?.doubleValue,
              let width = anchor["width"]?.doubleValue,
              let height = anchor["height"]?.doubleValue,
              x.isFinite, y.isFinite, width.isFinite, height.isFinite,
              x >= 0, y >= 0, width > 0, height > 0,
              width <= 8_192, height <= 8_192 else { return nil }
        guard let window = activeWindow(), let contentView = window.contentView else { return nil }
        let contentBounds = contentView.bounds
        guard x + width <= contentBounds.width + 1, y + height <= contentBounds.height + 1 else { return nil }
        let menu = NSMenu(title: title)
        let target = NativeActionMenuTarget()
        for (index, itemTitle) in items.enumerated() {
            let item = NSMenuItem(title: itemTitle, action: #selector(NativeActionMenuTarget.select(_:)), keyEquivalent: "")
            item.target = target
            item.tag = index
            menu.addItem(item)
        }
        // React Native's macOS measureInWindow result uses the host view's
        // coordinate system. Preserve that native Y coordinate for an ordinary
        // AppKit content view; converting it a second time moves a top toolbar
        // menu to the bottom of the dialog. A flipped host still needs the
        // button's visual lower edge.
        let pointY = contentView.isFlipped ? y + height : y
        let point = NSPoint(x: x, y: pointY)
        _ = menu.popUp(positioning: nil, at: point, in: contentView)
        return target.selectedIndex
    }

    func relayLogin(
        accountID: String,
        type: String,
        label: String,
        origin: String,
        language: String,
        username: String?,
        rememberPassword: Bool,
        completion: @escaping (CoreIPCBridge.RelayLoginResult?) -> Void
    ) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in
                self?.relayLogin(
                    accountID: accountID,
                    type: type,
                    label: label,
                    origin: origin,
                    language: language,
                    username: username,
                    rememberPassword: rememberPassword,
                    completion: completion
                )
            }
            return
        }
        guard accountID.range(of: #"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"#, options: .regularExpression) != nil,
              ["newapi", "sub2api"].contains(type),
              label.utf8.count <= 160,
              origin.utf8.count <= 2_048,
              let originURL = URL(string: origin),
              NativeRelayOriginPolicy.allows(originURL),
              originURL.host != nil,
              originURL.user == nil,
              originURL.password == nil,
              originURL.query == nil,
              originURL.fragment == nil,
              ["system", "en", "zh-Hans"].contains(language),
              (username?.utf8.count ?? 0) <= 320,
              activeRelayLoginController == nil else {
            completion(nil)
            return
        }
        let canonicalOrigin = URLComponents(url: originURL, resolvingAgainstBaseURL: false).map { components -> URL in
            var normalized = components
            let path = normalized.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            normalized.path = path.isEmpty ? "" : "/" + path
            return normalized.url ?? originURL
        } ?? originURL
        let controller = NativeRelayLoginController(
            accountID: accountID,
            type: type,
            label: label,
            originURL: canonicalOrigin,
            language: language,
            username: username,
            rememberPassword: rememberPassword
        )
        activeRelayLoginController = controller
        controller.start { [weak self] result in
            self?.activeRelayLoginController = nil
            completion(result)
        }
    }

    func openRelayLogs(
        accountID: String,
        type: String,
        label: String,
        origin: String,
        language: String,
        completion: @escaping () -> Void
    ) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in
                self?.openRelayLogs(
                    accountID: accountID,
                    type: type,
                    label: label,
                    origin: origin,
                    language: language,
                    completion: completion
                )
            }
            return
        }
        guard accountID.range(of: #"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"#, options: .regularExpression) != nil,
              ["newapi", "sub2api"].contains(type),
              label.utf8.count <= 160,
              origin.utf8.count <= 2_048,
              let originURL = URL(string: origin),
              NativeRelayOriginPolicy.allows(originURL),
              originURL.host != nil,
              originURL.user == nil,
              originURL.password == nil,
              originURL.query == nil,
              originURL.fragment == nil,
              ["system", "en", "zh-Hans"].contains(language),
              activeRelayLoginController == nil else {
            completion()
            return
        }
        let canonicalOrigin = URLComponents(url: originURL, resolvingAgainstBaseURL: false).map { components -> URL in
            var normalized = components
            let path = normalized.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            normalized.path = path.isEmpty ? "" : "/" + path
            return normalized.url ?? originURL
        } ?? originURL
        let controller = NativeRelayLoginController(
            accountID: accountID,
            type: type,
            label: label,
            originURL: canonicalOrigin,
            language: language,
            username: nil,
            rememberPassword: false,
            mode: .logs
        )
        activeRelayLoginController = controller
        controller.start { [weak self] _ in
            self?.activeRelayLoginController = nil
            completion()
        }
    }

    func clearRelayCredentials(accountID: String) -> Bool {
        guard accountID.range(of: #"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"#, options: .regularExpression) != nil else { return false }
        return NativeRelayCredentialStore.clear(accountID: accountID)
    }

    func restoreRelaySession(
        accountID: String,
        type: String,
        label: String,
        origin: String,
        username: String?
    ) -> CoreIPCBridge.RelaySessionRestoreResult? {
        guard accountID.range(of: #"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"#, options: .regularExpression) != nil,
              ["newapi", "sub2api"].contains(type),
              !label.isEmpty, label.utf8.count <= 160,
              origin.utf8.count <= 2_048,
              let originURL = URL(string: origin),
              NativeRelayOriginPolicy.allows(originURL),
              originURL.host != nil,
              originURL.user == nil,
              originURL.password == nil,
              originURL.query == nil,
              originURL.fragment == nil,
              (username?.utf8.count ?? 0) <= 320 else { return nil }
        let canonicalOrigin = URLComponents(url: originURL, resolvingAgainstBaseURL: false).map { components -> URL in
            var normalized = components
            let path = normalized.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            normalized.path = path.isEmpty ? "" : "/" + path
            return normalized.url ?? originURL
        } ?? originURL
        guard let session = NativeRelayCredentialStore.readSession(
            accountID: accountID,
            accountType: type,
            origin: canonicalOrigin.absoluteString
        ) else {
            return try? CoreIPCBridge.shared.restoreRelaySession(
                accountID: accountID,
                type: type,
                label: label,
                origin: canonicalOrigin.absoluteString,
                loginStatus: "signed_out"
            )
        }
        switch NativeRelaySessionProbe.verify(
            type: type,
            originURL: canonicalOrigin,
            presetUsername: username?.trimmingCharacters(in: .whitespacesAndNewlines),
            session: session
        ) {
        case .verified(let probe):
            let refreshedSession = NativeRelaySession(
                accountType: type,
                origin: canonicalOrigin.absoluteString,
                cookie: probe.cookie,
                accessToken: probe.accessToken,
                refreshToken: probe.refreshToken
            )
            guard NativeRelayCredentialStore.writeSession(refreshedSession, accountID: accountID) else {
                return nil
            }
            return try? CoreIPCBridge.shared.restoreRelaySession(
                accountID: accountID,
                type: type,
                label: label,
                origin: canonicalOrigin.absoluteString,
                loginStatus: "signed_in",
                username: probe.username,
                cookie: probe.cookie,
                accessToken: probe.accessToken,
                refreshToken: probe.refreshToken
            )
        case .expired:
            return try? CoreIPCBridge.shared.restoreRelaySession(
                accountID: accountID,
                type: type,
                label: label,
                origin: canonicalOrigin.absoluteString,
                loginStatus: "expired"
            )
        case .unavailable:
            // A transient network failure cannot safely be presented as an
            // expired login. Keep the last known state until a later native
            // check can verify the outcome.
            return nil
        }
    }

    func clearRelayPassword(accountID: String) -> Bool {
        guard accountID.range(of: #"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"#, options: .regularExpression) != nil else { return false }
        return NativeRelayCredentialStore.clearPassword(accountID: accountID)
    }

    func chooseModelsToAdd(models: [String], providerName: String, keyName: String) -> [String]? {
        let candidates = models
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !candidates.isEmpty else { return [] }

        let contentWidth: CGFloat = 620
        let rowHeight: CGFloat = 28
        let listHeight = min(480, max(220, CGFloat(candidates.count) * rowHeight + 2))
        let controller = NativeModelChooserController(
            models: candidates,
            width: contentWidth - 36,
            countTemplate: localized("modelChooserCount", fallback: "{count} models"),
            filteredCountTemplate: localized("modelChooserCountFiltered", fallback: "{visible} of {total} models"),
            selectedCountTemplate: localized("modelChooserCountSelected", fallback: "{count} selected"),
            emptyLabel: localized("modelChooserEmpty", fallback: "No models available"),
            noMatchesLabel: localized("modelChooserNoMatches", fallback: "No matching models")
        )
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
        panel.title = localized("modelChooserTitle", fallback: "Choose Models to Add")
        panel.minSize = NSSize(width: 520, height: 340)
        panel.isReleasedWhenClosed = false
        panel.delegate = controller
        controller.modalWindow = panel

        let content = NSView()
        panel.contentView = content
        let titleLabel = NSTextField(labelWithString: localized("modelChooserHeading", fallback: "Choose models to add"))
        titleLabel.font = NSFont.systemFont(ofSize: 16, weight: .semibold)
        let subtitleLabel = NSTextField(labelWithString: "\(localized("modelChooserProvider", fallback: "Provider")): \(providerName)    \(localized("modelChooserKey", fallback: "Key")): \(keyName)")
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.lineBreakMode = .byTruncatingMiddle
        let searchField = NSSearchField()
        searchField.placeholderString = localized("modelChooserSearch", fallback: "Search models")
        searchField.sendsSearchStringImmediately = true
        searchField.sendsWholeSearchString = false

        let selectionControls = NSStackView()
        selectionControls.orientation = .horizontal
        selectionControls.alignment = .centerY
        selectionControls.spacing = 8
        let selectAllButton = modelChooserButton(title: localized("modelChooserAll", fallback: "All"), toolTip: localized("modelChooserSelectAllVisible", fallback: "Select all visible models"))
        selectAllButton.target = controller
        selectAllButton.action = #selector(NativeModelChooserController.selectAllAction(_:))
        let invertButton = modelChooserButton(title: localized("modelChooserInvert", fallback: "Invert"), toolTip: localized("modelChooserInvertVisible", fallback: "Invert visible model selection"))
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

        let cancelButton = NSButton(title: localized("cancel", fallback: "Cancel"), target: controller, action: #selector(NativeModelChooserController.cancelAction(_:)))
        cancelButton.bezelStyle = .rounded
        cancelButton.keyEquivalent = "\u{1b}"
        let addButton = modelChooserButton(title: "+", toolTip: localized("modelChooserAddSelected", fallback: "Add Selected"))
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
            let item = applicationMenu.addItem(withTitle: localized("settings", fallback: "Settings…"), action: #selector(openCodex), keyEquivalent: ",")
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
        installLanguageMenu(in: applicationMenu)
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
        menu.autoenablesItems = false
        var actionMap: [String: MenuAction] = [:]
        for action in actions {
            actionMap[action.id] = action
        }
        var consumed = Set<String>()

        for marker in Self.statusMenuOrder where !Self.footerMenuActionIDs.contains(marker) {
            switch marker {
            case "status":
                let status = menu.addItem(withTitle: statusTitle, action: nil, keyEquivalent: "")
                status.tag = 1
                configureStatusMenuItem(status)
            case "separator":
                if menu.items.last?.isSeparatorItem == false {
                    menu.addItem(.separator())
                }
            case let id where Self.footerMenuActionIDs.contains(id):
                addMenuActionItem(id, from: actionMap, to: menu, consumed: &consumed)
            case let id:
                addMenuActionItem(id, from: actionMap, to: menu, consumed: &consumed)
            }
        }

        for action in actions where !consumed.contains(action.id) &&
            !Self.footerMenuActionIDs.contains(action.id) &&
            !Self.suppressedStatusMenuActionIDs.contains(action.id) &&
            !Self.applicationMenuActionIDs.contains(action.id) {
            addMenuItem(action.id, title: action.title, enabled: action.enabled, checked: action.checked, to: menu)
            consumed.insert(action.id)
        }

        if menu.items.last?.isSeparatorItem == false { menu.addItem(.separator()) }
        for marker in Self.statusMenuOrder where Self.footerMenuActionIDs.contains(marker) {
            addMenuActionItem(marker, from: actionMap, to: menu, consumed: &consumed)
        }
        return menu
    }

    private func refreshStatusMenu() {
        guard !menuTracking else {
            menuNeedsRefresh = true
            return
        }
        menuNeedsRefresh = false
        statusItem.menu = makeMenu(actions: menuActions)
    }

    public func menuWillOpen(_ menu: NSMenu) {
        menuTracking = true
    }

    public func menuDidClose(_ menu: NSMenu) {
        menuTracking = false
        guard menuNeedsRefresh else { return }
        refreshStatusMenu()
    }

    private func addMenuActionItem(
        _ id: String,
        from actions: [String: MenuAction],
        to menu: NSMenu,
        consumed: inout Set<String>
    ) {
        if let action = actions[id] {
            addMenuItem(id, title: menuTitle(for: id, fallback: action.title), enabled: action.enabled, checked: action.checked, to: menu)
            consumed.insert(id)
            return
        }

        guard let fallback = menuFallback(for: id) else { return }
        addMenuItem(id, title: fallback, to: menu, keyEquivalent: menuKeyEquivalent(for: id))
    }

    private func menuTitle(for id: String, fallback: String) -> String {
        switch id {
        case "open-providers-models": return localized("routeProvidersModels", fallback: fallback) + "…"
        case "open-relay-accounts": return localized("routeRelayAccounts", fallback: fallback) + "…"
        case "open-runtime-settings": return localized("routeRuntimeSettings", fallback: fallback) + "…"
        case "open-codex-settings": return localized("routeCodexSettings", fallback: fallback) + "…"
        case "open-webdav-settings": return localized("routeWebdavSettings", fallback: fallback) + "…"
        case "open-logs", "open-logs?tab=recovery": return fallback
        case "quit": return localized("menuQuit", fallback: fallback)
        default: return fallback
        }
    }

    private func menuFallback(for id: String) -> String? {
        switch id {
        case "toggle-autostart": return localized("autoStart", fallback: "Auto Start at Login")
        case "open-providers-models": return localized("routeProvidersModels", fallback: "Providers & Models") + "…"
        case "open-relay-accounts": return localized("routeRelayAccounts", fallback: "Relay Accounts") + "…"
        case "open-runtime-settings": return localized("routeRuntimeSettings", fallback: "Runtime Settings") + "…"
        case "open-codex-settings": return localized("routeCodexSettings", fallback: "Codex / Claude Settings") + "…"
        case "open-webdav-settings": return localized("routeWebdavSettings", fallback: "WebDAV Sync Settings") + "…"
        case "open-logs", "open-logs?tab=recovery": return localized("routeLogs", fallback: "Logs")
        case "show-version": return localized("version", fallback: "Version")
        case "quit": return localized("menuQuit", fallback: "Quit LiteLLM Menu")
        default: return nil
        }
    }

    private func menuKeyEquivalent(for id: String) -> String {
        switch id {
        case "quit": return "q"
        default: return ""
        }
    }

    private func addMenuItem(
        _ id: String,
        title: String,
        enabled: Bool = true,
        checked: Bool = false,
        to menu: NSMenu,
        keyEquivalent: String = ""
    ) {
        let item = NSMenuItem(title: title, action: #selector(menuAction(_:)), keyEquivalent: keyEquivalent)
        item.keyEquivalentModifierMask = keyEquivalent.isEmpty ? [] : [.command]
        item.representedObject = id
        item.isEnabled = enabled
        item.state = checked ? .on : .off
        item.target = self
        if id == "webdav-status" {
            configureStatusMenuItem(item)
        }
        menu.addItem(item)
    }

    private func configureStatusMenuItem(_ item: NSMenuItem) {
        item.action = nil
        item.target = nil
        item.isEnabled = false
        item.attributedTitle = NSAttributedString(
            string: item.title,
            attributes: [.foregroundColor: NSColor.secondaryLabelColor]
        )
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
        installLanguageMenu(in: applicationMenu)
    }

    private func installLanguageMenuIfAvailable() {
        guard let applicationMenu = NSApp.mainMenu?.items.first?.submenu else { return }
        installLanguageMenu(in: applicationMenu)
    }

    private func installLanguageMenu(in applicationMenu: NSMenu) {
        applicationMenu.items
            .filter { $0.representedObject as? String == "native-language-menu" }
            .forEach(applicationMenu.removeItem)
        let root = NSMenuItem(title: localized("languageMenu", fallback: "Language"), action: nil, keyEquivalent: "")
        root.representedObject = "native-language-menu"
        let submenu = NSMenu(title: root.title)
        for (id, key, fallback) in [
            ("set-language-system", "languageSystem", "System"),
            ("set-language-en", "languageEnglish", "English"),
            ("set-language-zh-Hans", "languageSimplifiedChinese", "简体中文"),
        ] {
            let choice = menuActions.first(where: { $0.id == id })
            let item = NSMenuItem(title: localized(key, fallback: fallback), action: #selector(menuAction(_:)), keyEquivalent: "")
            item.representedObject = id
            item.target = self
            item.isEnabled = choice?.enabled ?? false
            item.state = choice?.checked == true ? .on : .off
            submenu.addItem(item)
        }
        root.submenu = submenu
        applicationMenu.addItem(root)
    }

    private func openNamedRoute(_ route: String) {
        guard let title = routeWindowTitle(route) else { return }
        open(route: route, title: title)
        emitAction("open-\(route)")
    }

    public func openRouteFromDeepLink(_ route: String, logTab: String?) {
        guard let title = routeWindowTitle(route) else { return }
        guard logTab == nil || (route == "logs" && isAllowedLogTab(logTab!)) else { return }
        open(route: route, title: title, initialLogTab: logTab)
        if let logTab {
            emitAction("open-logs?tab=\(logTab)")
        } else {
            emitAction("open-\(route)")
        }
    }

    private func requestClose(route: String?) {
        guard let route else { return }
        emitAction("request-close-\(route)")
    }

    private func canonicalRoute(_ route: String) -> String {
        route == "claude-settings" ? "codex-settings" : route
    }

    private func routeForWindow(_ window: NSWindow) -> String? {
        routeWindows.first(where: { $0.value === window })?.key
    }

    private func activeWindow() -> NSWindow? {
        if let keyWindow = NSApp.keyWindow, routeForWindow(keyWindow) != nil {
            return keyWindow
        }
        return routeWindows.values.first ?? hostWindow
    }

    private func updateActivationPolicy() {
        if routeWindows.isEmpty {
            NSApp.setActivationPolicy(.accessory)
            return
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.applicationIconImage = Self.applicationIcon
    }

    private func configure(_ window: NSWindow, for route: String, title: String) {
        let layout = routeWindowLayout(for: route)
        window.title = title
        window.minSize = layout.minSize
        window.maxSize = layout.maxSize ?? Self.unconstrainedWindowSize
        window.setContentSize(layout.contentSize)
        window.center()
        window.collectionBehavior = [.fullScreenPrimary]
        window.level = .normal
    }

    private func hideHostWindow() {
        guard let window = hostWindow else { return }
        window.orderOut(nil)
        updateActivationPolicy()
    }

    private func routeWindowLayout(for route: String) -> RouteWindowLayout {
        switch route {
        case "providers-models":
            return RouteWindowLayout(
                contentSize: NSSize(width: 1052, height: 600),
                minSize: NSSize(width: 1052, height: 560),
                maxSize: nil
            )
        case "relay-accounts":
            return RouteWindowLayout(
                contentSize: NSSize(width: 920, height: 620),
                minSize: NSSize(width: 760, height: 500),
                maxSize: nil
            )
        case "codex-settings", "claude-settings":
            return RouteWindowLayout(
                contentSize: NSSize(width: 1160, height: 700),
                minSize: NSSize(width: 1100, height: 640),
                maxSize: nil
            )
        case "runtime-settings":
            return RouteWindowLayout(
                contentSize: NSSize(width: 1080, height: 620),
                minSize: NSSize(width: 800, height: 520),
                maxSize: NSSize(width: 1160, height: CGFloat.greatestFiniteMagnitude)
            )
        case "webdav-settings":
            return RouteWindowLayout(
                contentSize: NSSize(width: 720, height: 440),
                minSize: NSSize(width: 700, height: 420),
                maxSize: nil
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
        ensureReactHostStarted()
        if let menuActionHandler {
            menuActionHandler(action)
        } else {
            pendingActions.append(action)
        }
    }

    private func ensureReactHostStarted() {
        guard routeWindowFactory == nil else { return }
        reactHostStarter?()
    }

    private func routeTitle(_ route: String) -> String? {
        switch route {
        case "home": return localized("routeHome", fallback: "LiteLLM Menu")
        case "providers-models": return localized("routeProvidersModels", fallback: "Providers & Models")
        case "relay-accounts": return localized("routeRelayAccounts", fallback: "Relay Accounts")
        case "codex-settings", "claude-settings": return localized("routeCodexSettings", fallback: "Codex / Claude Settings")
        case "runtime-settings": return localized("routeRuntimeSettings", fallback: "Runtime Settings")
        case "webdav-settings": return localized("routeWebdavSettings", fallback: "WebDAV Sync Settings")
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
        case "relay-accounts": return "LiteLLM " + localized("routeRelayAccounts", fallback: "Relay Accounts")
        case "codex-settings", "claude-settings": return localized("routeCodexSettings", fallback: "Codex / Claude Settings")
        case "runtime-settings": return localized("routeRuntimeSettings", fallback: "Runtime Settings")
        case "webdav-settings": return localized("routeWebdavSettings", fallback: "WebDAV Sync Settings")
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
    @objc private func openRelayAccounts() { openNamedRoute("relay-accounts") }
    @objc private func openCodex() { openNamedRoute("codex-settings") }
    @objc private func openClaude() { openNamedRoute("claude-settings") }
    @objc private func openRuntime() { openNamedRoute("runtime-settings") }
    @objc private func openWebDAV() { openNamedRoute("webdav-settings") }
    private func openLogs(tab: String?) {
        guard let title = routeWindowTitle("logs") else { return }
        open(route: "logs", title: title, initialLogTab: tab)
        emitAction(tab.map { "open-logs?tab=\($0)" } ?? "open-logs")
    }
    @objc private func openLogs() { openLogs(tab: nil) }
    @objc private func reloadFromShortcut() { emitAction("service-reload") }
    @objc private func closeFromShortcut() { requestClose(route: NSApp.keyWindow.flatMap(routeForWindow)) }
    @objc private func menuAction(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        switch id {
        case "open-providers-models": openProviders()
        case "open-relay-accounts": openRelayAccounts()
        case "open-codex-settings": openCodex()
        case "open-claude-settings": openClaude()
        case "open-runtime-settings": openRuntime()
        case "open-webdav-settings": openWebDAV()
        case "open-logs", "open-logs?tab=recovery": openLogs(tab: id == "open-logs?tab=recovery" ? "recovery" : nil)
        case "toggle-autostart":
            emitAction(id)
        case "show-version": showVersion()
        case "quit": quit()
        default: emitAction(id)
        }
    }
    func requestQuit() {
        NSApp.terminate(nil)
    }

    public func prepareForTermination() {
        statusItem.menu = nil
        NSStatusBar.system.removeStatusItem(statusItem)
        for window in routeWindows.values { window.orderOut(nil) }
        hostWindow?.orderOut(nil)
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
    private let emptyLabel: String
    private let noMatchesLabel: String
    var stateDidChange: (() -> Void)?
    let rowHeight: CGFloat = 28

    override class var isCompatibleWithResponsiveScrolling: Bool { true }
    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }

    init(models: [String], width: CGFloat, emptyLabel: String, noMatchesLabel: String) {
        rows = models.map { Row(title: $0, selected: false) }
        visibleRowIndexes = Array(models.indices)
        self.emptyLabel = emptyLabel
        self.noMatchesLabel = noMatchesLabel
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
            let label = NSTextField(labelWithString: searchQuery.isEmpty ? emptyLabel : noMatchesLabel)
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
    private let countTemplate: String
    private let filteredCountTemplate: String
    private let selectedCountTemplate: String

    init(models: [String], width: CGFloat, countTemplate: String, filteredCountTemplate: String, selectedCountTemplate: String, emptyLabel: String, noMatchesLabel: String) {
        listView = NativeModelChooserListView(models: models, width: width, emptyLabel: emptyLabel, noMatchesLabel: noMatchesLabel)
        self.countTemplate = countTemplate
        self.filteredCountTemplate = filteredCountTemplate
        self.selectedCountTemplate = selectedCountTemplate
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
        var summary = listView.hasActiveSearch
            ? filteredCountTemplate
                .replacingOccurrences(of: "{visible}", with: String(listView.visibleCount))
                .replacingOccurrences(of: "{total}", with: String(listView.totalCount))
            : countTemplate.replacingOccurrences(of: "{count}", with: String(listView.totalCount))
        if listView.selectedCount > 0 {
            summary += "  |  " + selectedCountTemplate.replacingOccurrences(of: "{count}", with: String(listView.selectedCount))
        }
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

private final class NativeActionMenuTarget: NSObject {
    private(set) var selectedIndex: Int?

    @objc func select(_ sender: NSMenuItem) {
        selectedIndex = sender.tag
    }
}

private final class NativeRelayLoginAttempt {
    private enum State {
        case pending
        case committing
        case finished
    }

    enum CancellationOutcome {
        case cancelled
        case committing
        case finished
    }

    private let lock = NSLock()
    private var state: State = .pending

    func requestCancellation() -> CancellationOutcome {
        lock.lock()
        defer { lock.unlock() }
        switch state {
        case .pending:
            state = .finished
            return .cancelled
        case .committing:
            return .committing
        case .finished:
            return .finished
        }
    }

    func beginCommit() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard state == .pending else { return false }
        state = .committing
        return true
    }

    func isCommitting() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return state == .committing
    }

    func finish() {
        lock.lock()
        state = .finished
        lock.unlock()
    }

    func isActive() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return state != .finished
    }
}

private enum NativeRelayBrowserMode {
    case login
    case logs
}

private final class NativeRelayLoginController: NSObject, NSWindowDelegate, WKNavigationDelegate, URLSessionTaskDelegate {
    private struct Probe {
        let path: String
        let usernamePaths: [[String]]
    }

    private let accountID: String
    private let type: String
    private let label: String
    private let originURL: URL
    private let language: String
    private let presetUsername: String?
    private let rememberPassword: Bool
    private let mode: NativeRelayBrowserMode
    private let passwordCapture: NativeRelayPasswordCapture
    private lazy var session = URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)
    private let panel: NSPanel
    private let webView: WKWebView
    private let loadingOverlay = NSVisualEffectView()
    private let loadingIndicator = NSProgressIndicator()
    private let loadingLabel = NSTextField(labelWithString: "")
    private let statusLabel = NSTextField(labelWithString: "")
    private let accountLabel = NSTextField(labelWithString: "")
    private let signInButton = NSButton(title: "Sign In", target: nil, action: nil)
    private let cancelButton = NSButton(title: "", target: nil, action: nil)
    private var finished = false
    private var checking = false
    private var result: CoreIPCBridge.RelayLoginResult?
    private var capturedPassword: String?
    private var capturedAccessToken: String?
    private var capturedRefreshToken: String?
    private var restoredSession: NativeRelaySession?
    private var didRestoreSession = false
    private var didLoadInitialPage = false
    private var didProbeRestoredSession = false
    private var pageReadinessProbe: DispatchWorkItem?
    private var panelClosedDuringCommit = false
    private var activeCheck: NativeRelayLoginAttempt?
    private var completion: ((CoreIPCBridge.RelayLoginResult?) -> Void)?

    init(
        accountID: String,
        type: String,
        label: String,
        originURL: URL,
        language: String,
        username: String?,
        rememberPassword: Bool,
        mode: NativeRelayBrowserMode = .login
    ) {
        self.accountID = accountID
        self.type = type
        self.label = label
        self.originURL = originURL
        self.language = language
        self.presetUsername = username?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.rememberPassword = rememberPassword
        self.mode = mode
        passwordCapture = NativeRelayPasswordCapture()

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        if rememberPassword {
            configuration.userContentController.add(passwordCapture, name: "relayPassword")
            configuration.userContentController.addUserScript(WKUserScript(
                source: """
                (() => {
                  const send = (value) => {
                    if (typeof value === 'string' && value.length > 0) {
                      window.webkit.messageHandlers.relayPassword.postMessage(value);
                    }
                  };
                  document.addEventListener('input', (event) => {
                    if (event.target?.matches?.('input[type=password], input[autocomplete=current-password]')) send(event.target.value);
                  }, true);
                  document.addEventListener('change', (event) => {
                    if (event.target?.matches?.('input[type=password], input[autocomplete=current-password]')) send(event.target.value);
                  }, true);
                  document.addEventListener('submit', (event) => {
                    send(event.target?.querySelector?.('input[type=password], input[autocomplete=current-password]')?.value || '');
                  }, true);
                })();
                """,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            ))
        }
        webView = WKWebView(frame: .zero, configuration: configuration)
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 820, height: 680),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        super.init()
        buildPanel()
    }

    func start(completion: @escaping (CoreIPCBridge.RelayLoginResult?) -> Void) {
        self.completion = completion
        beginBrowserFlow()
        NSApp.activate(ignoringOtherApps: true)
        panel.center()
        panel.makeKeyAndOrderFront(nil)
        restoreSessionAndLoad()
    }

    /// Each controller represents one browser login flow. Clear temporary
    /// captures before its initial navigation rather than when checking a
    /// completed page: a successful login often removes the password field.
    private func beginBrowserFlow() {
        _ = activeCheck?.requestCancellation()
        activeCheck = nil
        clearCapturedCredentials()
    }

    private var loginURL: URL {
        if mode == .logs {
            return relayURL(path: type == "newapi" ? "usage-logs" : "usage") ?? originURL
        }
        guard type == "sub2api" else { return originURL }
        return originURL.appendingPathComponent("login")
    }

    private var localizedChinese: Bool {
        language == "zh-Hans" || (language == "system" && Locale.preferredLanguages.first?.lowercased().hasPrefix("zh") == true)
    }

    private func text(_ english: String, _ chinese: String) -> String {
        localizedChinese ? chinese : english
    }

    private func buildPanel() {
        panel.title = mode == .logs
            ? text("Relay Usage Logs", "中转站用量日志")
            : text("Relay Account Sign In", "中转站账号登录")
        panel.minSize = NSSize(width: 720, height: 560)
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        webView.navigationDelegate = self

        let header = NSView()
        let titleLabel = NSTextField(labelWithString: label)
        titleLabel.font = NSFont.systemFont(ofSize: 17, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingTail
        accountLabel.stringValue = "\(type == "newapi" ? text("New API", "NewAPI") : "Sub2API")  |  \(originURL.host ?? originURL.absoluteString)"
        accountLabel.textColor = .secondaryLabelColor
        statusLabel.stringValue = mode == .logs
            ? text("Showing the relay site's usage logs.", "正在显示中转站站内用量日志。")
            : text("Sign in on the site, then select Check Sign-In.", "请在站点内完成登录，然后选择“检查登录”。")
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.lineBreakMode = .byTruncatingTail
        signInButton.title = mode == .logs ? text("Reload", "刷新") : text("Check Sign-In", "检查登录")
        signInButton.target = self
        signInButton.action = mode == .logs ? #selector(reloadBrowser(_:)) : #selector(checkSignIn(_:))
        signInButton.bezelStyle = .rounded
        signInButton.keyEquivalent = "\r"
        cancelButton.title = mode == .logs ? text("Close", "关闭") : text("Cancel", "取消")
        cancelButton.target = self
        cancelButton.action = #selector(cancel(_:))
        cancelButton.bezelStyle = .rounded
        cancelButton.keyEquivalent = "\u{1b}"

        [titleLabel, accountLabel, statusLabel, signInButton, cancelButton].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            header.addSubview($0)
        }
        webView.translatesAutoresizingMaskIntoConstraints = false
        loadingOverlay.material = .contentBackground
        loadingOverlay.blendingMode = .withinWindow
        loadingOverlay.state = .active
        loadingOverlay.translatesAutoresizingMaskIntoConstraints = false
        loadingIndicator.style = .spinning
        loadingIndicator.controlSize = .regular
        loadingIndicator.translatesAutoresizingMaskIntoConstraints = false
        loadingIndicator.startAnimation(nil)
        loadingLabel.stringValue = mode == .logs
            ? text("Loading usage logs…", "正在加载用量日志…")
            : text("Loading sign-in page…", "正在加载登录页面…")
        loadingLabel.textColor = .secondaryLabelColor
        loadingLabel.alignment = .center
        loadingLabel.translatesAutoresizingMaskIntoConstraints = false
        loadingOverlay.addSubview(loadingIndicator)
        loadingOverlay.addSubview(loadingLabel)
        let content = NSView()
        content.addSubview(header)
        content.addSubview(webView)
        content.addSubview(loadingOverlay)
        header.translatesAutoresizingMaskIntoConstraints = false
        panel.contentView = content

        NSLayoutConstraint.activate([
            header.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            header.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            header.topAnchor.constraint(equalTo: content.topAnchor),
            header.heightAnchor.constraint(equalToConstant: 76),
            titleLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: 18),
            titleLabel.topAnchor.constraint(equalTo: header.topAnchor, constant: 12),
            titleLabel.trailingAnchor.constraint(lessThanOrEqualTo: cancelButton.leadingAnchor, constant: -16),
            accountLabel.leadingAnchor.constraint(equalTo: titleLabel.trailingAnchor, constant: 10),
            accountLabel.centerYAnchor.constraint(equalTo: titleLabel.centerYAnchor),
            accountLabel.trailingAnchor.constraint(lessThanOrEqualTo: cancelButton.leadingAnchor, constant: -16),
            statusLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: 18),
            statusLabel.trailingAnchor.constraint(lessThanOrEqualTo: signInButton.leadingAnchor, constant: -16),
            statusLabel.bottomAnchor.constraint(equalTo: header.bottomAnchor, constant: -12),
            signInButton.trailingAnchor.constraint(equalTo: cancelButton.leadingAnchor, constant: -8),
            signInButton.centerYAnchor.constraint(equalTo: header.centerYAnchor),
            signInButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 104),
            cancelButton.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -18),
            cancelButton.centerYAnchor.constraint(equalTo: header.centerYAnchor),
            cancelButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 76),
            webView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            webView.topAnchor.constraint(equalTo: header.bottomAnchor),
            webView.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            loadingOverlay.leadingAnchor.constraint(equalTo: webView.leadingAnchor),
            loadingOverlay.trailingAnchor.constraint(equalTo: webView.trailingAnchor),
            loadingOverlay.topAnchor.constraint(equalTo: webView.topAnchor),
            loadingOverlay.bottomAnchor.constraint(equalTo: webView.bottomAnchor),
            loadingIndicator.centerXAnchor.constraint(equalTo: loadingOverlay.centerXAnchor),
            loadingIndicator.centerYAnchor.constraint(equalTo: loadingOverlay.centerYAnchor, constant: -12),
            loadingLabel.topAnchor.constraint(equalTo: loadingIndicator.bottomAnchor, constant: 12),
            loadingLabel.centerXAnchor.constraint(equalTo: loadingOverlay.centerXAnchor),
        ])
    }

    private func restoreSessionAndLoad() {
        guard isBrowserFlowLive, !didLoadInitialPage else { return }
        didLoadInitialPage = true
        restoredSession = NativeRelayCredentialStore.readSession(
            accountID: accountID,
            accountType: type,
            origin: originURL.absoluteString
        )
        let cookies = restoredSession.map { cookies(fromHeader: $0.cookie) } ?? []
        guard !cookies.isEmpty else {
            webView.load(URLRequest(url: loginURL))
            return
        }
        let group = DispatchGroup()
        for cookie in cookies {
            group.enter()
            webView.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) { group.leave() }
        }
        group.notify(queue: .main) { [weak self] in
            guard let self, self.isBrowserFlowLive else { return }
            self.webView.load(URLRequest(url: self.loginURL))
        }
    }

    private func restoreLocalStorageWhenReady() {
        guard isBrowserFlowLive, !didRestoreSession else { return }
        didRestoreSession = true
        guard let session = restoredSession else { return }
        let script = """
        (() => {
          const accessToken = \(jsonLiteral(session.accessToken));
          const refreshToken = \(jsonLiteral(session.refreshToken));
          if (accessToken) {
            localStorage.setItem('auth_token', accessToken);
            localStorage.setItem('access_token', accessToken);
          }
          if (refreshToken) localStorage.setItem('refresh_token', refreshToken);
        })();
        """
        webView.evaluateJavaScript(script) { [weak self] _, _ in
            guard let self, self.isBrowserFlowLive else { return }
            if self.mode == .logs {
                if !session.accessToken.isEmpty || !session.refreshToken.isEmpty {
                    // The protected usage route may redirect to login before
                    // local storage is restored. Navigate back to the intended
                    // route after injection instead of reloading that redirect.
                    self.webView.load(URLRequest(url: self.loginURL))
                }
                return
            }
            if session.accessToken.isEmpty {
                self.probeRestoredSession()
            } else {
                self.webView.reload()
            }
        }
    }

    private func probeRestoredSession() {
        guard mode == .login, restoredSession != nil, !didProbeRestoredSession, !checking else { return }
        didProbeRestoredSession = true
        checkSignIn(nil)
    }

    private func prefillLoginWhenReady() {
        guard mode == .login, isBrowserFlowLive, let username = presetUsername, !username.isEmpty else { return }
        // Let a restored session seed local storage and reload before looking
        // for a login form. Otherwise a saved session could be sent straight
        // back to the unauthenticated form during its first page load.
        guard restoredSession == nil || didRestoreSession else { return }
        let password = rememberPassword
            ? NativeRelayCredentialStore.readPassword(
                accountID: accountID,
                accountType: type,
                origin: originURL.absoluteString
              )
            : nil
        let safeUser = jsonLiteral(username)
        let safePassword = jsonLiteral(password ?? "")
        let script = """
        (() => {
          const user = document.querySelector('input[type=email], input[type=text], input:not([type]), input[name=email], input[name=username], input[autocomplete=username], input[placeholder*="用户名"], input[placeholder*="email" i]');
          const password = document.querySelector('input[type=password], input[autocomplete=current-password]');
          if (!user) {
            const words = new Set(['login', 'log in', 'sign in', '登录']);
            const signIn = Array.from(document.querySelectorAll('a, button')).find((node) => words.has((node.textContent || node.getAttribute('aria-label') || '').trim().toLowerCase()));
            if (signIn instanceof HTMLElement) signIn.click();
            return;
          }
          const set = (node, value) => {
            if (!node || node.value) return;
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            setter?.call(node, value);
            node.dispatchEvent(new Event('input', { bubbles: true }));
            node.dispatchEvent(new Event('change', { bubbles: true }));
          };
          set(user, \(safeUser));
          if (\(safePassword)) set(password, \(safePassword));
        })();
        """
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            guard let self, self.isBrowserFlowLive else { return }
            self.webView.evaluateJavaScript(script, completionHandler: nil)
        }
    }

    private func jsonLiteral(_ value: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [value]),
              let text = String(data: data, encoding: .utf8), text.count >= 2 else { return "\"\"" }
        return String(text.dropFirst().dropLast())
    }

    @objc private func checkSignIn(_ sender: Any?) {
        guard mode == .login else {
            reloadBrowser(sender)
            return
        }
        guard !finished, !checking else { return }
        let attempt = NativeRelayLoginAttempt()
        activeCheck = attempt
        checking = true
        panelClosedDuringCommit = false
        capturedAccessToken = nil
        capturedRefreshToken = nil
        signInButton.isEnabled = false
        cancelButton.isEnabled = true
        statusLabel.stringValue = text("Checking sign-in…", "正在检查登录…")
        captureBrowserCredentials(attempt: attempt) { [weak self, weak attempt] in
            guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
            self.collectCookies(attempt: attempt) { [weak self, weak attempt] cookieHeader in
                guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                self.probe(index: 0, cookieHeader: cookieHeader, attempt: attempt)
            }
        }
    }

    private func isCurrentCheck(_ attempt: NativeRelayLoginAttempt) -> Bool {
        !finished && checking && activeCheck === attempt && attempt.isActive()
    }

    private var isBrowserFlowLive: Bool {
        !finished && !panelClosedDuringCommit
    }

    private func captureBrowserCredentials(attempt: NativeRelayLoginAttempt, completion: @escaping () -> Void) {
        let passwordExpression = rememberPassword
            ? "document.querySelector('input[type=password], input[autocomplete=current-password]')?.value || ''"
            : "''"
        let script = """
        (() => ({
          password: \(passwordExpression),
          accessToken: localStorage.getItem('auth_token') || localStorage.getItem('access_token') || '',
          refreshToken: localStorage.getItem('refresh_token') || ''
        }))();
        """
        webView.evaluateJavaScript(script) { [weak self, weak attempt] value, _ in
            DispatchQueue.main.async {
                guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                if let fields = value as? [String: Any] {
                    if self.rememberPassword {
                        if let password = (fields["password"] as? String).flatMap({ $0.isEmpty ? nil : $0 }) {
                            self.capturedPassword = password
                        } else if let captured = self.passwordCapture.value {
                            self.capturedPassword = captured
                        } else if self.capturedPassword == nil {
                            self.capturedPassword = self.passwordCapture.value
                        }
                    }
                    self.capturedAccessToken = (fields["accessToken"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                    self.capturedRefreshToken = (fields["refreshToken"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                }
                completion()
            }
        }
    }

    private func collectCookies(attempt: NativeRelayLoginAttempt, completion: @escaping (String?) -> Void) {
        let originURL = self.originURL
        webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { [weak self, weak attempt, originURL] cookies in
            DispatchQueue.main.async {
                guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                let originHost = originURL.host?.lowercased() ?? ""
                let accepted = cookies.filter { cookie in
                    let domain = cookie.domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
                    return originHost == domain || originHost.hasSuffix("." + domain)
                }
                completion(HTTPCookie.requestHeaderFields(with: accepted)["Cookie"])
            }
        }
    }

    private func cookies(fromHeader header: String) -> [HTTPCookie] {
        guard let host = originURL.host, !header.isEmpty else { return [] }
        return header.split(separator: ";").compactMap { rawPair in
            let pair = rawPair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard pair.count == 2 else { return nil }
            let name = pair[0].trimmingCharacters(in: .whitespacesAndNewlines)
            guard !name.isEmpty else { return nil }
            var properties: [HTTPCookiePropertyKey: Any] = [
                .name: name,
                .value: String(pair[1]),
                .domain: host,
                .path: "/",
            ]
            if originURL.scheme?.lowercased() == "https" { properties[.secure] = "TRUE" }
            return HTTPCookie(properties: properties)
        }
    }

    private func cookieHeader(after response: HTTPURLResponse, existing: String?) -> String? {
        var values: [String: String] = [:]
        for cookie in cookies(fromHeader: existing ?? "") { values[cookie.name] = cookie.value }
        if let url = response.url {
            let headers = response.allHeaderFields.reduce(into: [String: String]()) { result, entry in
                guard let key = entry.key as? String, let value = entry.value as? String else { return }
                result[key] = value
            }
            for cookie in HTTPCookie.cookies(withResponseHeaderFields: headers, for: url) {
                values[cookie.name] = cookie.value
            }
        }
        guard !values.isEmpty else { return nil }
        return values.keys.sorted().map { "\($0)=\(values[$0] ?? "")" }.joined(separator: "; ")
    }

    private var probes: [Probe] {
        type == "newapi"
            ? [
                Probe(path: "api/user/self", usernamePaths: [["data", "username"], ["data", "email"]]),
                Probe(path: "api/user/auth/refresh", usernamePaths: [["data", "user", "username"], ["data", "user", "email"]]),
              ]
            : [Probe(path: "api/v1/auth/me", usernamePaths: [["data", "email"], ["data", "username"], ["email"], ["username"]])]
    }

    private func probe(index: Int, cookieHeader: String?, attempt: NativeRelayLoginAttempt) {
        guard isCurrentCheck(attempt) else { return }
        guard index < probes.count else {
            finishCheckingFailure(text("No active sign-in was found. Complete login in the page and try again.", "未检测到有效登录。请在页面内完成登录后重试。"), attempt: attempt)
            return
        }
        let probe = probes[index]
        guard let url = relayURL(path: probe.path),
              sameOrigin(url) else {
            self.probe(index: index + 1, cookieHeader: cookieHeader, attempt: attempt)
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = probe.path.hasSuffix("auth/refresh") ? "POST" : "GET"
        request.timeoutInterval = 12
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(originHeader, forHTTPHeaderField: "Origin")
        request.setValue(originHeader, forHTTPHeaderField: "Referer")
        if let cookieHeader, !cookieHeader.isEmpty { request.setValue(cookieHeader, forHTTPHeaderField: "Cookie") }
        let probeAccessToken = capturedAccessToken ?? restoredSession?.accessToken
        if let probeAccessToken, !probeAccessToken.isEmpty {
            request.setValue("Bearer \(probeAccessToken)", forHTTPHeaderField: "Authorization")
        }
        session.dataTask(with: request) { [weak self, weak attempt] data, response, _ in
            guard let self, let attempt, attempt.isActive() else { return }
            guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode),
                  let data, data.count <= 2 * 1024 * 1024,
                  let object = try? JSONSerialization.jsonObject(with: data) else {
                DispatchQueue.main.async { [weak self, weak attempt] in
                    guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                    self.probe(index: index + 1, cookieHeader: cookieHeader, attempt: attempt)
                }
                return
            }
            let detectedUsername = self.firstString(in: object, paths: probe.usernamePaths)
            let detectedAccessToken = self.firstString(in: object, paths: [["data", "access_token"], ["access_token"]])
            let detectedRefreshToken = self.firstString(in: object, paths: [["data", "refresh_token"], ["refresh_token"]])
            let acceptedCookie = self.cookieHeader(after: response, existing: cookieHeader)
            DispatchQueue.main.async { [weak self, weak attempt] in
                guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                let username = detectedUsername ?? self.presetUsername ?? ""
                let accessToken = detectedAccessToken ?? self.capturedAccessToken ?? self.restoredSession?.accessToken
                let refreshToken = detectedRefreshToken ?? self.capturedRefreshToken ?? self.restoredSession?.refreshToken
                guard !username.isEmpty,
                      !(acceptedCookie?.isEmpty ?? true) || !(accessToken?.isEmpty ?? true) else {
                    self.probe(index: index + 1, cookieHeader: cookieHeader, attempt: attempt)
                    return
                }
                self.persistVerifiedLogin(
                    username: username,
                    cookie: acceptedCookie,
                    accessToken: accessToken,
                    refreshToken: refreshToken,
                    attempt: attempt
                )
            }
        }.resume()
    }

    private func persistVerifiedLogin(
        username: String,
        cookie: String?,
        accessToken: String?,
        refreshToken: String?,
        attempt: NativeRelayLoginAttempt
    ) {
        guard isCurrentCheck(attempt) else { return }
        let accountID = self.accountID
        let accountType = self.type
        let accountLabel = self.label
        let origin = self.originURL.absoluteString
        let rememberPassword = self.rememberPassword
        let capturedPassword = self.capturedPassword ?? self.passwordCapture.value
        let session = NativeRelaySession(
            accountType: accountType,
            origin: origin,
            cookie: cookie ?? "",
            accessToken: accessToken ?? "",
            refreshToken: refreshToken ?? ""
        )
        let saveFailure = text("The signed-in session could not be saved securely.", "无法安全保存已登录会话。")
        let attachFailure = text("The signed-in session could not be attached.", "无法关联已登录会话。")
        DispatchQueue.global(qos: .userInitiated).async { [weak self, weak attempt] in
            guard let attempt, attempt.isActive() else { return }
            let passwordToRemember = rememberPassword
                ? capturedPassword ?? NativeRelayCredentialStore.readPassword(accountID: accountID, accountType: accountType, origin: origin)
                : nil
            guard attempt.beginCommit() else { return }
            DispatchQueue.main.async { [weak self, weak attempt] in
                guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                self.cancelButton.isEnabled = false
                self.statusLabel.stringValue = self.text("Saving sign-in…", "正在保存登录…")
            }
            let previousCredentials = NativeRelayCredentialStore.backup(accountID: accountID, includePassword: rememberPassword)
            guard NativeRelayCredentialStore.writeSession(session, accountID: accountID),
                  NativeRelayCredentialStore.writePassword(
                    rememberPassword ? passwordToRemember : nil,
                    accountID: accountID,
                    accountType: accountType,
                    origin: origin
                  ) else {
                NativeRelayCredentialStore.restore(previousCredentials, accountID: accountID)
                DispatchQueue.main.async { [weak self, weak attempt] in
                    guard let self, let attempt else { return }
                    self.finishCheckingFailure(saveFailure, attempt: attempt)
                }
                return
            }
            do {
                let accepted = try CoreIPCBridge.shared.acceptRelayLogin(
                    accountID: accountID,
                    type: accountType,
                    label: accountLabel,
                    origin: origin,
                    username: username,
                    cookie: cookie,
                    accessToken: accessToken,
                    refreshToken: refreshToken
                )
                DispatchQueue.main.async { [weak self, weak attempt] in
                    guard let self, let attempt else { return }
                    self.finish(accepted, session: session, attempt: attempt)
                }
            } catch {
                NativeRelayCredentialStore.restore(previousCredentials, accountID: accountID)
                DispatchQueue.main.async { [weak self, weak attempt] in
                    guard let self, let attempt else { return }
                    self.finishCheckingFailure(attachFailure, attempt: attempt)
                }
            }
        }
    }

    private func sameOrigin(_ url: URL) -> Bool {
        url.scheme?.lowercased() == originURL.scheme?.lowercased()
            && url.host?.lowercased() == originURL.host?.lowercased()
            && effectivePort(url) == effectivePort(originURL)
    }

    private func relayURL(path: String) -> URL? {
        guard var components = URLComponents(url: originURL, resolvingAgainstBaseURL: false) else { return nil }
        let basePath = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.path = "/" + [basePath, path].filter { !$0.isEmpty }.joined(separator: "/")
        components.query = nil
        components.fragment = nil
        return components.url
    }

    private var originHeader: String {
        var components = URLComponents()
        components.scheme = originURL.scheme
        components.host = originURL.host
        components.port = originURL.port
        return components.string ?? originURL.absoluteString
    }

    private func effectivePort(_ url: URL) -> Int {
        url.port ?? (url.scheme?.lowercased() == "https" ? 443 : 80)
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard let url = request.url, sameOrigin(url) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }

    private func firstString(in value: Any, paths: [[String]]) -> String? {
        for path in paths {
            var current: Any = value
            var valid = true
            for key in path {
                guard let map = current as? [String: Any], let next = map[key] else { valid = false; break }
                current = next
            }
            if valid, let result = current as? String {
                let trimmed = result.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty && trimmed.utf8.count <= 32_768 { return trimmed }
            }
        }
        return nil
    }

    private func finish(_ value: CoreIPCBridge.RelayLoginResult, session: NativeRelaySession, attempt: NativeRelayLoginAttempt) {
        guard isCurrentCheck(attempt) else { return }
        attempt.finish()
        activeCheck = nil
        restoredSession = session
        result = value
        checking = false
        finished = true
        clearCapturedCredentials()
        let callback = completion
        completion = nil
        self.session.finishTasksAndInvalidate()
        panel.orderOut(nil)
        panel.close()
        callback?(value)
    }

    @objc private func cancel(_ sender: Any?) {
        guard !finished else { return }
        if let activeCheck, activeCheck.requestCancellation() == .committing {
            dismissWhileCommitting()
            return
        }
        activeCheck = nil
        finished = true
        checking = false
        signInButton.isEnabled = false
        cancelButton.isEnabled = false
        clearCapturedCredentials()
        session.invalidateAndCancel()
        panel.orderOut(nil)
        let callback = completion
        completion = nil
        panel.close()
        callback?(nil)
    }

    func windowWillClose(_ notification: Notification) {
        if activeCheck?.isCommitting() == true {
            panelClosedDuringCommit = true
            return
        }
        cancel(nil)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        true
    }

    private func finishCheckingFailure(_ message: String, attempt: NativeRelayLoginAttempt) {
        guard isCurrentCheck(attempt) else { return }
        attempt.finish()
        activeCheck = nil
        checking = false
        if panelClosedDuringCommit {
            finished = true
            clearCapturedCredentials()
            let callback = completion
            completion = nil
            session.finishTasksAndInvalidate()
            callback?(nil)
            return
        }
        signInButton.isEnabled = true
        cancelButton.isEnabled = true
        statusLabel.stringValue = message
    }

    private func clearCapturedCredentials() {
        capturedPassword = nil
        capturedAccessToken = nil
        capturedRefreshToken = nil
        passwordCapture.reset()
    }

    private func dismissWhileCommitting() {
        panelClosedDuringCommit = true
        signInButton.isEnabled = false
        cancelButton.isEnabled = false
        panel.orderOut(nil)
        panel.close()
    }

    @objc private func reloadBrowser(_ sender: Any?) {
        guard isBrowserFlowLive else { return }
        showBrowserLoading()
        webView.reload()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard isBrowserFlowLive else { return }
        schedulePageReadinessProbe()
        if mode == .logs {
            if !didRestoreSession {
                restoreLocalStorageWhenReady()
            }
            return
        }
        prefillLoginWhenReady()
        if didRestoreSession {
            probeRestoredSession()
        } else {
            restoreLocalStorageWhenReady()
        }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        showBrowserLoading()
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        guard isBrowserFlowLive else { return }
        schedulePageReadinessProbe()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showBrowserFailure()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showBrowserFailure()
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        showBrowserFailure()
    }

    private func schedulePageReadinessProbe() {
        pageReadinessProbe?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.isBrowserFlowLive else { return }
            self.webView.evaluateJavaScript("Boolean(document.body && document.body.children.length > 0 && document.body.innerText.trim().length > 0)") { [weak self] value, _ in
                guard let self, self.isBrowserFlowLive else { return }
                if value as? Bool == true {
                    self.loadingIndicator.stopAnimation(nil)
                    self.loadingOverlay.isHidden = true
                    if self.mode == .logs {
                        if !self.didRestoreSession {
                            self.restoreLocalStorageWhenReady()
                        }
                    } else {
                        self.prefillLoginWhenReady()
                        if self.didRestoreSession {
                            self.probeRestoredSession()
                        } else {
                            self.restoreLocalStorageWhenReady()
                        }
                    }
                } else {
                    self.schedulePageReadinessProbe()
                }
            }
        }
        pageReadinessProbe = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4, execute: work)
    }

    private func showBrowserLoading() {
        pageReadinessProbe?.cancel()
        loadingLabel.stringValue = mode == .logs
            ? text("Loading usage logs…", "正在加载用量日志…")
            : text("Loading sign-in page…", "正在加载登录页面…")
        loadingOverlay.isHidden = false
        loadingIndicator.startAnimation(nil)
    }

    private func showBrowserFailure() {
        pageReadinessProbe?.cancel()
        loadingIndicator.stopAnimation(nil)
        loadingLabel.stringValue = mode == .logs
            ? text("The usage log page could not be loaded.", "用量日志页面加载失败。")
            : text("The sign-in page could not be loaded.", "登录页面加载失败。")
        loadingOverlay.isHidden = false
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url, sameOrigin(url) else {
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

private final class NativeRelayPasswordCapture: NSObject, WKScriptMessageHandler {
    private(set) var value: String?

    func reset() {
        value = nil
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "relayPassword",
              let password = message.body as? String,
              !password.isEmpty,
              password.utf8.count <= 4_096 else { return }
        value = password
    }
}

private struct NativeRelaySession: Codable {
    let accountType: String
    let origin: String
    let cookie: String
    let accessToken: String
    let refreshToken: String
}

private struct NativeRelaySessionProbeResult {
    let username: String
    let cookie: String
    let accessToken: String
    let refreshToken: String
}

private enum NativeRelaySessionProbeOutcome {
    case verified(NativeRelaySessionProbeResult)
    case expired
    case unavailable
}

private enum NativeRelaySessionProbe {
    private struct Probe {
        let path: String
        let method: String
        let usernamePaths: [[String]]
    }

    static func verify(
        type: String,
        originURL: URL,
        presetUsername: String?,
        session: NativeRelaySession
    ) -> NativeRelaySessionProbeOutcome {
        let probes: [Probe] = type == "newapi"
            ? [
                Probe(path: "api/user/self", method: "GET", usernamePaths: [["data", "username"], ["data", "email"]]),
                Probe(path: "api/user/auth/refresh", method: "POST", usernamePaths: [["data", "user", "username"], ["data", "user", "email"]]),
              ]
            : [Probe(path: "api/v1/auth/me", method: "GET", usernamePaths: [["data", "email"], ["data", "username"], ["email"], ["username"]])]
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        let client = URLSession(configuration: configuration, delegate: NativeRelayProbeRedirectGuard(originURL: originURL), delegateQueue: nil)
        defer { client.invalidateAndCancel() }
        var sawAuthenticationRejection = false
        var sawNonAuthenticationFailure = false
        for probe in probes {
            guard let url = relayURL(originURL: originURL, path: probe.path), sameOrigin(url, originURL) else {
                sawNonAuthenticationFailure = true
                continue
            }
            var request = URLRequest(url: url)
            request.httpMethod = probe.method
            request.timeoutInterval = 12
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            let origin = originHeader(originURL)
            request.setValue(origin, forHTTPHeaderField: "Origin")
            request.setValue(origin, forHTTPHeaderField: "Referer")
            if !session.cookie.isEmpty { request.setValue(session.cookie, forHTTPHeaderField: "Cookie") }
            if !session.accessToken.isEmpty { request.setValue("Bearer \(session.accessToken)", forHTTPHeaderField: "Authorization") }
            let semaphore = DispatchSemaphore(value: 0)
            var outcome: (Data?, HTTPURLResponse?)?
            client.dataTask(with: request) { data, response, _ in
                outcome = (data, response as? HTTPURLResponse)
                semaphore.signal()
            }.resume()
            guard semaphore.wait(timeout: .now() + 13) == .success,
                  let outcome,
                  let response = outcome.1 else {
                sawNonAuthenticationFailure = true
                continue
            }
            if response.statusCode == 401 || response.statusCode == 403 {
                sawAuthenticationRejection = true
                continue
            }
            if !(200..<300).contains(response.statusCode) {
                sawNonAuthenticationFailure = true
                continue
            }
            guard let data = outcome.0,
                  data.count <= 2 * 1024 * 1024,
                  let object = try? JSONSerialization.jsonObject(with: data),
                  let username = firstString(object, paths: probe.usernamePaths) ?? presetUsername,
                  !username.isEmpty, username.utf8.count <= 320 else {
                sawNonAuthenticationFailure = true
                continue
            }
            let accessToken = firstString(object, paths: [["data", "access_token"], ["access_token"]]) ?? session.accessToken
            let refreshToken = firstString(object, paths: [["data", "refresh_token"], ["refresh_token"]]) ?? session.refreshToken
            let cookie = cookieHeader(after: response, existing: session.cookie) ?? session.cookie
            guard !cookie.isEmpty || !accessToken.isEmpty else {
                sawNonAuthenticationFailure = true
                continue
            }
            return .verified(NativeRelaySessionProbeResult(username: username, cookie: cookie, accessToken: accessToken, refreshToken: refreshToken))
        }
        return sawAuthenticationRejection && !sawNonAuthenticationFailure ? .expired : .unavailable
    }

    private static func relayURL(originURL: URL, path: String) -> URL? {
        guard var components = URLComponents(url: originURL, resolvingAgainstBaseURL: false) else { return nil }
        let basePath = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.path = "/" + [basePath, path].filter { !$0.isEmpty }.joined(separator: "/")
        components.query = nil
        components.fragment = nil
        return components.url
    }

    private static func sameOrigin(_ left: URL, _ right: URL) -> Bool {
        left.scheme?.lowercased() == right.scheme?.lowercased()
            && left.host?.lowercased() == right.host?.lowercased()
            && (left.port ?? (left.scheme?.lowercased() == "https" ? 443 : 80)) == (right.port ?? (right.scheme?.lowercased() == "https" ? 443 : 80))
    }

    private static func originHeader(_ url: URL) -> String {
        var components = URLComponents()
        components.scheme = url.scheme
        components.host = url.host
        components.port = url.port
        return components.string ?? url.absoluteString
    }

    private static func firstString(_ value: Any, paths: [[String]]) -> String? {
        for path in paths {
            var current: Any = value
            for key in path {
                guard let map = current as? [String: Any], let next = map[key] else { current = NSNull(); break }
                current = next
            }
            if let result = current as? String {
                let trimmed = result.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty && trimmed.utf8.count <= 32_768 { return trimmed }
            }
        }
        return nil
    }

    private static func cookieHeader(after response: HTTPURLResponse, existing: String) -> String? {
        var values = cookieValues(from: existing)
        if let url = response.url {
            let headers = response.allHeaderFields.reduce(into: [String: String]()) { result, entry in
                guard let key = entry.key as? String, let value = entry.value as? String else { return }
                result[key] = value
            }
            for cookie in HTTPCookie.cookies(withResponseHeaderFields: headers, for: url) {
                values[cookie.name] = cookie.value
            }
        }
        guard !values.isEmpty else { return nil }
        return values.keys.sorted().map { "\($0)=\(values[$0] ?? "")" }.joined(separator: "; ")
    }

    private static func cookieValues(from header: String) -> [String: String] {
        header.split(separator: ";").reduce(into: [String: String]()) { values, rawPair in
            let pair = rawPair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            let name = pair.first?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            guard pair.count == 2, !name.isEmpty else { return }
            values[name] = String(pair[1])
        }
    }
}

private final class NativeRelayProbeRedirectGuard: NSObject, URLSessionTaskDelegate {
    private let originURL: URL

    init(originURL: URL) { self.originURL = originURL }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard let url = request.url,
              url.scheme?.lowercased() == originURL.scheme?.lowercased(),
              url.host?.lowercased() == originURL.host?.lowercased(),
              (url.port ?? (url.scheme?.lowercased() == "https" ? 443 : 80)) == (originURL.port ?? (originURL.scheme?.lowercased() == "https" ? 443 : 80)) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }
}

private struct NativeRelayPassword: Codable {
    let accountType: String
    let origin: String
    let password: String
}

private struct NativeRelayCredentialBackup {
    let password: Data?
    let session: Data?
}

private enum NativeRelayCredentialStore {
    private static var servicePrefix: String {
        let bundleIdentifier = Bundle.main.bundleIdentifier?.trimmingCharacters(in: .whitespacesAndNewlines)
        return (bundleIdentifier?.isEmpty == false ? bundleIdentifier! : "menu.litellm.menu") + ".relay"
    }
    private static var passwordService: String { servicePrefix + "-password" }
    private static var sessionService: String { servicePrefix + "-session" }

    static func backup(accountID: String, includePassword: Bool) -> NativeRelayCredentialBackup {
        NativeRelayCredentialBackup(
            password: includePassword ? read(service: passwordService, accountID: accountID) : nil,
            session: read(service: sessionService, accountID: accountID)
        )
    }

    static func restore(_ backup: NativeRelayCredentialBackup, accountID: String) {
        _ = write(backup.password, service: passwordService, accountID: accountID)
        _ = write(backup.session, service: sessionService, accountID: accountID)
    }

    static func readPassword(accountID: String, accountType: String, origin: String) -> String? {
        guard let data = read(service: passwordService, accountID: accountID),
              data.count <= 8 * 1024,
              let value = try? JSONDecoder().decode(NativeRelayPassword.self, from: data),
              value.accountType == accountType,
              value.origin == origin,
              !value.password.isEmpty,
              value.password.utf8.count <= 4_096 else { return nil }
        return value.password
    }

    static func readSession(accountID: String, accountType: String, origin: String) -> NativeRelaySession? {
        guard let data = read(service: sessionService, accountID: accountID),
              data.count <= 96 * 1024,
              let value = try? JSONDecoder().decode(NativeRelaySession.self, from: data),
              value.accountType == accountType,
              value.origin == origin,
              value.cookie.utf8.count <= 32_768,
              value.accessToken.utf8.count <= 32_768,
              value.refreshToken.utf8.count <= 32_768,
              !value.cookie.isEmpty || !value.accessToken.isEmpty else { return nil }
        return value
    }

    private static func read(service: String, accountID: String) -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: accountID,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess else { return nil }
        return item as? Data
    }

    @discardableResult
    static func writePassword(_ password: String?, accountID: String, accountType: String, origin: String) -> Bool {
        guard (password?.utf8.count ?? 0) <= 4_096 else { return false }
        let value = password.map { NativeRelayPassword(accountType: accountType, origin: origin, password: $0) }
        let data = value.flatMap { try? JSONEncoder().encode($0) }
        guard (data?.count ?? 0) <= 8 * 1024 else { return false }
        return write(data, service: passwordService, accountID: accountID)
    }

    @discardableResult
    static func writeSession(_ session: NativeRelaySession, accountID: String) -> Bool {
        guard session.cookie.utf8.count <= 32_768,
              session.accessToken.utf8.count <= 32_768,
              session.refreshToken.utf8.count <= 32_768,
              !session.cookie.isEmpty || !session.accessToken.isEmpty,
              let data = try? JSONEncoder().encode(session),
              data.count <= 96 * 1024 else { return false }
        return write(data, service: sessionService, accountID: accountID)
    }

    static func clear(accountID: String) -> Bool {
        let passwordRemoved = delete(service: passwordService, accountID: accountID)
        let sessionRemoved = delete(service: sessionService, accountID: accountID)
        return passwordRemoved && sessionRemoved
    }

    static func clearPassword(accountID: String) -> Bool {
        delete(service: passwordService, accountID: accountID)
    }

    private static func delete(service: String, accountID: String) -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: accountID,
        ]
        let status = SecItemDelete(query as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }

    private static func write(_ data: Data?, service: String, accountID: String) -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: accountID,
        ]
        guard let data, !data.isEmpty else { return delete(service: service, accountID: accountID) }
        let updateStatus = SecItemUpdate(
            query as CFDictionary,
            [kSecValueData as String: data] as CFDictionary
        )
        if updateStatus == errSecSuccess { return true }
        guard updateStatus == errSecItemNotFound else { return false }
        var attributes = query
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(attributes as CFDictionary, nil) == errSecSuccess
    }
}
