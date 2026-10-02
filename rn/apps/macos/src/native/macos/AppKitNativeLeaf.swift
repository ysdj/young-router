import AppKit
import Foundation
import QuartzCore
import ServiceManagement
import WebKit

private let nativeUIFontSize: CGFloat = 13
// Monospaced glyphs have a larger optical body than the surrounding system
// labels at the same point size. Keep read-only code text visually aligned.
/// The one heading step inside a native window — the window's own title and the
/// section headings under it — and the same step the shared UI draws its window
/// titles with (`windowTitle`: `UI_FONT_SIZE`, weight 600), so a window reads the
/// same whether its heading comes from React or from the native leaf.
private let nativeHeadingFont = NSFont.systemFont(ofSize: nativeUIFontSize, weight: .semibold)
/// The one inset every native child window keeps from its own edges — its
/// heading, its body, and its footer — matching the inset the shared UI gives a
/// route window (`windowContent`: 16), so a child surface reads the same
/// whichever side draws it.
private let nativePanelInset: CGFloat = 16
/// The one decision surface for the whole app, drawn the way a macOS alert is
/// drawn rather than like one of the app's own document windows: a borderless
/// rounded panel that is dragged by its background, no title bar, the question
/// in bold over its detail, and equal-width answers across the bottom.  The
/// numbers are the ones `NSAlert` itself uses — a 220 pt text column (260 pt
/// window) growing to 380 pt (420 pt) for a longer line, 20 pt insets, a 28 pt
/// answer with an 8 pt gap, an 11 pt gap between the question, the detail, and
/// the answers, and the alert's own corner radius.
private let nativeDecisionTextWidth: CGFloat = 220
private let nativeDecisionMaxTextWidth: CGFloat = 380
private let nativeDecisionPanelInset: CGFloat = 20
private let nativeDecisionCornerRadius: CGFloat = 11
private let nativeDecisionGap: CGFloat = 11
private let nativeDecisionAnswerHeight: CGFloat = 28
private let nativeDecisionAnswerGap: CGFloat = 8
private let nativeDecisionMaxQuestionHeight: CGFloat = 44
private let nativeDecisionMaxMessageHeight: CGFloat = 240

private func withoutAnimations(_ changes: () -> Void) {
    NSAnimationContext.runAnimationGroup { context in
        context.duration = 0
        context.allowsImplicitAnimation = false
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        changes()
        CATransaction.commit()
    }
}

// Kept at module scope so every native route, child window, alert, and file panel
// shares the same zero-animation presentation policy.
func configureImmediatePresentation(_ window: NSWindow) {
    window.animationBehavior = .none
}


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
        let image = NSImage(named: NSImage.Name("StatusBarIcon"))!
        image.size = NSSize(width: 20, height: 20)
        image.isTemplate = true
        return image
    }()

    // Keep the menu-bar shell anchored to the pre-RN AppKit app. The strings
    // are stable action IDs (plus the two presentation markers), not labels.
    private static let settingsPaneRoutes: Set<String> = [
        "general-settings", "providers-models", "runtime-settings", "codex-settings", "data-management", "logs",
    ]
    private static let statusMenuOrder = [
        "status", "separator",
        "toggle-autostart", "toggle-codex-model-catalog", "separator",
        "open-general-settings", "open-providers-models", "open-runtime-settings", "open-codex-settings", "separator",
        "webdav-status", "open-data-management", "separator",
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
        "language-picker", "set-language-system", "set-language-en", "set-language-zh-Hans",
    ]
    public static let shared = AppKitNativeLeaf()
    /// The Core capability exchange is asynchronous so AppKit never waits on
    /// the Core endpoint while a file panel is being handled.
    var fileCapabilityRegistrar: ((URL, String, @escaping (String?) -> Void) -> Void)?
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
    private var routeWindowFactory: ((String, String?, String?, NSWindow?) -> NSWindow?)?
    /// The raw file editor is presented as a child window over the workspace,
    /// exactly like the provider wizard. Its window is created once — as soon
    /// as the pane that opens it appears — so the embedded editor boots while
    /// the user is still reading the file list; opening then only presents the
    /// already loaded window. The pending document travels as a small JSON
    /// payload the window reads through ``pendingFileEditorTarget()``.
    private var pendingFileEditorTargetValue: String?
    /// Every child panel on screen with the window it locks, outermost first:
    /// ending one ends the children opened from it, and the lock over a parent
    /// is only released with its last child. See ``presentChildPanel(_:in:prepare:)``.
    private var reactHostStarter: (() -> Void)?
    private var routeWindows: [String: NSWindow] = [:]
    /// The service menu is shown on right-click only; a left click opens the
    /// single settings window directly.
    private var statusMenu: NSMenu?
    private var statusMenuVisible = false
    private var approvedCloseRoutes: Set<String> = []
    /// Every decision panel on screen, keyed by its window: the answer Escape
    /// and the title-bar close button carry, the answers it offers, and the
    /// completion it settles exactly once. See ``presentDecisionPanel(_:)``.
    private var decisionPanels: [ObjectIdentifier: DecisionPanelState] = [:]
    /// The catalog restart question, so a second request settles the one on
    /// screen instead of stacking two identical panels over the app.
    private var codexRestartPanel: NSPanel?
    private var childPanels: [ChildPanel] = []
    /// The model chooser on screen: its window, the controller that answers its
    /// controls, and the completion the pending JS promise waits on. AppKit
    /// keeps a window's delegate and a control's target weak, so the host is
    /// what keeps a chooser alive; without this entry the panel draws and lists
    /// its models, but 全选, 反选, + and 取消 each deliver their action to
    /// nobody — and the window can then only be closed with its title-bar
    /// button, which leaves the parent locked.
    private var openModelChooser: ModelChooser?
    private var groupManagerPanel: NSPanel?
    private var groupManagerCompletionBlock: ((NativeGroupManagerResult?) -> Void)?
    private var groupManagerController: NativeGroupManagerController?
    /// The 保存并关闭 a caller has not answered yet, and the caller waiting for
    /// the next one.  The sheet stays up (and keeps the window it was opened
    /// from locked) while the caller writes and applies its staged edits, and
    /// states the outcome in its own status strip, so a child surface reports
    /// the work it started.
    private var groupManagerPendingApply: NativeGroupManagerResult?
    private var groupManagerApplyWaiter: ((NativeGroupManagerResult?) -> Void)?
    private var activeReadOnlyCodeController: NativeReadOnlyCodeController?
    // Official device-code browser flow. The controller is deliberately
    // separate from relay login: it never installs script message handlers or
    // reads credentials from the provider page.
    // Keep one isolated WebView per account fingerprint. The shared UI may
    // poll several authorizations at once; a provider's account id must not
    // cause one login panel to suppress or replace another account's panel.
    private var activeProviderAuthControllers: [String: NativeProviderAuthController] = [:]
    // Retain the browser flow across the asynchronous React Native promise.
    private var activeRelayLoginController: NativeRelayLoginController?
    // The native shell is visible before React and Core publish their first
    // snapshot. Keep that interval explicit instead of showing the app name as
    // if it were a service state.
    private var statusTitle = "Status: Starting"
    private var statusTitleIsBootstrap = true
    private var statusRunning = false
    private var menuActions: [MenuAction] = []
    private var menuTracking = false
    private var menuNeedsRefresh = false
    private var pendingActions: [String] = []
    private var strings: [String: String] = [
        "appTitle": "Young Router", "autoStart": "Auto Start at Login", "serviceUnavailable": "service unavailable",
        "serviceStatus": "Status: {status}", "serviceStarting": "Starting",
        "cancel": "Cancel", "set": "Set", "clear": "Clear", "stage": "Stage", "find": "Find", "findNext": "Find Next",
        "edit": "Edit", "undo": "Undo", "redo": "Redo", "cut": "Cut", "copy": "Copy",
        "paste": "Paste", "selectAll": "Select All", "settings": "Settings...",
        "reload": "Reload", "closeWindow": "Close Window", "version": "Version",
        "build": "build", "ok": "OK", "invalidText": "The document contains invalid text.",
        "languageMenu": "Language", "languageSystem": "System", "languageEnglish": "English", "languageSimplifiedChinese": "简体中文",
        "menuQuit": "Quit Young Router",
        "routeHome": "Young Router", "routeProvidersModels": "Providers & Models",
        "routeProviderWizard": "Add Provider",
        "providerAuthInstruction": "Complete sign-in on the official provider page. The code below is shown only for this device-code flow.",
        "providerAuthCode": "Device code", "providerAuthCopy": "Copy", "providerAuthBlocked": "This navigation was blocked because it is outside the official provider authentication flow.",
        "routeCodexSettings": "External", "routeClaudeSettings": "Claude Settings",
        "routeRuntimeSettings": "Runtime",
        "routeDataManagement": "Data Management", "routeLogs": "Logs",
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
        statusItem.button?.setAccessibilityLabel(Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Young Router")
        // A left click opens the settings window; the status menu stays on
        // right-click (and Control-click) for service and lifecycle actions.
        statusItem.button?.target = self
        statusItem.button?.action = #selector(statusItemPressed(_:))
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        statusMenu = makeMenu()
    }

    public func setRouteWindowFactory(_ factory: @escaping (String, String?, String?, NSWindow?) -> NSWindow?) {
        routeWindowFactory = factory
    }

    public func setReactHostStarter(_ starter: @escaping () -> Void) {
        reactHostStarter = starter
    }

    func setStatus(title: String, running: Bool) {
        guard title != statusTitle || running != statusRunning else { return }
        statusTitle = title
        statusTitleIsBootstrap = false
        statusRunning = running
        statusItem.length = NSStatusItem.squareLength
        statusItem.button?.title = ""
        statusItem.button?.image = Self.statusBarIcon
        statusItem.button?.toolTip = statusTitle
        statusItem.button?.setAccessibilityLabel(statusTitle)
        if let status = statusMenu?.item(withTag: 1) {
            status.title = statusTitle
            configureStatusMenuItem(status)
        }
    }

    func setLocalization(_ values: [String: String]) {
        for (key, value) in values where !value.isEmpty { strings[key] = value }
        if statusTitleIsBootstrap {
            let template = localized("serviceStatus", fallback: "Status: {status}")
            let starting = localized("serviceStarting", fallback: "Starting")
            statusTitle = template.replacingOccurrences(of: "{status}", with: starting)
            statusItem.button?.toolTip = statusTitle
            statusItem.button?.setAccessibilityLabel(statusTitle)
        }
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

    func open(route: String, title: String, initialLogTab: String? = nil, warmOnly: Bool = false) {
        // The legacy app was menu-bar first. "home" exists only as a routing
        // target for RN, not as a dashboard window.
        guard route != "home" else {
            hideHostWindow()
            // "home" leaves the settings shell for the menu bar. Close the
            // wizard window first, then the shared settings window, so no
            // empty shell window stays onscreen after the route switch.
            if routeWindows["provider-wizard"] != nil {
                close(route: "provider-wizard")
            }
            if let settingsKey = settingsWindowKey() {
                close(route: settingsKey)
            }
            return
        }

        // React owns every settings route so state, validation, and actions
        // stay shared with Windows. Fabric component views below that surface
        // supply AppKit controls, focus behavior, and system appearance.
        let windowRoute = canonicalRoute(route)
        if windowRoute == "file-editor", pendingFileEditorTargetValue == nil {
            // One window edits one registered document. A bare deep link, a
            // restored window, or a stale request has no document to edit, so
            // it must not present an empty editor.
            return
        }
        ensureReactHostStarted()
        if windowRoute == "provider-wizard", settingsWindowKey() == nil,
           let parentTitle = routeWindowTitle("providers-models") {
            // The provider wizard is a child of the provider workspace: its own
            // movable window in front of that workspace, which is locked until
            // the wizard closes.
            open(route: "providers-models", title: parentTitle)
        }
        if Self.settingsPaneRoutes.contains(windowRoute),
           let existingKey = settingsWindowKey(), existingKey != windowRoute,
           let existing = routeWindows[existingKey] {
            // Every settings pane shares one window. Switch the shared shell
            // instead of opening a second settings window.
            if let initialLogTab, windowRoute == "logs" {
                emitAction("open-logs?tab=\(initialLogTab)")
            } else {
                emitAction("open-\(windowRoute)")
            }
            updateActivationPolicy()
            configureImmediatePresentation(existing)
            withoutAnimations { existing.makeKeyAndOrderFront(nil) }
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let window: NSWindow
        if let existing = routeWindows[windowRoute] {
            if let initialLogTab,
               let refreshed = routeWindowFactory?(route, initialLogTab, pendingFileEditorTargetValue, existing) {
                window = refreshed
                routeWindows[windowRoute] = window
                window.delegate = self
            } else {
                window = existing
            }
            window.title = title
        } else if let created = routeWindowFactory?(route, initialLogTab, pendingFileEditorTargetValue, nil) {
            window = created
            routeWindows[windowRoute] = window
            window.delegate = self
            window.isReleasedWhenClosed = false
            configure(window, for: windowRoute, title: title)
        } else {
            return
        }
        updateActivationPolicy()
        if warmOnly {
            // A warm window boots its React root behind the scenes and reaches
            // the screen only when the user opens the document it edits.
            configureImmediatePresentation(window)
            withoutAnimations { window.orderOut(nil) }
        } else if windowRoute == "provider-wizard" || windowRoute == "file-editor" {
            // The wizard and the file editor are children of the workspace they
            // open from: each is its own movable window with the rest of the
            // app locked until it closes, exactly like the model chooser. A
            // window that is already up is only brought forward, never given a
            // second modal session.
            if isChildPanel(window) {
                NSApp.activate(ignoringOtherApps: true)
                configureImmediatePresentation(window)
                withoutAnimations { window.makeKeyAndOrderFront(nil) }
            } else {
                presentChildPanel(window, in: settingsWindow())
            }
        } else {
            configureImmediatePresentation(window)
            withoutAnimations { window.makeKeyAndOrderFront(nil) }
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    func open(route: String) {
        guard let title = routeWindowTitle(route) else { return }
        open(route: route, title: title)
    }

    /// The document the editor window is currently asked to show, as the JSON
    /// payload the shared UI sent. The window reads it through the synchronous
    /// accessor below; the host never parses its contents.
    func pendingFileEditorTarget() -> String {
        pendingFileEditorTargetValue ?? ""
    }

    /// Create the editor window without presenting it, so its embedded editor
    /// boots before the user asks for it. Safe to call repeatedly.
    func prepareFileEditor() {
        guard routeWindows["file-editor"] == nil else { return }
        guard let title = routeWindowTitle("file-editor") else { return }
        let keep = pendingFileEditorTargetValue
        // The window factory only forwards a requested document; a prepared
        // window starts with none and receives the first one on open.
        pendingFileEditorTargetValue = nil
        open(route: "file-editor", title: title, warmOnly: true)
        pendingFileEditorTargetValue = keep
        updateActivationPolicy()
    }

    /// Present the raw file editor for one Core-listed client document in its
    /// own movable window over the workspace. ``payload`` is the shared UI's
    /// JSON description of that document; a warm window reads it directly.
    func openFileEditor(_ payload: String) {
        let trimmed = payload.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.utf8.count <= 8192,
              trimmed.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value != 0x7f }) else { return }
        pendingFileEditorTargetValue = trimmed
        defer { pendingFileEditorTargetValue = nil }
        guard let title = routeWindowTitle("file-editor") else { return }
        if routeWindows["file-editor"] != nil {
            // A warm window already booted its editor: present it and let the
            // mounted root pick up the new document.
            open(route: "file-editor", title: title)
            emitAction("edit-file")
            return
        }
        open(route: "file-editor", title: title)
    }

    func close(route: String? = nil) {
        let selectedRoute = route.map(canonicalRoute)
            ?? NSApp.keyWindow.flatMap(routeForWindow)
        guard let selectedRoute, let window = routeWindows[selectedRoute] else { return }
        approvedCloseRoutes.insert(selectedRoute)
        defer { approvedCloseRoutes.remove(selectedRoute) }
        let restoreProviderModels = selectedRoute == "provider-wizard"
            ? settingsWindow() : nil
        withoutAnimations {
            if isChildPanel(window) {
                // End the child window's own modal session before it closes, so
                // the workspace it was opened from is interactive again.
                endChildPanel(window)
            }
            window.orderOut(nil)
            window.close()
        }
        routeWindows.removeValue(forKey: selectedRoute)
        if let restoreProviderModels {
            restoreProviderModels.makeKeyAndOrderFront(nil)
        }
        updateActivationPolicy()
    }

    func setWindowContentSize(route: String, width: Double, height: Double) -> Bool {
        let minimumContentExtent = canonicalRoute(route) == "data-management" ? 84.0 : 128.0
        let maximumContentExtent = 8_192.0
        guard width.isFinite,
              height.isFinite,
              width >= minimumContentExtent,
              height >= minimumContentExtent,
              width <= maximumContentExtent,
              height <= maximumContentExtent,
              let window = routeWindows[canonicalRoute(route)]
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
            requestClose(route: route, hiding: sender)
            return false
        }
        // 分组管理 discards its draft on Close, so the title-bar close button
        // asks the same question the footer's Close does before it goes away.
        // The window stays up until that answer lands (false), and the answer
        // closes it through ``closeGroupManager(applied:)``.
        if let panel = groupManagerPanel, sender === panel {
            guard let controller = groupManagerController, controller.hasStagedChanges else { return true }
            closeGroupManager(applied: false)
            return false
        }
        return true
    }

    public func windowWillClose(_ notification: Notification) {
        guard let window = notification.object as? NSWindow else { return }
        // A decision panel closed by its own title-bar button answers like
        // Escape: the question is settled, never abandoned.
        if let state = decisionPanels[ObjectIdentifier(window)] {
            finishDecisionPanel(window, answer: state.cancelAnswerID)
            return
        }
        // The group manager's own title-bar close button is one dismissal path
        // beside its footer Close/Apply pair; every one of them settles the
        // pending result, so a closed window never leaves the pane waiting.
        if let panel = groupManagerPanel, window === panel {
            finishGroupManager()
            return
        }
        // A child window AppKit closed itself — its parent going away, or a
        // close that did not come through ``close(route:)`` — releases the lock
        // it held over that parent.
        if isChildPanel(window) {
            endChildPanel(window)
        }
        if let route = routeForWindow(window) {
            routeWindows.removeValue(forKey: route)
            updateActivationPolicy()
        }
    }

    // MARK: - Child panels

    /// One open child surface: the surface, the window it locks, and the lock
    /// view that keeps that window's content from answering clicks and keys.
    private struct ChildPanel {
        let window: NSWindow
        let parent: NSWindow?
    }

    /// One decision panel on screen: the answer Escape and the title-bar close
    /// button carry, and the completion the caller waits on.
    private struct DecisionPanelState {
        let panel: NSPanel
        let cancelAnswerID: String
        let completion: (String) -> Void
    }

    /// One built decision panel: its window, its answer buttons, and the answer
    /// Escape and the title-bar close button carry.
    private struct BuiltDecisionPanel {
        let panel: NativeDecisionPanel
        let buttons: [NativeDecisionAnswerButton]
        let cancelAnswerID: String
    }

    /// One open model chooser, owned by the host for as long as its window is
    /// up so the controls, the window delegate, and the pending completion all
    /// stay answerable. One chooser at a time: a request that lands while a
    /// chooser is up settles that one as cancelled instead of stacking a second
    /// identical window over it.
    private struct ModelChooser {
        let panel: NSPanel
        let controller: NativeModelChooserController
        let completion: ([String]?) -> Void
    }

    /// Present a child surface — the provider wizard, the file editor, 分组管理,
    /// the relay sign-in browser, the relay usage log, the read-only document
    /// viewer, the official provider sign-in, or the model chooser — the way
    /// the app presents every child: its own movable titled window in front of
    /// the app, attached above the window it was opened from, with that
    /// window's content locked until the child closes.
    ///
    /// The lock is ``NativeChildPanelShield`` rather than a modal session. This
    /// app cannot use `NSApp.runModal`: that runs the main run loop in its modal
    /// mode alone, and the React host's frame, timer, event, and promise work
    /// all stall there — a window opened that way paints nothing and every
    /// React surface in the process stops committing and stops answering, while
    /// a native timer heartbeat does not restore it. A sheet keeps the loop
    /// alive but cannot be dragged and shows no title bar. The shield does both
    /// jobs: the child keeps a real movable window, the parent stays locked, and
    /// the app keeps running.
    ///
    /// ``prepare`` runs once the panel is key, so a surface can install its
    /// first responder or load its document.
    func presentChildPanel(_ panel: NSWindow, in parent: NSWindow?, prepare: (() -> Void)? = nil) {
        panel.isReleasedWhenClosed = false
        lockParentWindow(parent, for: panel)
        childPanels.append(ChildPanel(window: panel, parent: parent))
        if let parent {
            // A child window follows the window it was opened from and always
            // stays above it, which is the part of AppKit's own sheet behavior
            // worth keeping here.
            parent.addChildWindow(panel, ordered: .above)
        }
        NSApp.activate(ignoringOtherApps: true)
        configureImmediatePresentation(panel)
        withoutAnimations { panel.makeKeyAndOrderFront(nil) }
        prepare?()
    }

    /// End a child surface: the children it opened end with it, the lock over
    /// its parent is released, and the window comes off screen. Every dismissal
    /// path — the title-bar close button, an in-surface Close or Apply, and a
    /// resolved completion — comes through here.
    func endChildPanel(_ panel: NSWindow) {
        // A question that leaves the screen without an answer — its parent is
        // going away, or AppKit closed it — answers as cancelled, so a decision
        // panel never leaves its caller waiting.  Its own completion re-enters
        // this method with the entry already taken, so the window still leaves
        // the screen and the lock over its parent is still released.
        if let state = decisionPanels[ObjectIdentifier(panel)] {
            finishDecisionPanel(panel, answer: state.cancelAnswerID)
            return
        }
        for descendant in childPanels.filter({ $0.parent === panel }).map({ $0.window }) {
            endChildPanel(descendant)
        }
        guard let entry = childPanels.first(where: { $0.window === panel }) else {
            withoutAnimations { panel.orderOut(nil) }
            return
        }
        childPanels.removeAll { $0.window === panel }
        if let parent = entry.parent {
            parent.removeChildWindow(panel)
            unlockParentWindow(parent)
        }
        withoutAnimations { panel.orderOut(nil) }
    }

    /// Cover the content of ``parent`` while ``panel`` is up: the shield takes
    /// every mouse event, holds the first responder, and keeps the child key, so
    /// no control of the locked window answers a click or a key. Its window
    /// buttons are disabled for the same reason, so the parent cannot be closed
    /// or minimized out from under the child.
    private func lockParentWindow(_ parent: NSWindow?, for panel: NSWindow) {
        guard let parent, let content = parent.contentView else { return }
        if let shield = content.subviews.first(where: { $0 is NativeChildPanelShield }) as? NativeChildPanelShield {
            shield.setAccessibilityLabel(panel.title)
            return
        }
        let shield = NativeChildPanelShield(frame: content.bounds)
        shield.autoresizingMask = [.width, .height]
        shield.setAccessibilityLabel(panel.title)
        shield.onInteraction = { [weak self, weak panel] in
            // The click belongs to the child: keep it key, so pressing the
            // locked parent selects nothing in it and blurs nothing in the
            // child. Only a child that is still open comes forward: a window
            // that already went away must not be brought back by a click on
            // the lock it left behind, and that lock is released as soon as
            // the child ends.
            guard let panel, self?.isChildPanel(panel) == true else { return }
            panel.makeKeyAndOrderFront(nil)
        }
        content.addSubview(shield, positioned: .above, relativeTo: nil)
        parent.makeFirstResponder(shield)
        for type in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            parent.standardWindowButton(type)?.isEnabled = false
        }
    }

    /// Release the lock over ``parent`` once its last child has closed.
    private func unlockParentWindow(_ parent: NSWindow) {
        guard !childPanels.contains(where: { $0.parent === parent }) else { return }
        parent.contentView?.subviews.first(where: { $0 is NativeChildPanelShield })?.removeFromSuperview()
        for type in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            parent.standardWindowButton(type)?.isEnabled = true
        }
    }

    /// True while ``window`` is one of the child panels on screen.
    private func isChildPanel(_ window: NSWindow) -> Bool {
        childPanels.contains { $0.window === window }
    }

    /// A locked window's content keeps its own first responder: a key press
    /// must never reach a control of the window a child surface locks.
    public func windowDidBecomeKey(_ notification: Notification) {
        guard let window = notification.object as? NSWindow,
              let shield = window.contentView?.subviews.first(where: { $0 is NativeChildPanelShield }) else { return }
        window.makeFirstResponder(shield)
    }

    func chooseImportFile(
        purpose: String = "import",
        completion: @escaping (String?) -> Void
    ) {
        let panel = NSOpenPanel()
        configureImmediatePresentation(panel)
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        let finish: (NSApplication.ModalResponse) -> Void = { [weak self, weak panel] response in
            guard response == .OK, let url = panel?.url else {
                completion(nil)
                return
            }
            self?.registerSelection(url, purpose: purpose, completion: completion)
        }
        if let owner = activeWindow() {
            panel.beginSheetModal(for: owner, completionHandler: finish)
        } else {
            panel.begin(completionHandler: finish)
        }
    }

    func chooseExportFile(
        suggestedName: String,
        completion: @escaping (String?) -> Void
    ) {
        let trimmed = suggestedName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              trimmed.utf8.count <= 255,
              !trimmed.contains("/"),
              !trimmed.contains(":") else {
            completion(nil)
            return
        }
        let panel = NSSavePanel()
        configureImmediatePresentation(panel)
        panel.nameFieldStringValue = trimmed
        panel.canCreateDirectories = true
        let finish: (NSApplication.ModalResponse) -> Void = { [weak self, weak panel] response in
            guard response == .OK, let url = panel?.url else {
                completion(nil)
                return
            }
            self?.registerSelection(url, purpose: "export", completion: completion)
        }
        if let owner = activeWindow() {
            panel.beginSheetModal(for: owner, completionHandler: finish)
        } else {
            panel.begin(completionHandler: finish)
        }
    }

    // MARK: - Decision panels

    /// One question the app puts to the user: 删除供应商, 放弃更改, the catalog
    /// restart notice, the version acknowledgement, one secret field.  Every
    /// question this app asks is this panel, so one question is drawn and
    /// answered the same way as every other one.
    ///
    /// It presents itself the way this app presents every subordinate surface —
    /// its own movable titled window over the window that asked, that window
    /// locked until the answer lands, the React host still running — and never
    /// through a modal session; see ``presentChildPanel(_:in:prepare:)`` for why
    /// `NSApp.runModal` is refused.  A question a background event asks
    /// (`locksParent` false: the catalog restart) stays a floating window
    /// instead, because the user is not answering for a window they were using.
    ///
    /// The answer reaches `completion` exactly once, as the answer's `id`.  A
    /// chosen answer, Escape, the title-bar close button, and a parent that goes
    /// away all settle the same entry, so no caller is left waiting.
    @discardableResult
    func presentDecisionPanel(
        _ title: String,
        message: String,
        answers: [NativeDecisionAnswer],
        in parent: NSWindow? = nil,
        locksParent: Bool = true,
        accessory: NSView? = nil,
        accessoryHeight: CGFloat = 0,
        prepare: (() -> Void)? = nil,
        completion: @escaping (String) -> Void
    ) -> NSPanel? {
        guard let built = makeDecisionPanel(
            title: title,
            message: message,
            answers: answers,
            accessory: accessory,
            accessoryHeight: accessoryHeight
        ) else {
            // A question this app cannot draw is answered as cancelled: the
            // caller hears one answer, never silence.
            completion(answers.first(where: { $0.isCancel })?.id ?? "")
            return nil
        }
        presentBuiltDecisionPanel(built, in: parent, locksParent: locksParent, prepare: prepare, completion: completion)
        return built.panel
    }

    /// Builds one decision panel: the question as its window title, the detail in
    /// its body, an optional accessory under the detail, and one button per answer
    /// on the trailing edge with the caller's last answer (the primary) outermost,
    /// which is where a macOS alert draws it too.  Every question this app asks is
    /// built here, so a prompt that carries its own control (the secret field)
    /// still draws the one panel shape.  Nil when the question is not drawable.
    private func makeDecisionPanel(
        title: String,
        message: String,
        answers: [NativeDecisionAnswer],
        accessory: NSView? = nil,
        accessoryHeight: CGFloat = 0
    ) -> BuiltDecisionPanel? {
        guard let cancelAnswer = answers.first(where: { $0.isCancel }),
              !title.isEmpty,
              !answers.isEmpty,
              answers.count <= 3,
              answers.allSatisfy({ !$0.title.isEmpty && $0.title.utf8.count <= 160 }),
              title.utf8.count <= 160,
              message.utf8.count <= 4_096
        else { return nil }

        let inset = nativeDecisionPanelInset
        let questionFont = nativeHeadingFont
        let messageFont = NSFont.systemFont(ofSize: nativeUIFontSize, weight: .regular)
        // One text column for the question, the detail, and the answers: as wide
        // as the longest line they have to draw, between the alert's own minimum
        // and maximum, and wide enough for answers at the alert's own 68 pt
        // floor.
        let answerFloor = nativeDecisionAnswerGap * CGFloat(answers.count - 1) + 68 * CGFloat(answers.count)
        let textWidth = min(
            nativeDecisionMaxTextWidth,
            max(
                nativeDecisionTextWidth,
                answerFloor,
                Self.decisionLineWidth(title, font: questionFont),
                Self.decisionLineWidth(message, font: messageFont)
            )
        )
        let questionHeight = Self.decisionTextHeight(
            title,
            font: questionFont,
            width: textWidth,
            limit: nativeDecisionMaxQuestionHeight
        )
        let messageHeight = message.isEmpty ? 0 : Self.decisionTextHeight(
            message,
            font: messageFont,
            width: textWidth,
            limit: nativeDecisionMaxMessageHeight
        )

        let panel = NativeDecisionPanel(
            contentRect: NSRect(x: 0, y: 0, width: textWidth + inset * 2, height: 200),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        configureImmediatePresentation(panel)
        // The alert shape: no title bar, one rounded panel, moved by dragging its
        // own background.  The question stays the window's title for the window
        // that asked (its lock announces the question) and for a screen reader,
        // even though a borderless panel never draws it.
        panel.title = title
        panel.isReleasedWhenClosed = false
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.hasShadow = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.delegate = self
        panel.onEscape = { [weak self, weak panel] in
            guard let panel else { return }
            self?.finishDecisionPanel(panel, answer: cancelAnswer.id)
        }

        let content = NSView()
        content.wantsLayer = true
        content.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        content.layer?.cornerRadius = nativeDecisionCornerRadius
        content.layer?.masksToBounds = true
        panel.contentView = content

        let questionLabel = NSTextField(wrappingLabelWithString: title)
        questionLabel.font = questionFont
        questionLabel.textColor = .labelColor
        questionLabel.maximumNumberOfLines = 0
        let messageLabel = NSTextField(wrappingLabelWithString: message)
        messageLabel.font = messageFont
        messageLabel.textColor = .labelColor
        messageLabel.maximumNumberOfLines = 0

        var buttons: [NativeDecisionAnswerButton] = []
        for answer in answers {
            let button = NativeDecisionAnswerButton(
                title: answer.title,
                target: self,
                action: #selector(selectDecisionAnswer(_:))
            )
            button.bezelStyle = .rounded
            button.font = NSFont.systemFont(ofSize: nativeUIFontSize)
            // The answer's id travels on its own control, so one action serves
            // every answer of every panel.
            button.answerID = answer.id
            if answer.isDefault {
                button.keyEquivalent = "\r"
                button.keyEquivalentModifierMask = []
            } else if answer.isCancel {
                // Escape answers the cancel answer through its key equivalent as
                // well as through the panel, so a field that consumes Escape (a
                // secret field's editor) cannot swallow the question's way out.
                button.keyEquivalent = "\u{1b}"
            }
            // A destructive answer draws the way an alert draws its own: the
            // ordinary bezel under the system's red ink.  `hasDestructiveAction`
            // and `bezelColor` only recolour a button AppKit owns (an alert's),
            // so the ink is set here — and a destructive answer that carries
            // Return stops drawing the default button's accent fill, which would
            // paint the delete blue.
            if answer.isDestructive {
                button.hasDestructiveAction = true
                button.drawsNeutralWhileDefault = answer.isDefault
                button.attributedTitle = NSAttributedString(
                    string: answer.title,
                    attributes: [
                        .font: NSFont.systemFont(ofSize: nativeUIFontSize),
                        .foregroundColor: NSColor.systemRed
                    ]
                )
            }
            buttons.append(button)
        }

        ([questionLabel, messageLabel] + (accessory.map { [$0] } ?? []) + buttons).forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview($0)
        }
        var constraints: [NSLayoutConstraint] = [
            questionLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: inset),
            questionLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -inset),
            questionLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: inset),
            questionLabel.heightAnchor.constraint(equalToConstant: questionHeight),
            messageLabel.leadingAnchor.constraint(equalTo: questionLabel.leadingAnchor),
            messageLabel.trailingAnchor.constraint(equalTo: questionLabel.trailingAnchor),
            messageLabel.topAnchor.constraint(equalTo: questionLabel.bottomAnchor, constant: messageHeight > 0 ? nativeDecisionGap : 0),
            messageLabel.heightAnchor.constraint(equalToConstant: messageHeight),
        ]
        // An accessory (the secret field) spans the text column under the detail.
        var aboveAnswers = messageLabel.bottomAnchor
        if let accessory {
            constraints.append(accessory.leadingAnchor.constraint(equalTo: questionLabel.leadingAnchor))
            constraints.append(accessory.trailingAnchor.constraint(equalTo: questionLabel.trailingAnchor))
            constraints.append(accessory.topAnchor.constraint(equalTo: messageLabel.bottomAnchor, constant: nativeDecisionGap))
            constraints.append(accessory.heightAnchor.constraint(equalToConstant: max(24, accessoryHeight)))
            aboveAnswers = accessory.bottomAnchor
        }
        // The answers sit across the bottom as one row of equal width, exactly as
        // an alert draws two or three of them: the first answer starts at the
        // text column's leading edge, the last ends at its trailing edge, and
        // every answer takes the same share of the width between them.
        if let first = buttons.first, let last = buttons.last {
            constraints.append(first.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: inset))
            constraints.append(last.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -inset))
            constraints.append(first.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -inset))
            constraints.append(first.heightAnchor.constraint(equalToConstant: nativeDecisionAnswerHeight))
            var placedButton: NSButton?
            for button in buttons {
                constraints.append(button.widthAnchor.constraint(equalTo: first.widthAnchor))
                constraints.append(button.centerYAnchor.constraint(equalTo: first.centerYAnchor))
                constraints.append(button.topAnchor.constraint(greaterThanOrEqualTo: aboveAnswers, constant: nativeDecisionGap))
                if let placedButton {
                    constraints.append(button.leadingAnchor.constraint(equalTo: placedButton.trailingAnchor, constant: nativeDecisionAnswerGap))
                }
                placedButton = button
            }
        }
        NSLayoutConstraint.activate(constraints)
        content.layoutSubtreeIfNeeded()
        panel.setContentSize(NSSize(
            width: textWidth + inset * 2,
            height: max(inset * 2 + nativeDecisionAnswerHeight, content.fittingSize.height)
        ))
        return BuiltDecisionPanel(panel: panel, buttons: buttons, cancelAnswerID: cancelAnswer.id)
    }

    /// The width of the longest line of `text` drawn unwrapped, so a panel only
    /// grows past the alert's own minimum width for a line that needs it.
    private static func decisionLineWidth(_ text: String, font: NSFont) -> CGFloat {
        text.split(separator: "\n", omittingEmptySubsequences: false)
            .map { ceil(($0 as NSString).size(withAttributes: [.font: font]).width) }
            .max() ?? 0
    }

    /// Presents a built decision panel and settles it exactly once, whichever way
    /// it ends.  ``prepare`` runs once the panel is key, so a question that
    /// carries a field can put the caret in it.
    private func presentBuiltDecisionPanel(
        _ built: BuiltDecisionPanel,
        in parent: NSWindow?,
        locksParent: Bool,
        prepare: (() -> Void)?,
        completion: @escaping (String) -> Void
    ) {
        let panel = built.panel
        decisionPanels[ObjectIdentifier(panel)] = DecisionPanelState(
            panel: panel,
            cancelAnswerID: built.cancelAnswerID,
            completion: completion
        )
        let anchor = locksParent ? (parent ?? activeWindow()) : nil
        Self.positionDecisionPanel(panel, over: anchor)
        if let anchor {
            presentChildPanel(panel, in: anchor, prepare: prepare)
        } else {
            panel.isFloatingPanel = true
            panel.hidesOnDeactivate = false
            NSApp.activate(ignoringOtherApps: true)
            withoutAnimations { panel.makeKeyAndOrderFront(nil) }
            prepare?()
        }
    }

    /// Puts a decision panel where a question belongs: centred on the window that
    /// asked it, or on the screen when a background event asks.  `NSWindow.center()`
    /// alone is not enough — a borderless panel added as a child of a window that
    /// is not full screen lands wherever the screen's own centre is relative to
    /// that window, which put a confirmation at the top edge of a maximised
    /// workspace window.  The panel is kept inside the anchor's screen so a
    /// question about a window at the display's edge stays readable.
    private static func positionDecisionPanel(_ panel: NSWindow, over anchor: NSWindow?) {
        guard let screen = anchor?.screen ?? panel.screen ?? NSScreen.main else {
            panel.center()
            return
        }
        let visible = screen.visibleFrame
        let frame = anchor?.frame
        let size = panel.frame.size
        var origin = NSPoint(
            x: (frame?.midX ?? visible.midX) - size.width / 2,
            y: (frame?.midY ?? visible.midY) - size.height / 2
        )
        origin.x = min(max(origin.x, visible.minX + 8), max(visible.minX + 8, visible.maxX - size.width - 8))
        origin.y = min(max(origin.y, visible.minY + 8), max(visible.minY + 8, visible.maxY - size.height - 8))
        withoutAnimations { panel.setFrameOrigin(origin) }
    }

    /// The app's own prompt for one secret: a decision panel with a secure field
    /// in it, so reading or replacing a key is the same surface as answering any
    /// other question — and the React host keeps running while the field is up,
    /// which the alert's modal loop did not allow (see the child-surface modal
    /// freeze incident).  The typed value never leaves this call: "set" carries
    /// it, "clear" carries an empty one, and every other way out answers nil.
    func presentSecretPrompt(
        title: String,
        clearLabel: String?,
        in parent: NSWindow? = nil,
        completion: @escaping (_ answer: String?, _ value: String) -> Void
    ) {
        let input = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        input.maximumNumberOfLines = 1
        input.font = NSFont.systemFont(ofSize: nativeUIFontSize)
        var answers: [NativeDecisionAnswer] = [
            NativeDecisionAnswer(id: "cancel", title: localized("cancel", fallback: "Cancel"), isCancel: true)
        ]
        if let clearLabel, !clearLabel.isEmpty {
            answers.append(NativeDecisionAnswer(id: "clear", title: clearLabel))
        }
        answers.append(NativeDecisionAnswer(id: "set", title: localized("set", fallback: "Set"), isDefault: true))
        guard let built = makeDecisionPanel(
            title: title,
            message: "",
            answers: answers,
            accessory: input,
            accessoryHeight: input.fittingSize.height
        ) else {
            completion(nil, "")
            return
        }
        // 设置 stays disabled until the field carries something, so an empty
        // answer can never be staged.
        let setButton = built.buttons.first(where: { $0.answerID == "set" })
        setButton?.isEnabled = false
        let observer = NotificationCenter.default.addObserver(
            forName: NSControl.textDidChangeNotification,
            object: input,
            queue: .main
        ) { _ in
            setButton?.isEnabled = !input.stringValue.isEmpty
        }
        presentBuiltDecisionPanel(built, in: parent, locksParent: true, prepare: { [weak input] in
            guard let input else { return }
            input.window?.makeFirstResponder(input)
        }) { answer in
            NotificationCenter.default.removeObserver(observer)
            // The typed secret is read once and cleared in the same turn, so it
            // survives only in the staging call that asked for it.
            let value = input.stringValue
            input.stringValue = ""
            let accepted = answer == "set" || answer == "clear"
            completion(accepted ? answer : nil, answer == "set" ? value : "")
        }
    }

    /// The height a decision panel's own text needs at the panel's width, so the
    /// window is as tall as its question and no taller.  A message longer than
    /// the limit clips rather than growing a window past the display.
    private static func decisionTextHeight(_ text: String, font: NSFont, width: CGFloat, limit: CGFloat) -> CGFloat {
        let measured = (text as NSString).boundingRect(
            with: NSSize(width: width, height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: [.font: font]
        ).height
        return min(limit, ceil(measured))
    }

    @objc private func selectDecisionAnswer(_ sender: NSButton) {
        guard let panel = sender.window,
              let answer = (sender as? NativeDecisionAnswerButton)?.answerID,
              !answer.isEmpty else { return }
        finishDecisionPanel(panel, answer: answer)
    }

    /// Settle a decision panel exactly once: the answer reaches its completion,
    /// the window leaves the screen, and the lock it held over the window that
    /// asked is released.  A chosen answer, Escape, the title-bar close button,
    /// and the window that asked going away all come through here.
    private func finishDecisionPanel(_ panel: NSWindow, answer: String) {
        guard let state = decisionPanels.removeValue(forKey: ObjectIdentifier(panel)) else { return }
        if codexRestartPanel === panel { codexRestartPanel = nil }
        endChildPanel(panel)
        state.completion(answer)
    }

    /// The catalog restart question: the same decision panel, asked by Core
    /// rather than by something the user just did, so it floats over the app
    /// without locking the window the user is working in.
    func showCodexRestartConfirmation(
        title: String,
        message: String,
        restartLabel: String,
        laterLabel: String,
        completion: @escaping (String) -> Void
    ) {
        // One question at a time: a second request answers the panel on screen
        // with "later" instead of stacking two identical windows over the app.
        if let previous = codexRestartPanel, decisionPanels[ObjectIdentifier(previous)] != nil {
            finishDecisionPanel(previous, answer: "later")
        }
        codexRestartPanel = presentDecisionPanel(
            title,
            message: message,
            answers: [
                NativeDecisionAnswer(id: "later", title: laterLabel, isCancel: true),
                NativeDecisionAnswer(id: "restart", title: restartLabel, isDefault: true),
            ],
            locksParent: false,
            completion: completion
        )
    }

    /// Native child window of the provider workspace: the station's API keys
    /// beside the group each one belongs to.  It opens in its own movable
    /// window with the workspace locked until it closes, drafts its edits
    /// locally, and returns them as staged actions, so every Core write still
    /// happens in the shared provider window.
    func showGroupManager(
        title: String,
        accountLabel: String,
        accountID: String,
        groups: [[String: String]],
        keys: [[String: String]],
        labels: [String: String],
        autoGrouping: Bool,
        loading: Bool,
        completion: @escaping (NativeGroupManagerResult?) -> Void
    ) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in
                self?.showGroupManager(
                    title: title,
                    accountLabel: accountLabel,
                    accountID: accountID,
                    groups: groups,
                    keys: keys,
                    labels: labels,
                    autoGrouping: autoGrouping,
                    loading: loading,
                    completion: completion
                )
            }
            return
        }
        let options = NativeGroupManagerController.groupOptions(from: groups)
        let rows = NativeGroupManagerController.keyRows(from: keys)
        guard rows.count <= 512, options.count <= 512 else {
            completion(nil)
            return
        }
        let controller = NativeGroupManagerController(
            title: title,
            accountLabel: accountLabel,
            accountID: accountID,
            groups: options,
            rows: rows,
            labels: labels,
            autoGrouping: autoGrouping,
            loading: loading
        )
        groupManagerController = controller
        groupManagerCompletionBlock = completion
        guard let panel = controller.makePanel() else {
            groupManagerController = nil
            groupManagerCompletionBlock = nil
            completion(nil)
            return
        }
        groupManagerPanel = panel
        panel.delegate = self
        // A child surface of the workspace: its own movable window over the
        // workspace, whose content stays locked until 关闭 or 应用并关闭 ends
        // it.  Every dismissal path settles the pending promise through
        // ``finishGroupManager()``.
        presentChildPanel(panel, in: settingsWindow())
    }

    /// Close the group manager and settle its pending result.  `applied` hands
    /// the staged draft back to the workspace (Save and Close); its false (Close,
    /// and the window's title-bar close button) drops the draft and asks first
    /// when edits would be lost.  Every dismissal path comes through here, so the
    /// window, the lock over the workspace, and the pane's pending promise
    /// settle together — a footer Close that only hid the window left the pane
    /// waiting for a result that never came.
    func closeGroupManager(applied: Bool) {
        guard let controller = groupManagerController, groupManagerPanel != nil else { return }
        let finish: () -> Void = { [weak self] in
            controller.markApplied(applied)
            self?.finishGroupManager()
        }
        guard !applied else {
            finish()
            return
        }
        controller.askToDiscardStagedChanges { accepted in
            guard accepted else { return }
            finish()
        }
    }

    /// Settle the group manager's pending result once, whichever way its window
    /// went away: the footer's Close or Apply, the title-bar close button, or
    /// the workspace window going away underneath it.
    private func finishGroupManager() {
        guard let panel = groupManagerPanel else { return }
        groupManagerPanel = nil
        panel.delegate = nil
        let result = groupManagerController?.resultOnEnd()
        groupManagerController = nil
        let completion = groupManagerCompletionBlock
        groupManagerCompletionBlock = nil
        // A caller waiting for the next 保存并关闭 hears the window is gone
        // instead of waiting for a sheet that no longer exists.
        let waiter = groupManagerApplyWaiter
        groupManagerApplyWaiter = nil
        groupManagerPendingApply = nil
        endChildPanel(panel)
        completion?(result)
        waiter?(nil)
    }

    /// The sheet's 保存并关闭, handed to the caller that writes those edits: the
    /// sheet stays up in its saving state until that caller answers through
    /// ``finishGroupManagerApply(status:close:)``, and a request nobody is
    /// waiting for yet is held for the next waiter.
    func handGroupManagerApply() {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in self?.handGroupManagerApply() }
            return
        }
        guard let controller = groupManagerController, groupManagerPanel != nil,
              let staged = controller.stagedResult() else { return }
        controller.setSaving(true)
        guard let waiter = groupManagerApplyWaiter else {
            groupManagerPendingApply = staged
            return
        }
        groupManagerApplyWaiter = nil
        waiter(staged)
    }

    /// The next 保存并关闭 from the open sheet.  A request already waiting is
    /// answered at once; otherwise the caller waits here until the sheet saves
    /// (or ends, which answers nil).
    func awaitGroupManagerApply(completion: @escaping (NativeGroupManagerResult?) -> Void) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in self?.awaitGroupManagerApply(completion: completion) }
            return
        }
        if let staged = groupManagerPendingApply {
            groupManagerPendingApply = nil
            completion(staged)
            return
        }
        groupManagerApplyWaiter = completion
    }

    /// Answer a handed-over save: the sheet states `status` in its own status
    /// strip and closes (the write landed), or keeps its rows and its
    /// 保存并关闭 so the cause can be fixed and tried again.
    func finishGroupManagerApply(status: String, close: Bool) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in
                self?.finishGroupManagerApply(status: status, close: close)
            }
            return
        }
        guard let controller = groupManagerController, groupManagerPanel != nil else { return }
        controller.setResultStatus(status)
        guard close else {
            controller.setSaving(false)
            return
        }
        controller.markApplied(true)
        finishGroupManager()
    }

    /// Applies a later snapshot of the same account to the window that is open
    /// for it, and ends its loading state.  The provider window opens the child
    /// window on the account facts it already holds, so 自动分组's aligned
    /// draft — which costs a station round trip — lands here instead of holding
    /// the window closed.  False when no live window took it: a load that
    /// finishes after Close, or during the window's dismissal, changes nothing.
    ///
    /// The push arrives while the child window is on screen, so it reaches the
    /// rows through the main queue exactly like the model chooser's own
    /// callbacks do; the window is never a modal session.
    @discardableResult
    func updateGroupManager(accountLabel: String, groups: [[String: String]], keys: [[String: String]], autoGrouping: Bool) -> Bool {
        guard Thread.isMainThread else { return false }
        guard let controller = groupManagerController, groupManagerPanel != nil else { return false }
        controller.applyData(
            accountLabel: accountLabel,
            groups: NativeGroupManagerController.groupOptions(from: groups),
            rows: NativeGroupManagerController.keyRows(from: keys),
            autoGrouping: autoGrouping
        )
        return true
    }

    func showReadOnlyText(
        title: String,
        text: String,
        closeTitle: String,
        language: String,
        html: String,
        completion: @escaping () -> Void
    ) {
        activeReadOnlyCodeController?.close()
        let controller = NativeReadOnlyCodeController(
            title: title,
            text: text,
            closeTitle: closeTitle,
            language: language,
            html: html,
            onClose: { [weak self] closedController in
                if self?.activeReadOnlyCodeController === closedController {
                    self?.activeReadOnlyCodeController = nil
                }
                completion()
            }
        )
        activeReadOnlyCodeController = controller
        // One child surface presentation for every surface: the viewer gets its
        // own movable window over the app, with the window behind it locked
        // until this document closes.
        presentChildPanel(controller.panel, in: activeWindow(), prepare: { controller.loadContent() })
    }

    /// Present an official provider login. Claude OAuth uses the system
    /// browser so its first-party Cloudflare challenge and an existing browser
    /// session work normally; device-code providers remain in an isolated
    /// WebView. Core owns the callback and credential exchange in either case.
    func showProviderAuth(
        provider: String,
        fingerprint: String?,
        verificationURL: String,
        userCode: String?,
        callbackURL: String?,
        title: String,
        closeTitle: String,
        completion: @escaping () -> Void
    ) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in
                self?.showProviderAuth(
                    provider: provider,
                    fingerprint: fingerprint,
                    verificationURL: verificationURL,
                    userCode: userCode,
                    callbackURL: callbackURL,
                    title: title,
                    closeTitle: closeTitle,
                    completion: completion
                )
            }
            return
        }
        guard let url = NativeProviderAuthPolicy.url(provider: provider, string: verificationURL),
              (callbackURL == nil || NativeProviderAuthPolicy.callbackURL(provider: provider, string: callbackURL) != nil),
              (userCode == nil || (userCode.map(NativeProviderAuthPolicy.validCode) ?? false)),
              (userCode != nil || callbackURL != nil),
              !title.isEmpty,
              !closeTitle.isEmpty else {
            completion()
            return
        }
        if provider == "claude" {
            NSWorkspace.shared.open(url)
            completion()
            return
        }
        let authFingerprint: String
        if let fingerprint {
            let normalized = fingerprint.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !normalized.isEmpty, normalized.utf8.count <= 256,
                  !normalized.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f }) else {
                completion()
                return
            }
            authFingerprint = normalized
        } else {
            authFingerprint = "\(provider)|\(url.absoluteString)|\(callbackURL ?? "")"
        }
        // Re-presentation for the same account replaces its stale challenge;
        // other account fingerprints keep their own window and stay usable once
        // the newest one closes.
        activeProviderAuthControllers[authFingerprint]?.close()
        let controller = NativeProviderAuthController(
            provider: provider,
            url: url,
            userCode: userCode,
            callbackURL: callbackURL,
            title: title,
            closeTitle: closeTitle,
            instructionText: localized("providerAuthInstruction", fallback: "Complete sign-in on the official provider page. The code below is shown only for this device-code flow."),
            codeLabelText: localized("providerAuthCode", fallback: "Device code"),
            copyLabelText: localized("providerAuthCopy", fallback: "Copy"),
            blockedMessage: localized("providerAuthBlocked", fallback: "This navigation was blocked because it is outside the official provider authentication flow.")
        ) { [weak self] closedController in
            if self?.activeProviderAuthControllers[authFingerprint] === closedController {
                self?.activeProviderAuthControllers.removeValue(forKey: authFingerprint)
            }
            completion()
        }
        activeProviderAuthControllers[authFingerprint] = controller
        // Its own movable window with its parent locked behind it, like every
        // other child surface. A second account's window stacks on top of this
        // one and takes over the locked window until it closes.
        presentChildPanel(controller.panel, in: activeWindow(), prepare: { controller.loadContent() })
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

    /// The point a menu opens at.  X comes from the caller's button rectangle
    /// (its left edge), Y from the pointer: the menu hangs *below the button*
    /// in the content view's own direction, so it never depends on which
    /// vertical space a caller's measurement used and never opens to the
    /// side.  Without a pointer inside the window the rectangle alone decides.
    private func menuAnchorPoint(contentView: NSView, anchor: [String: NSNumber]) -> NSPoint {
        let bounds = contentView.bounds
        let anchorX = min(max(anchor["x"]?.doubleValue ?? 0, 0), max(bounds.maxX - 1, 0))
        let anchorHeight = max(anchor["height"]?.doubleValue ?? 0, 0)
        if let mouse = contentView.window?.mouseLocationOutsideOfEventStream {
            let pointer = contentView.convert(mouse, from: nil)
            if bounds.contains(pointer) {
                // The pointer sits on the button that asked for the menu; one
                // button height below it clears the button's own bounds.
                let top = min(pointer.y + anchorHeight, max(bounds.maxY - 1, 0))
                return NSPoint(x: anchorX, y: top)
            }
        }
        let anchorY = anchor["y"]?.doubleValue ?? 0
        let topDownY = contentView.isFlipped ? anchorY + anchorHeight : anchorY
        return NSPoint(
            x: anchorX,
            y: min(max(topDownY, 0), max(bounds.maxY - 1, 0))
        )
    }

    /// A grouped action menu: groups are section headers and their items are
    /// listed underneath, so a long list of saved models stays navigable in
    /// one menu (never a submenu on the right).  Resolves the chosen item as
    /// the same `{group, item}` object the Windows leaf resolves — a bare
    /// array would reach React as `[0, 1]`, whose `group`/`item` are both
    /// undefined and leave the caller's selection silently unapplied — or nil
    /// when the menu was dismissed.
    func showGroupedActionMenu(title: String, groups: [[String: Any]], anchor: [String: NSNumber]) -> [String: NSNumber]? {
        guard !title.isEmpty, title.utf8.count <= 160,
              !groups.isEmpty, groups.count <= 32,
              let window = activeWindow(), let contentView = window.contentView else { return nil }
        var entries: [(String, [String])] = []
        for group in groups {
            guard let groupTitle = group["title"] as? String, !groupTitle.isEmpty, groupTitle.utf8.count <= 240,
                  let items = group["items"] as? [String], !items.isEmpty, items.count <= 64,
                  items.allSatisfy({ !$0.isEmpty && $0.utf8.count <= 240 }) else { return nil }
            entries.append((groupTitle, items))
        }
        let menu = NSMenu(title: title)
        let target = NativeActionMenuTarget()
        for (groupIndex, entry) in entries.enumerated() {
            if #available(macOS 14.0, *) {
                menu.addItem(.sectionHeader(title: entry.0))
            } else {
                // Older systems have no section-header item: a disabled row
                // still reads as the group's own caption.
                let header = NSMenuItem(title: entry.0, action: nil, keyEquivalent: "")
                header.isEnabled = false
                menu.addItem(header)
            }
            for (itemIndex, itemTitle) in entry.1.enumerated() {
                let item = NSMenuItem(title: itemTitle, action: #selector(NativeActionMenuTarget.select(_:)), keyEquivalent: "")
                item.target = target
                item.tag = groupIndex * 1_000 + itemIndex
                menu.addItem(item)
            }
        }
        _ = menu.popUp(positioning: nil, at: menuAnchorPoint(contentView: contentView, anchor: anchor), in: contentView)
        guard let tag = target.selectedIndex, tag >= 0 else { return nil }
        return ["group": NSNumber(value: tag / 1_000), "item": NSNumber(value: tag % 1_000)]
    }

    func relayLogin(
        accountID: String,
        type: String,
        label: String,
        origin: String,
        language: String,
        username: String?,
        embedded: Bool = false,
        pendingAccount: Bool = false,
        stationID: String? = nil,
        stationName: String? = nil,
        stationType: String? = nil,
        stationOrigin: String? = nil,
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
                    embedded: embedded,
                    pendingAccount: pendingAccount,
                    stationID: stationID,
                    stationName: stationName,
                    stationType: stationType,
                    stationOrigin: stationOrigin,
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
        let embeddedWindow = embedded ? routeWindows["provider-wizard"].flatMap { $0.contentView == nil ? nil : $0 } : nil
        guard !embedded || embeddedWindow != nil else {
            completion(nil)
            return
        }
        // The sign-in browser is a child surface of the workspace it is opened
        // from: its own movable window with the rest of the app locked until
        // the flow finishes.
        let embeddedClose: (() -> Void)? = embedded ? { [weak self] in
            self?.close(route: "provider-wizard")
        } : nil
        let presentationParent = embeddedWindow == nil ? settingsWindow() : nil
        let controller = NativeRelayLoginController(
            accountID: accountID,
            type: type,
            label: label,
            originURL: canonicalOrigin,
            language: language,
            username: username,
            embeddedWindow: embeddedWindow,
            embeddedClose: embeddedClose,
            presentationParent: presentationParent,
            pendingAccount: pendingAccount,
            stationID: stationID,
            stationName: stationName,
            stationType: stationType,
            stationOrigin: stationOrigin
        )
        activeRelayLoginController = controller
        controller.start { [weak self] result in
            self?.activeRelayLoginController = nil
            completion(result)
        }
    }

    func cancelRelayLogin() {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in self?.cancelRelayLogin() }
            return
        }
        activeRelayLoginController?.cancelFromReact()
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
            presentationParent: settingsWindow(),
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
        NativeRelaySessionMemoryStore.clear(accountID: accountID)
        return true
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
        guard let session = NativeRelaySessionMemoryStore.readSession(
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
            NativeRelaySessionMemoryStore.writeSession(refreshedSession, accountID: accountID)
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
        return true
    }

    func chooseModelsToAdd(models: [String], providerName: String, keyName: String, completion: @escaping ([String]?) -> Void) {
        let candidates = models
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !candidates.isEmpty else {
            completion([])
            return
        }

        let contentWidth: CGFloat = 620
        let rowHeight: CGFloat = 28
        let listHeight = min(480, max(220, CGFloat(candidates.count) * rowHeight + 2))
        // One chooser at a time: a second request settles the open one as
        // cancelled, so its promise never hangs and no identical window stacks
        // over the one the user is answering.
        if let open = openModelChooser {
            finishModelChooser(open.panel, selection: nil)
        }
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
        // The model chooser is the reference child surface: its own movable
        // window in front of the app, with the window behind it locked until it
        // ends. `initialFirstResponder` is not guaranteed to win when a panel is
        // presented, so its editor is established once the panel is key.
        //
        // The host owns the chooser for as long as its window is up: the
        // panel's delegate and every control's target are weak AppKit
        // references, so a chooser that the host only builds and presents is
        // collected the moment this method returns, and its 取消, 全选, 反选
        // and + buttons then answer nobody.
        controller.onFinish = { [weak self] selection in
            self?.finishModelChooser(panel, selection: selection)
        }
        openModelChooser = ModelChooser(panel: panel, controller: controller, completion: completion)
        presentChildPanel(panel, in: activeWindow(), prepare: { controller.focusSearchField() })
    }

    /// Settle the open chooser exactly once, whichever way its window went
    /// away: the footer's + carries the selection, while 取消, Esc and the
    /// title-bar close button carry nothing. Ending the child surface here is
    /// what releases the lock over the window the chooser was opened from.
    private func finishModelChooser(_ panel: NSPanel, selection: [String]?) {
        guard let chooser = openModelChooser, chooser.panel === panel else { return }
        openModelChooser = nil
        // This close must not answer as a second dismissal: the window is
        // closed by the host from here on, not by the user.
        panel.delegate = nil
        endChildPanel(panel)
        withoutAnimations { panel.close() }
        chooser.completion(selection)
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
        configureImmediatePresentation(panel)
        panel.title = localized("modelChooserTitle", fallback: "Choose Models to Add")
        panel.minSize = NSSize(width: 520, height: 340)
        panel.isReleasedWhenClosed = false
        panel.delegate = controller
        controller.chooserWindow = panel

        let content = NSView()
        panel.contentView = content
        let titleLabel = NSTextField(labelWithString: localized("modelChooserHeading", fallback: "Choose models to add"))
        titleLabel.font = nativeHeadingFont
        let subtitleLabel = NSTextField(labelWithString: "\(localized("modelChooserProvider", fallback: "Provider")): \(providerName)    \(localized("modelChooserKey", fallback: "Key")): \(keyName)")
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.lineBreakMode = .byTruncatingMiddle
        subtitleLabel.usesSingleLineMode = true
        subtitleLabel.toolTip = subtitleLabel.stringValue
        let searchField = NativeInstantFocusSearchField()
        searchField.placeholderString = localized("modelChooserSearch", fallback: "Search models")
        searchField.sendsSearchStringImmediately = true
        searchField.sendsWholeSearchString = false
        searchField.focusRingType = .none
        searchField.usesSingleLineMode = true
        searchField.cell?.wraps = false
        searchField.cell?.isScrollable = true

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
        resultCountLabel.lineBreakMode = .byTruncatingTail
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
        scroll.modelListView = controller.listView
        controller.listView.frame = NSRect(x: 0, y: 0, width: contentWidth - 36, height: max(listHeight, CGFloat(modelCount) * controller.listView.rowHeight))
        controller.listView.autoresizingMask = [.width]
        controller.listView.updateVisibleRows()

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
            titleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: nativePanelInset),
            titleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            titleLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),
            subtitleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: nativePanelInset),
            subtitleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            subtitleLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 4),
            searchField.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: nativePanelInset),
            searchField.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            searchField.topAnchor.constraint(equalTo: subtitleLabel.bottomAnchor, constant: 12),
            searchField.heightAnchor.constraint(equalToConstant: 28),
            selectionControls.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: nativePanelInset),
            selectionControls.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            selectionControls.topAnchor.constraint(equalTo: searchField.bottomAnchor, constant: 8),
            selectionControls.heightAnchor.constraint(equalToConstant: 28),
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: nativePanelInset),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            scroll.topAnchor.constraint(equalTo: selectionControls.bottomAnchor, constant: 8),
            scroll.bottomAnchor.constraint(equalTo: cancelButton.topAnchor, constant: -16),
            addButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            addButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -nativePanelInset),
            cancelButton.trailingAnchor.constraint(equalTo: addButton.leadingAnchor, constant: -8),
            cancelButton.centerYAnchor.constraint(equalTo: addButton.centerYAnchor),
        ])
        panel.initialFirstResponder = searchField
        panel.center()
        return panel
    }

    private func modelChooserButton(title: String, toolTip: String) -> NSButton {
        // Keep Return-to-add via the default-button key equivalent, but draw
        // the ordinary (non-accent) bezel so the "+" stays a neutral action.
        let button = NeutralDefaultButton(title: title, target: nil, action: nil)
        button.bezelStyle = .rounded
        button.toolTip = toolTip
        button.setAccessibilityLabel(toolTip)
        return button
    }

    func localizedText(_ key: String, fallback: String) -> String {
        localized(key, fallback: fallback)
    }

    public func setShortcuts(_ shortcuts: [String: String]) {
        ensureSystemEditMenu()
        guard let mainMenu = NSApp.mainMenu else { return }
        let applicationMenu: NSMenu
        if let existing = mainMenu.items.first?.submenu {
            applicationMenu = existing
        } else {
            applicationMenu = NSMenu(title: "Young Router")
            let appRoot = NSMenuItem(title: "Young Router", action: nil, keyEquivalent: "")
            appRoot.submenu = applicationMenu
            mainMenu.insertItem(appRoot, at: 0)
        }
        // The storyboard owns the standard application menu. Localize and wire
        // its About and Preferences items in place; appending new ones would
        // duplicate them (About shows the standard panel, Preferences gets the
        // Settings action and ⌘,).
        if let aboutItem = applicationMenu.items.first(where: { $0.action == Selector(("orderFrontStandardAboutPanel:")) }) {
            aboutItem.title = localized("about", fallback: "About Young Router")
        }
        if let preferencesItem = applicationMenu.items.first(where: { $0.action == nil && $0.keyEquivalent == "," }) {
            preferencesItem.title = localized("settings", fallback: "Settings…")
            preferencesItem.action = #selector(openCodex)
            preferencesItem.target = self
            preferencesItem.representedObject = "open-settings"
        }
        guard !applicationMenu.items.contains(where: { $0.representedObject as? String == "native-open-data-management" }) else { return }
        let dataManagementItem = applicationMenu.addItem(
            withTitle: localized("routeDataManagement", fallback: "Data Management"),
            action: #selector(openDataManagement),
            keyEquivalent: "d"
        )
        dataManagementItem.keyEquivalentModifierMask = [.command, .shift]
        dataManagementItem.target = self
        dataManagementItem.representedObject = "native-open-data-management"
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
            let quitItem = applicationMenu.addItem(withTitle: localized("menuQuit", fallback: "Quit Young Router"), action: #selector(quit), keyEquivalent: "q")
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

    func restartCodex() -> Bool {
        guard let applicationURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex") else {
            return false
        }
        NSRunningApplication.runningApplications(withBundleIdentifier: "com.openai.codex").forEach { application in
            application.forceTerminate()
        }
        launchCodex(applicationURL, attemptsRemaining: 20)
        return true
    }

    private func launchCodex(_ applicationURL: URL, attemptsRemaining: Int) {
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.openai.codex")
        guard running.isEmpty || attemptsRemaining == 0 else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
                self?.launchCodex(applicationURL, attemptsRemaining: attemptsRemaining - 1)
            }
            return
        }
        DispatchQueue.main.async {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            NSWorkspace.shared.openApplication(at: applicationURL, configuration: configuration)
        }
    }

    func systemLocale() -> String {
        Locale.preferredLanguages.first ?? Locale.current.identifier
    }

    func showVersion() {
        let info = Bundle.main.infoDictionary ?? [:]
        let version = info["CFBundleShortVersionString"] as? String ?? "?"
        let build = info["CFBundleVersion"] as? String ?? "?"
        // An acknowledgement is a decision panel with one answer: the same
        // window, the same keys, and no modal session holding the React host.
        presentDecisionPanel(
            localized("appTitle", fallback: "Young Router"),
            message: "\(localized("version", fallback: "Version")) \(version) (\(localized("build", fallback: "build")) \(build))",
            answers: [
                NativeDecisionAnswer(
                    id: "ok",
                    title: localized("ok", fallback: "OK"),
                    isDefault: true,
                    isCancel: true
                )
            ]
        ) { _ in }
    }

    /// Version strings for the shared About pane: the app bundle version plus
    /// the LiteLLM version recorded beside the bundled Core runtime.
    func versionInfo() -> [String: String] {
        let info = Bundle.main.infoDictionary ?? [:]
        let version = info["CFBundleShortVersionString"] as? String ?? "?"
        let build = info["CFBundleVersion"] as? String ?? "?"
        let litellmURL = Bundle.main.resourceURL?
            .appendingPathComponent("Core/runtime/LITELLM_VERSION", isDirectory: false)
        let litellm = litellmURL.flatMap { try? String(contentsOf: $0, encoding: .utf8) }
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) } ?? ""
        var result = [
            "app": build.isEmpty ? version : "\(version) (\(build))",
            "litellm": litellm,
        ]
        if let icon = NSApp.applicationIconImage,
           let png = Self.pngData(from: icon, size: 64) {
            result["icon"] = "data:image/png;base64," + png.base64EncodedString()
        }
        return result
    }

    private static func pngData(from image: NSImage, size: CGFloat) -> Data? {
        let target = NSImage(size: NSSize(width: size, height: size))
        target.lockFocus()
        image.draw(in: NSRect(x: 0, y: 0, width: size, height: size))
        target.unlockFocus()
        guard let tiff = target.tiffRepresentation,
              let representation = NSBitmapImageRep(data: tiff) else { return nil }
        return representation.representation(using: .png, properties: [:])
    }

    /// Open an http(s) URL in the user's browser. Non-web schemes are ignored.
    func openExternalURL(_ url: String) {
        guard let parsed = URL(string: url),
              let scheme = parsed.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else { return }
        NSWorkspace.shared.open(parsed)
    }

    /// Reveal one Core-listed configuration file in Finder.
    ///
    /// The settings pane shows files the user may not have created yet, so a
    /// missing target opens its nearest existing parent directory instead of
    /// doing nothing. Only an absolute path is accepted; the path itself comes
    /// from Core's registered client file listing.
    func revealFile(_ path: String) {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("/"),
              trimmed.utf8.count <= 4096,
              !trimmed.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f }) else { return }
        let target = URL(fileURLWithPath: trimmed)
        if FileManager.default.fileExists(atPath: target.path) {
            NSWorkspace.shared.activateFileViewerSelecting([target])
            return
        }
        var directory = target.deletingLastPathComponent()
        while directory.path != "/" && !FileManager.default.fileExists(atPath: directory.path) {
            let parent = directory.deletingLastPathComponent()
            if parent.path == directory.path { break }
            directory = parent
        }
        NSWorkspace.shared.open(directory)
    }

    private func registerSelection(
        _ url: URL?,
        purpose: String,
        completion: @escaping (String?) -> Void
    ) {
        guard let url else {
            completion(nil)
            return
        }
        fileCapabilityRegistrar?(url, purpose, completion) ?? completion(nil)
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
        statusMenu = makeMenu(actions: menuActions)
    }

    /// Left-clicking the status icon opens the settings window. The service
    /// menu is reserved for the secondary click so the icon never behaves like
    /// a navigation menu again.
    @objc private func statusItemPressed(_ sender: NSStatusBarButton) {
        let event = NSApp.currentEvent
        let secondaryClick = event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true
        if secondaryClick {
            showStatusMenu()
            return
        }
        openNamedRoute("providers-models")
    }

    private func showStatusMenu() {
        if statusMenu == nil { statusMenu = makeMenu(actions: menuActions) }
        guard let menu = statusMenu else { return }
        statusMenuVisible = true
        statusItem.menu = menu
        statusItem.button?.performClick(nil)
        // ``menuDidClose`` clears ``statusItem.menu`` so the next left click
        // opens the settings window again.
    }

    public func menuWillOpen(_ menu: NSMenu) {
        menuTracking = true
    }

    public func menuDidClose(_ menu: NSMenu) {
        menuTracking = false
        if statusMenuVisible {
            statusMenuVisible = false
            statusItem.menu = nil
        }
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
        case "open-general-settings": return localized("routeGeneralSettings", fallback: fallback)
        case "open-providers-models": return localized("routeProvidersModels", fallback: fallback)
        case "open-runtime-settings": return localized("routeRuntimeSettings", fallback: fallback)
        case "open-codex-settings": return localized("routeCodexSettings", fallback: fallback)
        case "open-data-management": return localized("routeDataManagement", fallback: fallback)
        case "open-logs", "open-logs?tab=recovery": return fallback
        case "quit": return localized("menuQuit", fallback: fallback)
        default: return fallback
        }
    }

    private func menuFallback(for id: String) -> String? {
        switch id {
        case "toggle-autostart": return localized("autoStart", fallback: "Auto Start at Login")
        case "toggle-codex-model-catalog": return localized("codexModelCatalog", fallback: "Use LiteLLM models in Codex")
        case "open-general-settings": return localized("routeGeneralSettings", fallback: "General")
        case "open-providers-models": return localized("routeProvidersModels", fallback: "Providers & Models")
        case "open-runtime-settings": return localized("routeRuntimeSettings", fallback: "Runtime")
        case "open-codex-settings": return localized("routeCodexSettings", fallback: "External")
        case "open-data-management": return localized("routeDataManagement", fallback: "Data Management")
        case "open-logs", "open-logs?tab=recovery": return localized("routeLogs", fallback: "Logs")
        case "show-version": return localized("version", fallback: "Version")
        case "quit": return localized("menuQuit", fallback: "Quit Young Router")
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
            case "open-settings": item.title = localized("settings", fallback: "Settings...")
            case "native-reload": item.title = localized("reload", fallback: "Reload")
            case "native-close-window": item.title = localized("closeWindow", fallback: "Close Window")
            case "native-quit": item.title = localized("menuQuit", fallback: "Quit Young Router")
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
            .filter { $0.representedObject as? String == "native-language-picker" }
            .forEach(applicationMenu.removeItem)
        let root = NSMenuItem(title: localized("languageMenu", fallback: "Language"), action: nil, keyEquivalent: "")
        root.representedObject = "native-language-picker"
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

    private func requestClose(route: String?, hiding requestedWindow: NSWindow? = nil) {
        guard let route else { return }
        let windowRoute = canonicalRoute(route)
        if let window = requestedWindow ?? routeWindows[windowRoute] {
            // Keep a child window visible while React decides whether a dirty
            // draft may be discarded: its modal session is still running, and
            // ordering it out here would leave the app locked behind a window
            // that is no longer on screen to dismiss.
            if !isChildPanel(window) {
                withoutAnimations {
                    window.orderOut(nil)
                }
            }
            updateActivationPolicy()
        }
        emitAction("request-close-\(route)")
    }

    private func canonicalRoute(_ route: String) -> String {
        // The former Service Provider Management surfaces are integrated into
        // the unified provider workspace; legacy routes land there.
        if route == "claude-settings" { return "codex-settings" }
        if route == "relay-accounts" { return "providers-models" }
        if route == "relay-add" { return "provider-wizard" }
        return route
    }

    private func routeForWindow(_ window: NSWindow) -> String? {
        routeWindows.first(where: { $0.value === window })?.key
    }

    /// Registry key of the shared settings window, whichever pane created it.
    private func settingsWindowKey() -> String? {
        routeWindows.keys.first(where: { Self.settingsPaneRoutes.contains($0) })
    }

    private func settingsWindow() -> NSWindow? {
        settingsWindowKey().flatMap { routeWindows[$0] }
    }

    func activeWindow() -> NSWindow? {
        if let keyWindow = NSApp.keyWindow, routeForWindow(keyWindow) != nil {
            return keyWindow
        }
        return routeWindows.values.first ?? hostWindow
    }

    private func updateActivationPolicy() {
        // A warm window is registered but ordered out: it must not keep the app
        // in the Dock with nothing on screen after the settings window closes.
        // A presented window — including a warm one the user just opened — must.
        let presented = routeWindows.values.contains { $0.isVisible }
        if !presented {
            NSApp.setActivationPolicy(.accessory)
            return
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.applicationIconImage = Self.applicationIcon
    }

    private func configure(_ window: NSWindow, for route: String, title: String) {
        let layout = routeWindowLayout(for: route)
        window.title = title
        // A document editor cannot be resumed without the document it was
        // opened for, so it is never restored by AppKit.
        window.isRestorable = route != "file-editor"
        window.minSize = layout.minSize
        window.maxSize = layout.maxSize ?? Self.unconstrainedWindowSize
        window.setContentSize(layout.contentSize)
        window.center()
        window.collectionBehavior = [.fullScreenPrimary]
        window.level = .normal
        configureImmediatePresentation(window)
    }

    private func hideHostWindow() {
        guard let window = hostWindow else { return }
        withoutAnimations { window.orderOut(nil) }
        updateActivationPolicy()
    }

    private func routeWindowLayout(for route: String) -> RouteWindowLayout {
        if Self.settingsPaneRoutes.contains(route) {
            // One window hosts every settings pane at one fixed size, so
            // switching panes never moves or resizes the window. The size fits
            // the widest workspace (provider three-column, assistant cards)
            // without the former extra width. The window is full-size content
            // view, so its content height includes the transparent title strip.
            return RouteWindowLayout(
                contentSize: NSSize(width: 960, height: 640),
                minSize: NSSize(width: 900, height: 560),
                maxSize: nil
            )
        }
        switch route {
        case "provider-wizard":
            return RouteWindowLayout(
                contentSize: NSSize(width: 620, height: 460),
                minSize: NSSize(width: 540, height: 420),
                maxSize: nil
            )
        case "file-editor":
            // The editor opens over the workspace it belongs to, so its default
            // size fits inside the settings window and leaves it readable
            // around the edges.
            return RouteWindowLayout(
                contentSize: NSSize(width: 900, height: 560),
                minSize: NSSize(width: 720, height: 420),
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
        case "home": return localized("routeHome", fallback: "Young Router")
        case "providers-models": return localized("routeProvidersModels", fallback: "Providers & Models")
        case "provider-wizard": return localized("routeProviderWizard", fallback: "Add Provider")
        case "file-editor": return localized("routeFileEditor", fallback: "Edit File")
        case "codex-settings", "claude-settings": return localized("routeCodexSettings", fallback: "External")
        case "runtime-settings": return localized("routeRuntimeSettings", fallback: "Runtime")
        case "data-management": return localized("routeDataManagement", fallback: "Data Management")
        case "logs": return localized("routeLogs", fallback: "Logs")
        default: return nil
        }
    }

    /// Keep the native window title in sync with the shared route localization.
    /// AppKit owns the title bar, but React owns the language preference.
    private func routeWindowTitle(_ route: String) -> String? {
        // Settings panes share one window, so its title is the app name while
        // the sidebar selection names the active pane (System Settings style).
        if Self.settingsPaneRoutes.contains(canonicalRoute(route)) {
            return localized("appTitle", fallback: "Young Router")
        }
        switch route {
        case "home": return localized("routeHome", fallback: "Young Router")
        case "provider-wizard": return "LiteLLM " + localized("routeProviderWizard", fallback: "Add Provider")
        case "file-editor": return localized("routeFileEditor", fallback: "Edit File")
        default: return nil
        }
    }

    private func isAllowedLogTab(_ tab: String) -> Bool {
        ["requests", "service", "actions", "route-trace", "recovery", "online-usage"].contains(tab)
    }

    private func localized(_ key: String, fallback: String) -> String {
        strings[key].flatMap { $0.isEmpty ? nil : $0 } ?? fallback
    }

    @objc private func openProviders() { openNamedRoute("providers-models") }
    @objc private func openCodex() { openNamedRoute("codex-settings") }
    @objc private func openClaude() { openNamedRoute("claude-settings") }
    @objc private func openRuntime() { openNamedRoute("runtime-settings") }
    @objc private func openDataManagement() { openNamedRoute("data-management") }
    private func openLogs(tab: String?) {
        guard let title = routeWindowTitle("logs") else { return }
        open(route: "logs", title: title, initialLogTab: tab)
        emitAction(tab.map { "open-logs?tab=\($0)" } ?? "open-logs")
    }
    @objc private func openLogs() { openLogs(tab: nil) }
    @objc private func reloadFromShortcut() { emitAction("service-reload") }
    @objc private func closeFromShortcut() {
        let window = NSApp.keyWindow
        requestClose(route: window.flatMap(routeForWindow), hiding: window)
    }
    @objc private func menuAction(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        switch id {
        case "open-providers-models": openProviders()
        case "open-codex-settings": openCodex()
        case "open-claude-settings": openClaude()
        case "open-runtime-settings": openRuntime()
        case "open-data-management": openDataManagement()
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
        activeReadOnlyCodeController?.close()
        activeReadOnlyCodeController = nil
        for controller in activeProviderAuthControllers.values { controller.close() }
        activeProviderAuthControllers.removeAll()
        statusItem.menu = nil
        NSStatusBar.system.removeStatusItem(statusItem)
        for window in routeWindows.values { window.orderOut(nil) }
        hostWindow?.orderOut(nil)
    }

    @objc private func quit() { requestQuit() }
}

/// The one browser identity every request of ours presents.  The literal is
/// mirrored by `src/young_router/browser_identity.py` and asserted by
/// `tests/test_browser_identity.py`: the app never exposes a User-Agent of its
/// own, and a relay that binds a browser session to its IP and User-Agent
/// fingerprint keeps one account on one fingerprint.
enum RelayBrowserIdentity {
    static let userAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15"
    static let acceptLanguage = "zh-CN,zh;q=0.9,en;q=0.8"
}

/// The lock a child surface puts over the window it was opened from.
///
/// The app cannot lock a parent with `NSApp.runModal`: that runs the main run
/// loop in its modal mode alone, and the React host's frame, timer, event, and
/// promise work all stall there, so a modal session freezes every React surface
/// in the process. This view does the locking instead. It covers the parent's
/// content, takes every mouse event, and holds the first responder, so no
/// control of the locked window answers a click or a key while the child
/// surface is up; the child keeps its own movable window, and the app keeps
/// rendering and answering everywhere else.
private final class NativeChildPanelShield: NSView {
    /// Called for every mouse press: the child takes the keyboard back, so a
    /// press on the locked window selects nothing in it.
    var onInteraction: (() -> Void)?

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let superview else { return nil }
        return bounds.contains(convert(point, from: superview)) ? self : nil
    }

    override var acceptsFirstResponder: Bool { true }
    override func mouseDown(with event: NSEvent) { onInteraction?() }
    override func mouseUp(with event: NSEvent) { onInteraction?() }
    override func rightMouseDown(with event: NSEvent) { onInteraction?() }
    override func rightMouseUp(with event: NSEvent) { onInteraction?() }
    override func otherMouseDown(with event: NSEvent) { onInteraction?() }
    override func otherMouseUp(with event: NSEvent) { onInteraction?() }
    override func mouseDragged(with event: NSEvent) { onInteraction?() }
    override func scrollWheel(with event: NSEvent) { onInteraction?() }
    override func keyDown(with event: NSEvent) {}
    override func keyUp(with event: NSEvent) {}
    override func flagsChanged(with event: NSEvent) {}

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .group }
}

/// A push button that keeps the Return-key default-button role without the
/// accent-colored default fill. Assigning ``keyEquivalent`` ``"\\r"`` makes
/// AppKit paint the button with the control accent color; temporarily
/// clearing the equivalent while drawing restores the ordinary bezel while
/// the window still routes Return to this cell.
/// One answer a decision panel offers: its label, the id the caller hears back,
/// whether the Return key carries it, whether Escape and the title-bar close
/// button carry it, and whether it destroys what it names.
struct NativeDecisionAnswer {
    let id: String
    let title: String
    var isDefault = false
    var isCancel = false
    var isDestructive = false
}

/// A decision panel's answer button: it carries the answer's id back to the
/// panel's single action, and it can draw neutrally while still carrying Return.
///
/// A destructive answer that is also the Return default would otherwise be
/// painted with the default button's accent fill — a blue delete.  An alert
/// draws that answer with the ordinary bezel and the system's red ink, so this
/// button drops its key equivalent for the one draw pass and puts it back.
final class NativeDecisionAnswerButton: NSButton {
    var answerID = ""
    var drawsNeutralWhileDefault = false

    override func draw(_ dirtyRect: NSRect) {
        guard drawsNeutralWhileDefault, keyEquivalent == "\r", window?.defaultButtonCell === cell else {
            super.draw(dirtyRect)
            return
        }
        keyEquivalent = ""
        super.draw(dirtyRect)
        keyEquivalent = "\r"
    }
}

/// A decision panel's window.  Escape answers the panel's cancel answer even
/// when no button carries that key, so every question dismisses the same way.
/// A decision panel's window: one rounded borderless alert, dragged by its own
/// background and answering Escape with the question's cancel answer even when
/// no button carries that key.  A borderless panel only becomes key when it says
/// so — without this it would never take the keyboard and Escape would do
/// nothing for every confirmation in the app.
final class NativeDecisionPanel: NSPanel {
    var onEscape: (() -> Void)?

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    override func cancelOperation(_ sender: Any?) {
        onEscape?()
    }
}

private final class NeutralDefaultButton: NSButton {
    override func draw(_ dirtyRect: NSRect) {
        guard keyEquivalent == "\r", window?.defaultButtonCell === cell else {
            super.draw(dirtyRect)
            return
        }
        keyEquivalent = ""
        super.draw(dirtyRect)
        keyEquivalent = "\r"
    }
}


/// Thin separator frame around a native list inside a window, matching the
/// shared table's frame instead of a heavier bezel box.
private final class NativeListFrameView: NSView {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.borderWidth = 1
        layer?.masksToBounds = true
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.backgroundColor = NSColor.clear.cgColor
        layer?.borderColor = NSColor.separatorColor.cgColor
    }
}

/// The 模型列表 view: the selected key's models, one aligned line each, in the
/// detail column.  The window sizes it for the longest list it shows, and the
/// persistent scroller takes over when the list is taller than that.
private final class NativeModelsListView: NSScrollView {
    private let textView = NSTextView(frame: .zero)
    private let emptyText: String
    private var models: [String] = []

    init(font: NSFont, emptyText: String) {
        self.emptyText = emptyText
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        drawsBackground = false
        borderType = .noBorder
        hasVerticalScroller = true
        autohidesScrollers = true
        // Overlay: the scroller floats over the list instead of reserving a
        // gutter, so rows keep the full list width.
        scrollerStyle = .overlay
        textView.isEditable = false
        textView.isSelectable = true
        textView.drawsBackground = false
        textView.isRichText = false
        textView.usesFontPanel = false
        textView.usesFindPanel = false
        textView.font = font
        textView.textColor = .labelColor
        textView.textContainerInset = NSSize(width: 0, height: 0)
        textView.textContainer?.lineFragmentPadding = 0
        textView.textContainer?.widthTracksTextView = true
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        documentView = textView
        usePersistentScrollers(horizontal: false, vertical: true)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    /// One model per line, in the station's order; an empty list states that
    /// the key provides none.
    func setModels(_ names: [String]) {
        models = names
        textView.string = names.isEmpty ? emptyText : names.joined(separator: "\n")
        textView.textColor = names.isEmpty ? .secondaryLabelColor : .labelColor
        refitDocument()
    }

    /// The text view grows with its text so the scroll view scrolls the list
    /// instead of clipping it.
    private func refitDocument() {
        guard let container = textView.textContainer, let layout = textView.layoutManager else { return }
        layout.ensureLayout(for: container)
        let used = layout.usedRect(for: container).height
        textView.frame = NSRect(
            x: 0,
            y: 0,
            width: max(1, contentSize.width),
            height: max(used + textView.textContainerInset.height * 2, contentSize.height)
        )
    }

    override func layout() {
        super.layout()
        usePersistentScrollers(horizontal: false, vertical: true)
        if abs(textView.frame.width - contentSize.width) > 0.5 { refitDocument() }
    }
}

/// Result of the native group manager window: the auto-grouping switch plus the
/// key edits the user staged in the master-detail editor.
struct NativeGroupManagerResult {
    struct Create {
        let name: String
        let groupID: String
    }

    struct Update {
        let keyID: String
        let name: String
        let groupID: String
        let enabled: Bool
    }

    let autoGrouping: Bool
    let creates: [Create]
    let updates: [Update]
    let deletes: [String]
}

/// Column headers inside the group manager window reuse the shared table's
/// header title style so the window list reads like every other native table.
private func groupManagerHeaderTitle(_ title: String) -> NSAttributedString {
    let paragraph = NSMutableParagraphStyle()
    paragraph.lineBreakMode = .byTruncatingTail
    paragraph.firstLineHeadIndent = 8
    paragraph.headIndent = 8
    paragraph.tailIndent = -8
    return NSAttributedString(string: title, attributes: [
        .font: NSFont.systemFont(ofSize: nativeUIFontSize, weight: .medium),
        .foregroundColor: NSColor.labelColor,
        .paragraphStyle: paragraph,
    ])
}

/// The plaintext keys this app already read, keyed by ``account:resource``.
/// The group manager window is rebuilt every time it opens and Core's lease is
/// read-once and process-local, so the host remembers what a window read: the
/// next window shows those keys at once instead of an empty value while its
/// own read runs.  Bounded, and dropped for an account whose keys the station
/// reports differently.
private final class NativeRelayKeyMemo {
    static let shared = NativeRelayKeyMemo()

    private let lock = NSLock()
    private var values: [String: String] = [:]
    private let limit = 1024

    private static func key(_ accountID: String, _ resourceID: String) -> String {
        "\(accountID)\u{1f}\(resourceID)"
    }

    func value(accountID: String, resourceID: String) -> String? {
        lock.lock()
        defer { lock.unlock() }
        return values[Self.key(accountID, resourceID)]
    }

    func remember(accountID: String, resourceID: String, value: String) {
        guard !value.isEmpty else { return }
        lock.lock()
        defer { lock.unlock() }
        values[Self.key(accountID, resourceID)] = value
        while values.count > limit, let oldest = values.keys.first {
            values.removeValue(forKey: oldest)
        }
    }

    /// Drop what the host remembered for one account, so a key the station
    /// reports differently is read again instead of being shown from memory.
    func forget(accountID: String) {
        lock.lock()
        defer { lock.unlock() }
        let prefix = "\(accountID)\u{1f}"
        values = values.filter { !$0.key.hasPrefix(prefix) }
    }
}

/// The group manager window: the pre-refactor master-detail editor.  The left
/// list carries the keys with their group and rate plus the ＋/－ toolbar, the
/// right pane edits the selected key, and the bottom bar applies or discards
/// the staged edits.  Manual edits follow the auto-grouping switch because
/// Core rejects them while automatic grouping owns the layout.
private final class NativeGroupManagerController: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSTextFieldDelegate {
    struct GroupOption {
        let id: String
        /// Picker title: the group name alone.
        let label: String
        /// List column title: the group name alone.
        let name: String
        /// The group's rate, as the shared UI formats it (倍率, or empty).
        let rate: String
    }

    struct KeyRow {
        let id: String
        var name: String
        var groupID: String
        var groupLabel: String
        var multiplier: String
        /// Core's credential-presence sentinel: non-empty means the key exists.
        let hint: String
        /// The models the station reports for the key, in its own order.
        let modelNames: [String]
        let originalName: String
        let originalGroupID: String
        let originalEnabled: Bool
        var enabled: Bool
        var deleted: Bool
        let isDraft: Bool
    }

    /// The picker's group options, built from the native payload: the name
    /// heads the picker with its rate appended, and 倍率 reports the rate alone.
    static func groupOptions(from entries: [[String: String]]) -> [GroupOption] {
        entries.compactMap { entry -> GroupOption? in
            guard let id = entry["id"], let label = entry["label"], !label.isEmpty else { return nil }
            let name = entry["name"].flatMap { $0.isEmpty ? nil : $0 } ?? label
            let rate = entry["rate"] ?? ""
            return GroupOption(id: id, label: label, name: name, rate: rate)
        }
    }

    /// The list's key rows, each drafted against the group the station reports.
    static func keyRows(from entries: [[String: String]]) -> [KeyRow] {
        entries.compactMap { entry -> KeyRow? in
            guard let id = entry["id"], let name = entry["name"] else { return nil }
            let groupID = entry["groupID"] ?? ""
            let enabled = (entry["enabled"] ?? "1") != "0"
            return KeyRow(
                id: id,
                name: name,
                groupID: groupID,
                groupLabel: entry["groupLabel"] ?? groupID,
                multiplier: entry["multiplier"] ?? "",
                hint: entry["hint"] ?? "",
                modelNames: (entry["models"] ?? "").split(separator: "\n").map(String.init),
                originalName: name,
                originalGroupID: groupID,
                originalEnabled: enabled,
                enabled: enabled,
                deleted: false,
                isDraft: false
            )
        }
    }

    private let title: String
    private var accountLabel: String
    /// The account that owns the keys; the copy action names it as the secret target.
    private let accountID: String
    private var groups: [GroupOption]
    private var rows: [KeyRow]
    private let labels: [String: String]
    /// The switch's value when the window's rows last arrived, so the footer's
    /// staged-change test compares the live switch against the loaded account.
    private var initialAutoGrouping: Bool
    /// The window's rows are still loading: 自动分组's aligned draft costs a
    /// station round trip, and the window opens before that answer arrives.  The
    /// rows it opened on stay read-only behind the wheel beside 密钥 until the
    /// update lands, so an edit can never be staged against data the load is
    /// about to replace.
    private var loading: Bool
    private var saving = false
    /// The last outcome this window stated (a finished save or its failure):
    /// the strip keeps it until a later load or save replaces it.
    private var resultStatus: String?
    private var syncingSelection = false
    private var applied = false
    /// A transient word (a failed copy) that takes the footer's status line
    /// until it clears itself; a later save result replaces it.
    private var transientStatus: String?
    private var transientStatusToken = 0
    ///
    /// Plaintext keys the window already revealed while it is open, keyed by
    /// key id.  Core's lease is read-once, so a re-selection reuses what the
    /// window already read instead of asking for another one, and the values
    /// are dropped with the controller when the window ends.
    private var revealedKeys: [String: String] = [:]
    /// The rows still waiting for their key, in the order the sheet reads them.
    private var revealQueue: [String] = []
    /// The queued rows a read is already running for, so a pass never asks for
    /// the same key twice.
    private var revealInFlight: Set<String> = []
    private var revealPassRunning = false

    private weak var panel: NSPanel?
    private weak var table: NSTableView?
    private weak var accountField: NSTextField?
    private weak var addButton: NSButton?
    private weak var removeButton: NSButton?
    private weak var enabledCheckbox: NSButton?
    private weak var nameField: NSTextField?
    private weak var groupPopUp: NSPopUpButton?
    private weak var multiplierField: NSTextField?
    private weak var valueField: NSTextField?
    private weak var modelsList: NativeModelsListView?
    private weak var copyButton: NSButton?

    private weak var toggle: NSButton?
    private weak var closeButton: NSButton?
    /// The window's own result line, beside its footer buttons: a child
    /// surface states its own save and its own failure there, and the window
    /// it was opened from never reports them.
    private weak var footerStatusField: NSTextField?
    /// The list header's busy wheel: the shared frames a working button draws,
    /// turning beside 密钥 while the station round trip is in flight.  The wheel
    /// keeps its box whether it turns or not, so the header never shifts.
    private weak var loadingSpinner: NSImageView?
    /// The wheel's timer and step: the frames are drawn once, so turning the
    /// wheel only swaps images and never re-lays the header out.
    private var loadingTimer: Timer?
    private var loadingStep = 0
    /// The 模型列表 grid's height, which a later snapshot can grow.
    private var modelsListHeight: NSLayoutConstraint?

    /// Save and Close stays disabled until the draft would change the account:
    /// the auto-grouping switch, a staged create, update, or delete.
    private var applyButton: NSButton?
    private var stagedDeleteCount = 0

    private var autoGroupingOn: Bool {
        (toggle?.state ?? (initialAutoGrouping ? .on : .off)) == .on
    }

    /// True while the draft would change the account: the auto-grouping switch,
    /// a staged create, update, or delete.  The leaf reads it to decide whether
    /// a Close has to ask before it drops the draft.
    var hasStagedChanges: Bool {
        guard autoGroupingOn == initialAutoGrouping else { return true }
        if stagedDeleteCount > 0 { return true }
        return rows.contains { row in
            if row.isDraft { return !row.name.isEmpty }
            return row.name != row.originalName
                || row.groupID != row.originalGroupID
                || row.enabled != row.originalEnabled
        }
    }

    /// Whether the window is closing to hand its draft back to the workspace
    /// (Save and Close) instead of dropping it (Close).  The leaf sets it as the
    /// window ends, so ``resultOnEnd()`` reports the draft exactly once.
    func markApplied(_ value: Bool) {
        applied = value
    }

    /// Ask before a Close that would drop staged edits, through the app's own
    /// decision panel: the question is drawn and answered like every other
    /// confirmation, and its copy stays the sheet's own.
    func askToDiscardStagedChanges(completion: @escaping (Bool) -> Void) {
        guard hasStagedChanges else {
            completion(true)
            return
        }
        AppKitNativeLeaf.shared.presentDecisionPanel(
            label("discardTitle"),
            message: label("discardBody"),
            answers: [
                NativeDecisionAnswer(
                    id: "cancel",
                    title: AppKitNativeLeaf.shared.localizedText("cancel", fallback: "Cancel"),
                    isCancel: true
                ),
                NativeDecisionAnswer(
                    id: "discard",
                    title: label("discardConfirm"),
                    isDefault: true,
                    isDestructive: true
                ),
            ],
            in: panel
        ) { answer in
            completion(answer == "discard")
        }
    }

    /// Group names the account still offers, keyed by group id, so the list's
    /// group column names the group while its 倍率 column carries the rate.
    private var currentGroupNames: [String: String]

    /// The 倍率 column reports the rate the row costs right now, on the same
    /// terms whether or not 自动分组 owns the layout; 倍率 carries a rate and
    /// nothing else, so a staged create or delete dims the row instead.
    private func presentation(for row: KeyRow) -> String {
        multiplierText(for: row)
    }

    /// A row the draft will create or delete; the list shows it dimmed.
    private func isStaged(_ row: KeyRow) -> Bool {
        row.deleted || row.isDraft
    }

    /// 未分组 when the key has no group the store still offers; otherwise the
    /// group's current name, falling back to the label a station sent.
    private func currentGroupText(for row: KeyRow) -> String {
        if row.groupID.isEmpty { return label("ungroupedLabel") }
        if let name = currentGroupNames[row.groupID] { return name }
        return row.groupLabel.isEmpty ? label("ungroupedLabel") : row.groupLabel
    }

    /// The key's rate while it belongs to a group the store still offers.  An
    /// ungrouped key has no rate to report, and the group column already names
    /// it, so the cell stays empty instead of repeating 未分组.
    private func multiplierText(for row: KeyRow) -> String {
        guard !row.groupID.isEmpty, currentGroupNames[row.groupID] != nil else { return "" }
        return row.multiplier
    }

    init(title: String, accountLabel: String, accountID: String, groups: [GroupOption], rows: [KeyRow], labels: [String: String], autoGrouping: Bool, loading: Bool) {
        self.title = title
        self.accountLabel = accountLabel
        self.accountID = accountID
        self.groups = groups
        self.rows = rows
        self.labels = labels
        self.initialAutoGrouping = autoGrouping
        self.loading = loading
        self.currentGroupNames = NativeGroupManagerController.groupNames(groups)
        // A key this app already read is on screen when the window opens: the
        // remembered value is what the row shows until the sheet reads the
        // current one, so the value row is never empty for a known key.
        for row in rows where !row.isDraft && !row.hint.isEmpty {
            if let value = NativeRelayKeyMemo.shared.value(accountID: accountID, resourceID: row.id) {
                revealedKeys[row.id] = value
            }
        }
    }

    /// Group names keyed by group id, for the column that names a row's group.
    private static func groupNames(_ groups: [GroupOption]) -> [String: String] {
        var names: [String: String] = [:]
        for group in groups where !group.id.isEmpty { names[group.id] = group.name }
        return names
    }

    /// The 模型列表 grid's row count: room for the longest key's list, at least
    /// the three rows a short list reads in, at most the twelve it shows a
    /// scroll for.
    private static func modelGridRows(_ rows: [KeyRow]) -> Int {
        max(3, min(12, rows.map { $0.modelNames.count }.max() ?? 0))
    }

    private func label(_ key: String, _ fallback: String = "") -> String {
        let value = labels[key] ?? ""
        return value.isEmpty ? fallback : value
    }

    /// The name-only group label a list row falls back to (the picker title in
    /// `groupLabel(_:)` above is the same group with its rate appended).
    private func groupLabel(for groupID: String) -> String {
        groups.first(where: { $0.id == groupID })?.name ?? groupID
    }

    /// The group's rate, so a re-grouped key reports the rate it now costs.
    private func groupRate(for groupID: String) -> String {
        groups.first(where: { $0.id == groupID })?.rate ?? ""
    }

    private func selectedGroupID(from popUp: NSPopUpButton?) -> String? {
        guard let popUp, popUp.indexOfSelectedItem >= 0, popUp.indexOfSelectedItem < groups.count else { return nil }
        return groups[popUp.indexOfSelectedItem].id
    }

    private var editingEnabled: Bool {
        !loading && !saving && toggle?.state == .off
    }

    func makePanel() -> NSPanel? {
        let rowHeight: CGFloat = 22
        let headerHeight: CGFloat = 24
        let listHeight = min(360, max(120, CGFloat(rows.count + 1) * rowHeight + 2 + headerHeight))
        // The window's two columns: the key list keeps its width and the detail
        // column states the key's facts at half of it, so the sheet carries no
        // empty right half and the key list and its detail read at one size.
        let listWidth: CGFloat = 344
        let detailWidth: CGFloat = 189
        // The margins and the gap between the columns, so the window fits its
        // two columns exactly.
        let contentWidth = nativePanelInset + listWidth + 18 + detailWidth + nativePanelInset
        // The same child-surface chrome the model chooser uses: a titled window
        // of its own with the platform close button, over the workspace it
        // belongs to.
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: contentWidth, height: 172 + listHeight),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        configureImmediatePresentation(panel)
        panel.title = title
        panel.isReleasedWhenClosed = false
        // The sheet states its columns and then sits in the middle of the
        // screen like every other child surface; a panel left at its creation
        // origin opens against the bottom-left corner of the display.
        panel.center()
        self.panel = panel

        let content = NSView()
        panel.contentView = content
        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = nativeHeadingFont
        let accountField = NSTextField(labelWithString: accountLabel)
        self.accountField = accountField
        accountField.font = NSFont.systemFont(ofSize: nativeUIFontSize)
        accountField.textColor = .secondaryLabelColor
        accountField.lineBreakMode = .byTruncatingMiddle
        accountField.maximumNumberOfLines = 1

        // Left column: the key list with the ＋ / － toolbar.
        let listTitle = NSTextField(labelWithString: label("listLabel"))
        listTitle.font = nativeHeadingFont
        // 密钥's own header declares the load: the key list is what the station
        // round trip is about to replace, so the wheel turns beside the title
        // those rows belong to, and the footer keeps only the switch and the
        // buttons.  It is the button's own busy wheel - the same shared frames,
        // swapped in place - never a progress-indicator view, and the words for
        // the wait ride it as its tooltip.
        let loadingSpinner = NSImageView()
        loadingSpinner.imageScaling = .scaleNone
        loadingSpinner.contentTintColor = .secondaryLabelColor
        loadingSpinner.toolTip = label("loadingLabel")
        loadingSpinner.setAccessibilityLabel(label("loadingLabel"))
        loadingSpinner.isHidden = true
        self.loadingSpinner = loadingSpinner
        // ± ride the list header as the app's compact icon buttons do: one
        // square small control each, four points apart.
        let removeButton = NSButton(title: "", target: self, action: #selector(removeSelectedKey(_:)))
        removeButton.bezelStyle = .rounded
        removeButton.controlSize = .small
        removeButton.image = NSImage(named: NSImage.removeTemplateName)
        removeButton.imagePosition = .imageOnly
        removeButton.toolTip = label("removeLabel")
        removeButton.setAccessibilityLabel(label("removeLabel"))
        self.removeButton = removeButton
        let addButton = NSButton(title: "", target: self, action: #selector(addDraftKey(_:)))
        addButton.bezelStyle = .rounded
        addButton.controlSize = .small
        addButton.image = NSImage(named: NSImage.addTemplateName)
        addButton.imagePosition = .imageOnly
        addButton.toolTip = label("addLabel")
        addButton.setAccessibilityLabel(label("addLabel"))
        self.addButton = addButton

        // The key list is the shared striped native table.
        let listFrame = NativeListFrameView()
        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true
        // Overlay: the shared capsule floats over the key list instead of
        // reserving a legacy gutter.
        scrollView.scrollerStyle = .overlay
        scrollView.borderType = .noBorder
        scrollView.drawsBackground = false
        scrollView.usePersistentScrollers(horizontal: false, vertical: true)
        let nameColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("key-name"))
        nameColumn.width = 160
        nameColumn.minWidth = 120
        nameColumn.title = label("nameLabel")
        nameColumn.headerCell.attributedStringValue = groupManagerHeaderTitle(nameColumn.title)
        nameColumn.headerCell.isBordered = false
        nameColumn.headerCell.isBezeled = false
        let groupColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("key-group"))
        groupColumn.width = 110
        groupColumn.minWidth = 110
        groupColumn.maxWidth = 110
        groupColumn.title = label("groupLabel")
        groupColumn.headerCell.attributedStringValue = groupManagerHeaderTitle(groupColumn.title)
        groupColumn.headerCell.isBordered = false
        groupColumn.headerCell.isBezeled = false
        let multiplierColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("key-multiplier"))
        multiplierColumn.width = 74
        multiplierColumn.minWidth = 74
        multiplierColumn.maxWidth = 74
        multiplierColumn.title = label("multiplierLabel")
        multiplierColumn.headerCell.attributedStringValue = groupManagerHeaderTitle(multiplierColumn.title)
        multiplierColumn.headerCell.isBordered = false
        multiplierColumn.headerCell.isBezeled = false
        let table = NSTableView()
        table.style = .plain
        // The window list labels its columns like every other native table so
        // 名称 / 分组 / 倍率 stay readable while 自动分组 disables the detail pane.
        table.headerView = NSTableHeaderView(frame: NSRect(x: 0, y: 0, width: 0, height: headerHeight))
        table.addTableColumn(nameColumn)
        table.addTableColumn(groupColumn)
        table.addTableColumn(multiplierColumn)
        table.rowHeight = rowHeight
        table.intercellSpacing = .zero
        table.gridStyleMask = []
        table.usesAlternatingRowBackgroundColors = true
        table.allowsColumnReordering = false
        table.allowsMultipleSelection = false
        table.allowsEmptySelection = true
        table.focusRingType = .none
        table.dataSource = self
        table.delegate = self
        self.table = table
        scrollView.documentView = table
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        listFrame.addSubview(scrollView)

        // Right column: the selected key reports one fact per row, so the
        // detail pane is never a half-filled form: the group's rate, the
        // station's key with its copy action, and the models the key provides.
        let detailFont = NSFont.systemFont(ofSize: nativeUIFontSize)
        let detailCaptions = [label("nameLabel"), label("groupLabel"), label("multiplierLabel"), label("valueLabel")]
        let detailCaptionWidth = max(44, detailCaptions.map {
            ($0 as NSString).size(withAttributes: [.font: detailFont]).width.rounded(.up) + 2
        }.max() ?? 44)
        func detailCaption(_ text: String) -> NSTextField {
            let field = NSTextField(labelWithString: text)
            field.font = detailFont
            field.textColor = .secondaryLabelColor
            field.lineBreakMode = .byTruncatingTail
            field.maximumNumberOfLines = 1
            return field
        }
        func detailValue() -> NSTextField {
            let field = NSTextField(labelWithString: "")
            field.font = detailFont
            field.textColor = .labelColor
            field.lineBreakMode = .byTruncatingMiddle
            field.maximumNumberOfLines = 1
            return field
        }
        let enabledCheckbox = NSButton(checkboxWithTitle: label("enabledLabel"), target: self, action: #selector(toggleSelectedEnabled(_:)))
        enabledCheckbox.font = detailFont
        self.enabledCheckbox = enabledCheckbox
        let nameLabel = detailCaption(label("nameLabel"))
        let nameField = NSTextField(string: "")
        nameField.font = detailFont
        nameField.delegate = self
        nameField.target = self
        nameField.action = #selector(commitNameField(_:))
        nameField.setAccessibilityLabel(label("nameLabel"))
        self.nameField = nameField
        let groupFieldLabel = detailCaption(label("groupLabel"))
        let groupPopUp = NSPopUpButton(frame: .zero, pullsDown: false)
        groupPopUp.font = detailFont
        groupPopUp.addItems(withTitles: groups.map { $0.label })
        groupPopUp.target = self
        groupPopUp.action = #selector(changeSelectedGroup(_:))
        groupPopUp.setAccessibilityLabel(label("groupLabel"))
        self.groupPopUp = groupPopUp
        let multiplierLabel = detailCaption(label("multiplierLabel"))
        let multiplierField = detailValue()
        multiplierField.setAccessibilityLabel(label("multiplierLabel"))
        self.multiplierField = multiplierField
        let valueLabel = detailCaption(label("valueLabel"))
        let valueField = detailValue()
        // 密钥值 stays one line: a station key is one long token, so it shows
        // its head, an ellipsis, and its tail instead of reflowing the rows
        // below it.  The field still holds the whole value, so selecting it or
        // pressing the copy button beside it hands over the key itself.
        valueField.lineBreakMode = .byTruncatingMiddle
        valueField.maximumNumberOfLines = 1
        valueField.isSelectable = true
        valueField.setAccessibilityLabel(label("valueLabel"))
        self.valueField = valueField
        // The copy is an icon button beside the value, the way every icon
        // action in the app reads: the words ride it as its tooltip and its
        // accessibility label instead of taking a text button's width from
        // the key and the models beside it.
        let copyButton = NSButton(title: "", target: self, action: #selector(copySelectedKey(_:)))
        copyButton.bezelStyle = .rounded
        copyButton.font = detailFont
        copyButton.controlSize = .small
        copyButton.image = NSImage(systemSymbolName: "doc.on.doc", accessibilityDescription: label("copyLabel"))
        copyButton.imagePosition = .imageOnly
        copyButton.toolTip = label("copyLabel")
        copyButton.setAccessibilityLabel(label("copyLabel"))
        self.copyButton = copyButton
        // The copy action keeps its own icon-sized slice of the row, so the
        // key (and the models) always wrap beside it instead of squeezing it
        // to nothing.
        copyButton.setContentCompressionResistancePriority(.required, for: .horizontal)
        copyButton.setContentHuggingPriority(.required, for: .horizontal)
        for field in [valueField] {
            field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            field.setContentHuggingPriority(.defaultLow, for: .horizontal)
        }
        // 模型列表 sits in the detail column under the key's own facts: one
        // aligned line per model, sized for the longest list any key carries so
        // switching groups never resizes the window.
        let modelsTitle = NSTextField(labelWithString: label("modelsLabel"))
        modelsTitle.font = nativeHeadingFont
        let modelsList = NativeModelsListView(font: detailFont, emptyText: label("emptyLabel"))
        self.modelsList = modelsList
        let modelsHeight = modelsList.heightAnchor.constraint(equalToConstant: CGFloat(NativeGroupManagerController.modelGridRows(rows)) * 17)
        self.modelsListHeight = modelsHeight

        let toggle = NSButton(checkboxWithTitle: label("autoGroupingLabel"), target: self, action: #selector(toggleAutoGrouping(_:)))
        toggle.font = NSFont.systemFont(ofSize: nativeUIFontSize)
        toggle.state = initialAutoGrouping ? .on : .off
        self.toggle = toggle
        let closeButton = NSButton(title: label("closeLabel"), target: self, action: #selector(closePanel(_:)))
        closeButton.bezelStyle = .rounded
        closeButton.font = NSFont.systemFont(ofSize: nativeUIFontSize)
        // Escape is the dismissive answer in every child window the app opens, and
        // Close asks before it drops a draft, so the key lands on the same question
        // the button asks.
        closeButton.keyEquivalent = "\u{1b}"
        let applyButton = NSButton(title: label("applyLabel"), target: self, action: #selector(applyPanel(_:)))
        applyButton.bezelStyle = .rounded
        applyButton.font = NSFont.systemFont(ofSize: nativeUIFontSize)
        applyButton.keyEquivalent = "\r"
        applyButton.keyEquivalentModifierMask = []
        applyButton.isEnabled = false
        self.applyButton = applyButton

        // The window's own result line sits beside its footer buttons: a child
        // states its own load, its own save, and its own failure there, and the
        // buttons keep their trailing edge, so a message never moves them.
        let footerStatus = NSTextField(labelWithString: "")
        footerStatus.font = NSFont.systemFont(ofSize: nativeUIFontSize)
        footerStatus.textColor = .secondaryLabelColor
        footerStatus.lineBreakMode = .byTruncatingTail
        footerStatus.maximumNumberOfLines = 1
        footerStatus.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        self.footerStatusField = footerStatus
        self.closeButton = closeButton

        [titleLabel, accountField, listTitle, loadingSpinner, addButton, removeButton, listFrame, enabledCheckbox, nameLabel, nameField, groupFieldLabel, groupPopUp, multiplierLabel, multiplierField, valueLabel, valueField, copyButton, modelsTitle, modelsList, toggle, footerStatus, closeButton, applyButton].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview($0)
        }
        // The detail column's own trailing edge: every row in it ends here so
        // the column reads as one block rather than a set of fields written
        // one by one against the window's edge.
        let detailGuide = NSLayoutGuide()
        content.addLayoutGuide(detailGuide)
        NSLayoutConstraint.activate([
            // The window keeps its own width: a long key or model list wraps in
            // its row instead of stretching the window to fit the text.  The
            // detail column's own guide is what ends its rows, so the halved
            // column is one fact about the layout rather than a constant
            // repeated by every field in it.
            content.widthAnchor.constraint(equalToConstant: contentWidth),
            detailGuide.leadingAnchor.constraint(equalTo: listFrame.trailingAnchor, constant: 18),
            detailGuide.widthAnchor.constraint(equalToConstant: detailWidth),
            titleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: nativePanelInset),
            titleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            titleLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: 16),
            accountField.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            accountField.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor),
            accountField.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 4),
            listTitle.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            listTitle.topAnchor.constraint(equalTo: accountField.bottomAnchor, constant: 12),
            // 密钥's ＋ / － actions belong to the key list, so they end at the
            // list's own edge instead of the window's.
            removeButton.trailingAnchor.constraint(equalTo: listFrame.trailingAnchor),
            removeButton.centerYAnchor.constraint(equalTo: listTitle.centerYAnchor),
            removeButton.widthAnchor.constraint(equalToConstant: 22),
            addButton.trailingAnchor.constraint(equalTo: removeButton.leadingAnchor, constant: -4),
            addButton.centerYAnchor.constraint(equalTo: listTitle.centerYAnchor),
            addButton.widthAnchor.constraint(equalToConstant: 22),
            listFrame.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            listFrame.topAnchor.constraint(equalTo: listTitle.bottomAnchor, constant: 6),
            listFrame.widthAnchor.constraint(equalToConstant: listWidth),
            listFrame.bottomAnchor.constraint(equalTo: toggle.topAnchor, constant: -14),
            scrollView.leadingAnchor.constraint(equalTo: listFrame.leadingAnchor, constant: 1),
            scrollView.trailingAnchor.constraint(equalTo: listFrame.trailingAnchor, constant: -1),
            scrollView.topAnchor.constraint(equalTo: listFrame.topAnchor, constant: 1),
            scrollView.bottomAnchor.constraint(equalTo: listFrame.bottomAnchor, constant: -1),
            enabledCheckbox.leadingAnchor.constraint(equalTo: detailGuide.leadingAnchor),
            enabledCheckbox.topAnchor.constraint(equalTo: listFrame.topAnchor),
            nameLabel.leadingAnchor.constraint(equalTo: enabledCheckbox.leadingAnchor),
            nameLabel.topAnchor.constraint(equalTo: enabledCheckbox.bottomAnchor, constant: 12),
            nameLabel.widthAnchor.constraint(equalToConstant: detailCaptionWidth),
            nameField.leadingAnchor.constraint(equalTo: nameLabel.trailingAnchor, constant: 8),
            nameField.trailingAnchor.constraint(equalTo: detailGuide.trailingAnchor),
            nameField.centerYAnchor.constraint(equalTo: nameLabel.centerYAnchor),
            groupFieldLabel.leadingAnchor.constraint(equalTo: enabledCheckbox.leadingAnchor),
            groupFieldLabel.topAnchor.constraint(equalTo: nameLabel.bottomAnchor, constant: 10),
            groupFieldLabel.widthAnchor.constraint(equalToConstant: detailCaptionWidth),
            groupPopUp.leadingAnchor.constraint(equalTo: groupFieldLabel.trailingAnchor, constant: 8),
            groupPopUp.trailingAnchor.constraint(equalTo: detailGuide.trailingAnchor),
            groupPopUp.centerYAnchor.constraint(equalTo: groupFieldLabel.centerYAnchor),
            multiplierLabel.leadingAnchor.constraint(equalTo: enabledCheckbox.leadingAnchor),
            multiplierLabel.topAnchor.constraint(equalTo: groupFieldLabel.bottomAnchor, constant: 10),
            multiplierLabel.widthAnchor.constraint(equalToConstant: detailCaptionWidth),
            multiplierField.leadingAnchor.constraint(equalTo: multiplierLabel.trailingAnchor, constant: 8),
            multiplierField.trailingAnchor.constraint(equalTo: detailGuide.trailingAnchor),
            multiplierField.centerYAnchor.constraint(equalTo: multiplierLabel.centerYAnchor),
            valueLabel.leadingAnchor.constraint(equalTo: enabledCheckbox.leadingAnchor),
            valueLabel.topAnchor.constraint(equalTo: multiplierLabel.bottomAnchor, constant: 10),
            valueLabel.widthAnchor.constraint(equalToConstant: detailCaptionWidth),
            valueField.leadingAnchor.constraint(equalTo: valueLabel.trailingAnchor, constant: 8),
            // Both detail lists wrap from their caption's top line, so each row
            // grows down and the rows below it stay clear of the text.
            valueField.topAnchor.constraint(equalTo: valueLabel.topAnchor, constant: 1),
            // The copy icon sits at the detail column's trailing edge; the key
            // wraps beside it instead of running under it.
            copyButton.leadingAnchor.constraint(equalTo: valueField.trailingAnchor, constant: 6),
            copyButton.trailingAnchor.constraint(equalTo: detailGuide.trailingAnchor),
            copyButton.widthAnchor.constraint(equalToConstant: 22),
            copyButton.centerYAnchor.constraint(equalTo: valueField.centerYAnchor),
            // 模型列表 takes the detail column's width under the key's facts;
            // the list scrolls when the station reports more models than the
            // window shows at once.
            modelsTitle.leadingAnchor.constraint(equalTo: enabledCheckbox.leadingAnchor),
            modelsTitle.trailingAnchor.constraint(equalTo: detailGuide.trailingAnchor),
            modelsTitle.topAnchor.constraint(equalTo: valueField.bottomAnchor, constant: 10),
            modelsList.leadingAnchor.constraint(equalTo: enabledCheckbox.leadingAnchor),
            modelsList.trailingAnchor.constraint(equalTo: detailGuide.trailingAnchor),
            modelsList.topAnchor.constraint(equalTo: modelsTitle.bottomAnchor, constant: 6),
            modelsHeight,
            // The detail column's last line ends above the footer, and the
            // window grows to fit the rows it carries (the key list stretches
            // with it).
            modelsList.bottomAnchor.constraint(lessThanOrEqualTo: toggle.topAnchor, constant: -14),
            toggle.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            toggle.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -nativePanelInset),
            // The result line takes the free space between the switch and the
            // buttons; the buttons keep the trailing edge they had.
            footerStatus.leadingAnchor.constraint(equalTo: toggle.trailingAnchor, constant: 12),
            footerStatus.trailingAnchor.constraint(lessThanOrEqualTo: closeButton.leadingAnchor, constant: -8),
            footerStatus.centerYAnchor.constraint(equalTo: toggle.centerYAnchor),
            applyButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            applyButton.centerYAnchor.constraint(equalTo: toggle.centerYAnchor),
            closeButton.trailingAnchor.constraint(equalTo: applyButton.leadingAnchor, constant: -8),
            closeButton.centerYAnchor.constraint(equalTo: toggle.centerYAnchor),
            // The wheel rides 密钥's own line, at the list column's left edge:
            // the load it reports is about that key list.
            loadingSpinner.leadingAnchor.constraint(equalTo: listTitle.trailingAnchor, constant: 6),
            loadingSpinner.centerYAnchor.constraint(equalTo: listTitle.centerYAnchor),
            loadingSpinner.widthAnchor.constraint(equalToConstant: AppKitBusySpinner.size),
            loadingSpinner.heightAnchor.constraint(equalToConstant: AppKitBusySpinner.size),
        ])
        // The window fits the longer of the key list and the detail column, so
        // a three-key account still shows every detail row.  The fit is
        // reapplied once a selection loads its wrapped rows.
        fitPanelToContent()
        // The selection drives the detail column, so the window opens on the
        // first key instead of a blank form.
        if !rows.isEmpty {
            table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        }
        updateLoadingChrome()
        return panel
    }

    /// Applies a later snapshot of the same account to the open window: the
    /// window opens on the account facts the provider window already holds and
    /// then shows the station's refreshed layout.  The rows are replaced
    /// wholesale because the window is read-only until the load lands, so no
    /// staged edit can be lost to it.
    func applyData(accountLabel: String, groups: [GroupOption], rows: [KeyRow], autoGrouping: Bool) {
        let previousID = selectedRowID
        self.accountLabel = accountLabel
        self.groups = groups
        self.rows = rows
        self.currentGroupNames = NativeGroupManagerController.groupNames(groups)
        self.initialAutoGrouping = autoGrouping
        accountField?.stringValue = accountLabel
        toggle?.state = autoGrouping ? .on : .off
        stagedDeleteCount = 0
        // A plaintext value the window read belongs to the key id it came from;
        // a key the refresh replaced is read again on its next selection.
        let ids = Set(rows.map { $0.id })
        revealedKeys = revealedKeys.filter { ids.contains($0.key) }
        revealQueue = revealQueue.filter { ids.contains($0) }
        // The station is the authority on which keys exist: a key it no longer
        // reports is forgotten rather than shown from what the host remembered.
        for row in rows where !row.isDraft && row.hint.isEmpty {
            NativeRelayKeyMemo.shared.remember(accountID: accountID, resourceID: row.id, value: "")
        }
        modelsListHeight?.constant = CGFloat(NativeGroupManagerController.modelGridRows(rows)) * 17
        groupPopUp?.removeAllItems()
        groupPopUp?.addItems(withTitles: groups.map { $0.label })
        loading = false
        // The selection follows its key across the load while that key is still
        // there; otherwise the window opens on the first row of the new list.
        let index = previousID.flatMap { id in rows.firstIndex { $0.id == id } } ?? (rows.isEmpty ? -1 : 0)
        table?.reloadData()
        if index >= 0 && index < rows.count {
            table?.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        } else {
            table?.deselectAll(nil)
        }
        updateLoadingChrome()
    }

    /// The list header states a load in flight and what it withholds: the wheel
    /// turns beside 密钥, while the switch and every edit control the update is
    /// about to replace are disabled.  Close stays available: a load the user
    /// does not want to wait for can still be discarded.
    private func updateLoadingChrome() {
        loadingSpinner?.isHidden = !loading
        if loading {
            startLoadingWheel()
        } else {
            stopLoadingWheel()
        }
        refreshStatusText()
        toggle?.isEnabled = !loading && !saving
        loadDetail()
        refreshApplyButton()
    }

    /// Turns the shared wheel one step per tick, in common modes so it keeps
    /// turning while a menu or a scroll is tracking.  The timer holds the
    /// controller weakly: a window that ends while the load is still in flight
    /// takes its wheel with it instead of leaving one turning off screen.
    private func startLoadingWheel() {
        guard loadingTimer == nil else { return }
        loadingSpinner?.image = AppKitBusySpinner.frames[loadingStep]
        let timer = Timer(timeInterval: AppKitBusySpinner.stepInterval, repeats: true) { [weak self] timer in
            guard let self else {
                timer.invalidate()
                return
            }
            self.advanceLoadingWheel()
        }
        loadingTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func stopLoadingWheel() {
        loadingTimer?.invalidate()
        loadingTimer = nil
    }

    /// The wheel turns by swapping pre-rendered frames, so nothing about the
    /// window's layout changes while it reports progress.
    private func advanceLoadingWheel() {
        guard !AppKitBusySpinner.frames.isEmpty else { return }
        loadingStep = (loadingStep + 1) % AppKitBusySpinner.frames.count
        loadingSpinner?.image = AppKitBusySpinner.frames[loadingStep]
    }

    /// The window fits the longer of the key list and the detail column, so a
    /// short key list still shows every detail row.  A loaded selection adds
    /// the 模型列表 grid, so the fit is reapplied whenever the rows load.
    private func fitPanelToContent() {
        guard let panel, let content = panel.contentView else { return }
        let minimumHeight = min(content.fittingSize.height, 900)
        if minimumHeight > content.frame.height {
            panel.setContentSize(NSSize(width: content.frame.width, height: minimumHeight))
        }
    }

    /// The detail pane follows the list selection and the auto-grouping switch.
    private func loadDetail() {
        guard let table else { return }
        let index = table.selectedRow
        let hasRow = index >= 0 && index < rows.count
        let editable = editingEnabled && hasRow
        // The list header keeps only the ＋ / － it can perform: no group to
        // place a key in, or no selected key, hides the control instead of
        // greying it out.
        let canAddKey = editingEnabled && !groups.isEmpty
        addButton?.isEnabled = canAddKey
        addButton?.isHidden = !canAddKey
        removeButton?.isEnabled = editable
        removeButton?.isHidden = !editable
        enabledCheckbox?.isEnabled = editable
        nameField?.isEnabled = editable
        groupPopUp?.isEnabled = editable && !groups.isEmpty
        let empty = label("emptyLabel")
        guard hasRow else {
            enabledCheckbox?.state = .off
            nameField?.stringValue = ""
            multiplierField?.stringValue = empty
            // Nothing is selected, so no read may write into the value row.
            valueField?.stringValue = empty
            valueField?.toolTip = nil
            modelsList?.setModels([])
            copyButton?.isEnabled = false
            syncingSelection = true
            groupPopUp?.selectItem(at: -1)
            syncingSelection = false
            return
        }
        let row = rows[index]
        enabledCheckbox?.state = row.enabled ? .on : .off
        multiplierField?.stringValue = row.multiplier.isEmpty ? empty : row.multiplier
        multiplierField?.toolTip = row.multiplier.isEmpty ? nil : row.multiplier
        revealSelectedKey(row)
        modelsList?.setModels(row.modelNames)
        copyButton?.isEnabled = canCopy(row)
        nameField?.stringValue = row.name
        syncingSelection = true
        if let groupIndex = groups.firstIndex(where: { $0.id == row.groupID }) {
            groupPopUp?.selectItem(at: groupIndex)
        } else {
            groupPopUp?.selectItem(at: -1)
        }
        syncingSelection = false
        fitPanelToContent()
    }

    private func reloadAndSelect(_ index: Int) {
        table?.reloadData()
        if index >= 0 && index < rows.count {
            table?.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        } else {
            table?.deselectAll(nil)
        }
        loadDetail()
        refreshApplyButton()
    }

    private func refreshApplyButton() {
        applyButton?.isEnabled = !loading && !saving && hasStagedChanges
    }

    /// The words a save in flight shows come from the host's localization
    /// table, so every child states the same wait in the same vocabulary.
    private func savingStatusText() -> String {
        AppKitNativeLeaf.shared.localizedText("childSaving", fallback: "Saving…")
    }

    /// State one line beside the footer buttons.
    func setStatus(_ text: String) {
        footerStatusField?.stringValue = text
        footerStatusField?.setAccessibilityLabel(text)
    }

    /// State a finished outcome: it stays until a later save replaces it.
    func setResultStatus(_ text: String) {
        resultStatus = text
        refreshStatusText()
    }

    /// The one line beside the buttons: the last outcome, else a transient
    /// word (a failed copy), else the save in flight, else nothing — an idle
    /// window says nothing here, and the wait for its rows already rides the
    /// wheel beside 密钥.
    private func refreshStatusText() {
        if let resultStatus {
            setStatus(resultStatus)
        } else if let transientStatus {
            setStatus(transientStatus)
        } else if saving {
            setStatus(savingStatusText())
        } else {
            setStatus("")
        }
    }

    /// The write this window handed over is in flight: its rows and its footer
    /// stay put until the workspace answers, so nothing is staged against data
    /// that answer is about to replace.
    func setSaving(_ value: Bool) {
        saving = value
        refreshStatusText()
        closeButton?.isEnabled = !value
        toggle?.isEnabled = !value && !loading
        updateLoadingChrome()
    }

    private func commitNameField() {
        guard let table, let index = table.selectedRow as Int?, index >= 0, index < rows.count else { return }
        let value = (nameField?.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else {
            nameField?.stringValue = rows[index].name
            return
        }
        guard value != rows[index].name else { return }
        rows[index].name = value
        reloadAndSelect(index)
    }

    @objc private func commitNameField(_ sender: NSTextField) {
        commitNameField()
    }

    @objc private func addDraftKey(_ sender: NSButton) {
        guard let groupID = groups.first?.id else { return }
        let base = label("newKeyName", "New key")
        var name = base
        var suffix = 2
        let existing = Set(rows.map { $0.name })
        while existing.contains(name) {
            name = "\(base) \(suffix)"
            suffix += 1
        }
        rows.append(KeyRow(
            id: "draft:\(UUID().uuidString)",
            name: name,
            groupID: groupID,
            groupLabel: groupLabel(for: groupID),
            multiplier: groupRate(for: groupID),
            hint: "",
            modelNames: [],
            originalName: "",
            originalGroupID: groupID,
            originalEnabled: true,
            enabled: true,
            deleted: false,
            isDraft: true
        ))
        let index = rows.count - 1
        table?.reloadData()
        table?.scrollRowToVisible(index)
        reloadAndSelect(index)
        panel?.makeFirstResponder(nameField)
    }

    @objc private func removeSelectedKey(_ sender: NSButton) {
        guard let table, let index = table.selectedRow as Int?, index >= 0, index < rows.count else { return }
        if rows[index].isDraft {
            rows.remove(at: index)
            reloadAndSelect(-1)
            return
        }
        rows[index].deleted.toggle()
        stagedDeleteCount = rows.filter { !$0.isDraft && $0.deleted }.count
        reloadAndSelect(index)
    }

    @objc private func toggleSelectedEnabled(_ sender: NSButton) {
        guard let table, let index = table.selectedRow as Int?, index >= 0, index < rows.count else { return }
        rows[index].enabled = sender.state == .on
        reloadAndSelect(index)
    }

    @objc private func changeSelectedGroup(_ sender: NSPopUpButton) {
        // loadDetail() also selects an item; only a user choice edits a row.
        guard !syncingSelection else { return }
        guard let table, let index = table.selectedRow as Int?, index >= 0, index < rows.count else { return }
        guard let groupID = selectedGroupID(from: sender) else { return }
        rows[index].groupID = groupID
        rows[index].groupLabel = groupLabel(for: groupID)
        // Re-grouping changes what the key costs, so 倍率 follows the choice.
        rows[index].multiplier = groupRate(for: groupID)
        reloadAndSelect(index)
    }

    /// The key id of the current selection, or nil while nothing is selected.
    private var selectedRowID: String? {
        guard let table else { return nil }
        let index = table.selectedRow
        guard index >= 0, index < rows.count else { return nil }
        return rows[index].id
    }

    /// Show the selected key in plaintext.  The value is read through Core's
    /// native capability and stays in this window: React never receives it.  A
    /// draft key has no key on the station yet, and a row with no credential
    /// states that instead of a stand-in label that reads like the value.
    private func revealSelectedKey(_ row: KeyRow) {
        valueField?.toolTip = nil
        guard canCopy(row) else {
            // 密钥值 carries the key itself: a row whose station has no key
            // says 未提供, and a row the sheet cannot read states nothing.
            valueField?.stringValue = row.hint.isEmpty ? label("emptyLabel") : ""
            return
        }
        if let revealed = revealedKeys[row.id] {
            valueField?.stringValue = revealed
            return
        }
        // The key is read by the pass below, which starts with this row: the
        // sheet shows what it can read rather than a stand-in label, and the
        // rows behind this one are filled while the user looks at this one.
        valueField?.stringValue = ""
        fillRowKeys()
    }

    /// Read the keys this sheet shows: the selected row first, then every other
    /// row that still needs one.  Core answers a key it already holds at once,
    /// and a cold account costs one station read for the whole pass, so a row
    /// carries its key when it is selected instead of staying empty while its
    /// own read runs.
    private func fillRowKeys() {
        let selected = selectedRowID
        var ids = rows
            .filter { canCopy($0) && revealedKeys[$0.id] == nil && !revealInFlight.contains($0.id) && !revealQueue.contains($0.id) }
            .map { $0.id }
        guard !ids.isEmpty else { return }
        if let selected, let index = ids.firstIndex(of: selected), index > 0 {
            ids.remove(at: index)
            ids.insert(selected, at: 0)
        }
        revealQueue.append(contentsOf: ids)
        drainRevealQueue()
    }

    /// One read at a time: this is background work for rows the user may not
    /// look at, and the station answers one key list for the whole account.
    private func drainRevealQueue() {
        guard !revealPassRunning, !revealQueue.isEmpty else { return }
        let keyID = revealQueue.removeFirst()
        revealInFlight.insert(keyID)
        revealPassRunning = true
        let target = "\(accountID):\(keyID)"
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let value = Self.readRevealedKey(target: target)
            DispatchQueue.main.async {
                guard let self else { return }
                self.revealInFlight.remove(keyID)
                self.revealPassRunning = false
                if let value {
                    self.revealedKeys[keyID] = value
                    NativeRelayKeyMemo.shared.remember(accountID: self.accountID, resourceID: keyID, value: value)
                    // The row being looked at shows what this pass read; a
                    // wrapped key makes the detail column taller than the
                    // panel was sized for, so the window takes the height it
                    // now needs.
                    if self.selectedRowID == keyID {
                        self.valueField?.stringValue = value
                        self.fitPanelToContent()
                    }
                } else if self.selectedRowID == keyID {
                    self.showTransientStatus(self.label("failedLabel"))
                }
                self.drainRevealQueue()
            }
        }
    }

    /// The key behind one relay resource, read through Core's one-time
    /// plaintext lease.  A Core revision that moves between the lease and the
    /// read fails that pair rather than the key, so a lost race is retried
    /// against a fresh lease instead of leaving the row without its value.
    private static func readRevealedKey(target: String) -> String? {
        let attempts = 3
        for attempt in 1...attempts {
            if let value = try? CoreIPCBridge.shared.readPlainTextSecret(
                domain: "relay_accounts",
                field: "api_key",
                target: target
            ), !value.isEmpty {
                return value
            }
            if attempt < attempts { Thread.sleep(forTimeInterval: 0.25) }
        }
        return nil
    }

    /// A key can only be revealed or copied while the station still holds it.
    private func canCopy(_ row: KeyRow) -> Bool {
        !row.hint.isEmpty && !row.isDraft && !row.id.isEmpty
    }

    /// Copy the selected key through Core's plaintext capability — the same
    /// target the provider workspace uses — and report the outcome in place.
    @objc private func copySelectedKey(_ sender: NSButton) {
        guard let table, let index = table.selectedRow as Int?, index >= 0, index < rows.count else { return }
        let row = rows[index]
        guard canCopy(row) else { return }
        // A key the window already revealed copies from what it holds, so the
        // read-once lease is not spent twice on the same key.
        if let revealed = revealedKeys[row.id] {
            let pasteboard = NSPasteboard.general
            pasteboard.clearContents()
            let copied = pasteboard.setString(revealed, forType: .string)
            copyButton?.isEnabled = canCopy(row)
            // A copy that worked says nothing: the key is on the pasteboard,
            // and only a failure is worth a word on the window's status line.
            if (!copied) { showTransientStatus(label("failedLabel")) }
            return
        }
        let target = "\(accountID):\(row.id)"
        copyButton?.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let value = Self.readRevealedKey(target: target)
            DispatchQueue.main.async {
                guard let self else { return }
                var copied = false
                if let value {
                    self.revealedKeys[row.id] = value
                    NativeRelayKeyMemo.shared.remember(accountID: self.accountID, resourceID: row.id, value: value)
                    let pasteboard = NSPasteboard.general
                    pasteboard.clearContents()
                    copied = pasteboard.setString(value, forType: .string)
                }
                self.copyButton?.isEnabled = self.canCopy(row)
                if (!copied) { self.showTransientStatus(self.label("failedLabel")) }
            }
        }
    }

    /// State a transient result on the window's one status line; it clears
    /// itself, and the newest message owns the line.
    func showTransientStatus(_ message: String, clearAfter seconds: Double = 4) {
        transientStatusToken += 1
        transientStatus = message.isEmpty ? nil : message
        refreshStatusText()
        guard !message.isEmpty else { return }
        let token = transientStatusToken
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            guard let self, self.transientStatusToken == token else { return }
            self.transientStatus = nil
            self.refreshStatusText()
        }
    }

    @objc private func toggleAutoGrouping(_ sender: NSButton) {
        // Leaving the switch off restores the staged deletes the automatic
        // layout had marked, so the list never shows them while it is on.
        if sender.state == .on, stagedDeleteCount > 0 {
            for index in rows.indices where rows[index].deleted { rows[index].deleted = false }
            stagedDeleteCount = 0
            table?.reloadData()
        }
        loadDetail()
        refreshApplyButton()
    }

    /// Close discards the draft, so a window that would lose edits asks first;
    /// a window the user never touched closes straight away.
    /// Close the window: Close drops the staged draft, Save and Close hands it
    /// back to the workspace.  Either way the leaf settles the pending result,
    /// so the pane hears an answer the moment the window goes away.
    @objc private func closePanel(_ sender: NSButton) {
        AppKitNativeLeaf.shared.closeGroupManager(applied: false)
    }

    @objc private func applyPanel(_ sender: NSButton) {
        guard hasStagedChanges, !saving else { return }
        commitNameField()
        // The edits go to the workspace while this window stays up: it states
        // the save in its own status strip and closes when the write landed.
        AppKitNativeLeaf.shared.handGroupManagerApply()
    }

    /// The staged edits, or nil when the draft would not change the account.
    func stagedResult() -> NativeGroupManagerResult? {
        guard hasStagedChanges else { return nil }
        return NativeGroupManagerResult(
            autoGrouping: (toggle?.state ?? (initialAutoGrouping ? .on : .off)) == .on,
            creates: rows.filter { $0.isDraft && !$0.deleted && !$0.name.isEmpty }.map {
                NativeGroupManagerResult.Create(name: $0.name, groupID: $0.groupID)
            },
            updates: rows.filter {
                !$0.isDraft && !$0.deleted && ($0.name != $0.originalName || $0.groupID != $0.originalGroupID || $0.enabled != $0.originalEnabled)
            }.map {
                NativeGroupManagerResult.Update(keyID: $0.id, name: $0.name, groupID: $0.groupID, enabled: $0.enabled)
            },
            deletes: rows.filter { !$0.isDraft && $0.deleted }.map { $0.id }
        )
    }

    /// The staged edits, once the window is ending with them handed over.
    func resultOnEnd() -> NativeGroupManagerResult? {
        guard applied else { return nil }
        return stagedResult()
    }

    func numberOfRows(in tableView: NSTableView) -> Int {
        rows.count
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row >= 0 && row < rows.count else { return nil }
        let entry = rows[row]
        let columnID = tableColumn?.identifier.rawValue ?? "key-name"
        let value: String
        if columnID == "key-name" {
            value = entry.name
        } else if columnID == "key-group" {
            // A key can point at a group the station no longer offers; report it
            // the same way the rest of the window does.
            value = currentGroupText(for: entry)
        } else {
            value = presentation(for: entry)
        }
        let identifier = NSUserInterfaceItemIdentifier("group-manager-\(columnID)")
        let cell: NSTableCellView
        if let reused = tableView.makeView(withIdentifier: identifier, owner: self) as? NSTableCellView {
            cell = reused
        } else {
            cell = NSTableCellView()
            cell.identifier = identifier
            let label = NSTextField(labelWithString: "")
            label.translatesAutoresizingMaskIntoConstraints = false
            label.font = NSFont.systemFont(ofSize: nativeUIFontSize)
            label.lineBreakMode = .byTruncatingTail
            label.cell?.truncatesLastVisibleLine = true
            label.maximumNumberOfLines = 1
            label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            label.setContentHuggingPriority(.defaultLow, for: .horizontal)
            cell.addSubview(label)
            cell.textField = label
            NSLayoutConstraint.activate([
                label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8),
                label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -8),
                label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            ])
        }
        let text = cell.textField
        text?.stringValue = value
        text?.toolTip = value
        // The list reports the store as it stands: every column keeps the full
        // label color, and a row the draft will create or delete is dimmed —
        // no column carries status text.
        text?.textColor = isStaged(entry) ? .secondaryLabelColor : .labelColor
        cell.setAccessibilityLabel(value)
        return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        if syncingSelection { return }
        loadDetail()
    }

    func controlTextDidEndEditing(_ obj: Notification) {
        commitNameField()
    }
}

final class NativeSplitView: NSSplitView {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        isVertical = true
        dividerStyle = .thin
        autosaveName = "YoungRouter.SettingsSplitView"
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
    }
}

/// The app's busy wheel: the frames a working control swaps in place to report
/// progress.  One drawing serves both callers - the Fabric busy button in
/// `AppKitControlViews.mm`, which reads this class through the generated Swift
/// header the way it reads `LiteLLMPersistentScroller`, and the 分组管理 window's
/// key list header beside 密钥 - so a wait in flight is one wheel, never a second
/// one drawn beside it.
///
/// The wheel is drawn as the control's own leading image, so AppKit centers the
/// icon together with the title exactly like a symbol-with-title button: one
/// centered group, no view laid over the bezel.  A native progress indicator
/// only renders correctly at its own intrinsic sizes, so the wheel is the small
/// graded spoke early iOS used, pre-rendered as rotated template frames that
/// AppKit tints with whatever the caller draws its text in.
///
/// The ObjC control views read this class through the generated Swift header, so
/// it must be public to appear in `YoungRouter-Swift.h`.
@objc(AppKitBusySpinner)
public final class AppKitBusySpinner: NSObject {
    /// The wheel's own size in points: every frame is drawn at this box, so a
    /// caller that reserves it never re-lays out while the wheel turns.
    @objc public static let size: CGFloat = 12
    /// Steps per full turn.
    @objc public static let stepCount = 12
    /// How long one step lasts, so one turn takes about a second.
    @objc public static var stepInterval: TimeInterval {
        1 / TimeInterval(stepCount)
    }
    /// The frames, one per step, in step order: a caller only ever swaps them.
    @objc public static let frames: [NSImage] = (0 ..< stepCount).map { step in
        frame(rotation: -360 / CGFloat(stepCount) * CGFloat(step))
    }

    /// One frame: the wheel drawn into a 2x bitmap and rotated into place, so the
    /// caller only swaps images and never redraws, resizes, or re-lays out.
    private static func frame(rotation: CGFloat) -> NSImage {
        // Two device pixels per point keeps the spokes crisp on a Retina display.
        let scale = 2
        let pixels = Int((size * CGFloat(scale)).rounded())
        let image = NSImage(size: NSSize(width: size, height: size))
        guard let representation = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: pixels,
            pixelsHigh: pixels,
            bitsPerSample: 8,
            samplesPerPixel: 4,
            hasAlpha: true,
            isPlanar: false,
            colorSpaceName: .deviceRGB,
            bytesPerRow: 0,
            bitsPerPixel: 0
        ), let context = NSGraphicsContext(bitmapImageRep: representation) else { return image }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        let transform = NSAffineTransform()
        transform.scale(by: CGFloat(scale))
        transform.translateX(by: size / 2, yBy: size / 2)
        transform.rotate(byDegrees: rotation)
        transform.translateX(by: -size / 2, yBy: -size / 2)
        transform.concat()
        drawWheel()
        NSGraphicsContext.restoreGraphicsState()
        image.addRepresentation(representation)
        // A template image is tinted by AppKit like the text it sits beside, so
        // the wheel needs no color of its own.
        image.isTemplate = true
        return image
    }

    /// One graded spoke per position: a template image is masked by its alpha,
    /// so the black spokes differ only by opacity - the fading trail that makes
    /// the wheel read as motion.
    private static func drawWheel() {
        let outer = size / 2 - 0.75
        let inner = outer * 0.42
        let center = size / 2
        let path = NSBezierPath()
        path.lineCapStyle = .round
        path.lineWidth = max(1.1, outer * 0.3)
        for step in 0 ..< stepCount {
            let alpha = 0.16 + 0.84 * (CGFloat(step) / CGFloat(stepCount - 1))
            let angle = CGFloat(step) * (2 * .pi / CGFloat(stepCount))
            let dx = sin(angle)
            let dy = -cos(angle)
            path.removeAllPoints()
            path.move(to: NSPoint(x: center + dx * inner, y: center + dy * inner))
            path.line(to: NSPoint(x: center + dx * outer, y: center + dy * outer))
            NSColor.black.withAlphaComponent(alpha).setStroke()
            path.stroke()
        }
    }
}

/// A scroller this app keeps on screen while its content overflows, drawn the
/// way the system draws its overlay scroller: one translucent capsule knob and
/// no track, so the row or cell underneath stays readable.
///
/// AppKit's own overlay scroller fades out as soon as scrolling stops, and a
/// custom `NSScroller` subclass cannot take the overlay style at all - AppKit
/// tiles such a scroller as a legacy bar in its own gutter.  The hosts
/// therefore keep the legacy style (so this class draws the knob) and float the
/// scroller over the content themselves
/// (`FloatPersistentScrollerOverContent` in `AppKitControlViews.mm` and
/// `floatOverContent()` below), which is what removes the gutter.
///
/// The ObjC hosts instantiate this class through the generated Swift header, so
/// it must be public to appear in `YoungRouter-Swift.h`.
@objc(LiteLLMPersistentScroller)
public final class LiteLLMPersistentScroller: NSScroller {
    private static let knobThickness: CGFloat = 6
    private static let idleKnobAlpha: CGFloat = 0.35
    private static let pressedKnobAlpha: CGFloat = 0.5

    public override func drawKnobSlot(in slotRect: NSRect, highlight flag: Bool) {
        // Deliberately empty: no track is drawn over the content.
    }

    public override func drawKnob() {
        let knobRect = rect(for: .knob)
        guard !knobRect.isEmpty else { return }
        let thickness = min(LiteLLMPersistentScroller.knobThickness, min(knobRect.width, knobRect.height))
        let fillRect = knobRect.width > knobRect.height
            ? knobRect.insetBy(dx: 0, dy: (knobRect.height - thickness) / 2)
            : knobRect.insetBy(dx: (knobRect.width - thickness) / 2, dy: 0)
        let radius = min(thickness, min(fillRect.width, fillRect.height)) / 2
        let alpha = hitPart == .knob
            ? LiteLLMPersistentScroller.pressedKnobAlpha
            : LiteLLMPersistentScroller.idleKnobAlpha
        NSColor.labelColor.withAlphaComponent(alpha).setFill()
        NSBezierPath(roundedRect: fillRect, xRadius: radius, yRadius: radius).fill()
    }
}

/// Keep the platform overlay scroller on screen while the content overflows.
/// A custom NSScroller subclass cannot take the overlay style - AppKit tiles it
/// as a legacy bar in its own gutter - so the floating, translucent platform
/// scroller is what the app keeps.
extension NSScrollView {
    /// Draw the scrollers this scroll view keeps visible with
    /// `LiteLLMPersistentScroller` instead of AppKit's opaque legacy bar, unhide
    /// them (AppKit leaves a scroller it hid while idle hidden until the next
    /// scroll event), and float them over the content instead of the gutter
    /// AppKit tiles for a legacy scroller.  AppKit rebuilds a scroller whenever
    /// a scroller flag flips back on, so callers run this from their layout
    /// path.
    func usePersistentScrollers(horizontal: Bool, vertical: Bool) {
        if horizontal, hasHorizontalScroller, !(horizontalScroller is LiteLLMPersistentScroller) {
            horizontalScroller = LiteLLMPersistentScroller(frame: .zero)
        }
        if vertical, hasVerticalScroller, !(verticalScroller is LiteLLMPersistentScroller) {
            verticalScroller = LiteLLMPersistentScroller(frame: .zero)
        }
        if horizontal, hasHorizontalScroller {
            horizontalScroller?.isHidden = false
            horizontalScroller?.alphaValue = 1
        }
        if vertical, hasVerticalScroller {
            verticalScroller?.isHidden = false
            verticalScroller?.alphaValue = 1
        }
        floatOverContent()
    }

    /// Let the clip view span the full scroll view and place the legacy
    /// scrollers over the content's trailing edges: AppKit's legacy tiling
    /// reserved a gutter for them, which took 15 pt off the list.
    func floatOverContent() {
        guard scrollerStyle == .legacy else { return }
        let bounds = self.bounds
        var verticalStrip: CGFloat = 0
        if hasVerticalScroller, let scroller = verticalScroller {
            verticalStrip = max(11, scroller.frame.width)
            var frame = scroller.frame
            frame.origin.x = bounds.maxX - verticalStrip
            scroller.frame = frame
        }
        var horizontalStrip: CGFloat = 0
        if hasHorizontalScroller, let scroller = horizontalScroller {
            horizontalStrip = max(11, scroller.frame.height)
            var frame = scroller.frame
            frame.origin.y = isFlipped ? bounds.maxY - horizontalStrip : bounds.minY
            scroller.frame = frame
        }
        let clipFrame = NSRect(x: bounds.minX, y: bounds.minY,
                               width: bounds.width, height: bounds.height)
        if contentView.frame != clipFrame {
            contentView.frame = clipFrame
        }
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
        textView.font = NSFont.monospacedSystemFont(ofSize: nativeUIFontSize, weight: .regular)
        textView.usesFindPanel = true
        textView.textContainer?.widthTracksTextView = false
        textView.textContainer?.containerSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        documentView = textView
        usePersistentScrollers(horizontal: true, vertical: true)
    }

    override func layout() {
        super.layout()
        usePersistentScrollers(horizontal: true, vertical: true)
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

private final class NativeInstantFocusBorderView: NSView {
    var borderVisible = false {
        didSet {
            guard borderVisible != oldValue else { return }
            updateBorder()
        }
    }
    var borderCornerRadius: CGFloat = 0 {
        didSet {
            guard borderCornerRadius != oldValue else { return }
            updateBorder()
        }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        updateBorder()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = true
        updateBorder()
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    private func updateBorder() {
        withoutAnimations {
            layer?.cornerRadius = borderCornerRadius
            layer?.borderColor = NSColor.keyboardFocusIndicatorColor.cgColor
            layer?.borderWidth = borderVisible ? 3 : 0
        }
    }
}

private final class NativeInstantFocusSearchField: NSSearchField {
    private let focusBorderView = NativeInstantFocusBorderView(frame: .zero)
    var showsInstantFocusBorder = false {
        didSet {
            guard showsInstantFocusBorder != oldValue else { return }
            updateFocusBorder()
        }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        configureFocusBorder()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        configureFocusBorder()
    }

    override func layout() {
        super.layout()
        focusBorderView.frame = bounds
        updateFocusBorder()
    }

    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted {
            DispatchQueue.main.async { [weak self] in
                guard let self, window?.firstResponder === currentEditor() else { return }
                showsInstantFocusBorder = true
            }
        }
        return accepted
    }

    private func configureFocusBorder() {
        focusBorderView.frame = bounds
        focusBorderView.autoresizingMask = [.width, .height]
        addSubview(focusBorderView, positioned: .above, relativeTo: nil)
        updateFocusBorder()
    }

    private func updateFocusBorder() {
        focusBorderView.borderCornerRadius = bounds.height / 2
        focusBorderView.borderVisible = showsInstantFocusBorder
    }
}

private final class NativeModelChooserScrollView: NSScrollView {
    weak var modelListView: NativeModelChooserListView?

    override func reflectScrolledClipView(_ cClipView: NSClipView) {
        super.reflectScrolledClipView(cClipView)
        modelListView?.updateVisibleRows()
    }

    override func layout() {
        super.layout()
        usePersistentScrollers(horizontal: false, vertical: true)
        guard let listView = (documentView as? NativeModelChooserListView) ?? modelListView else { return }
        let viewportWidth = contentView.bounds.width
        if viewportWidth > 0, abs(listView.frame.width - viewportWidth) > 0.5 {
            var frame = listView.frame
            frame.size.width = viewportWidth
            listView.frame = frame
        }
        listView.updateVisibleRows()
    }

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
        (documentView as? NativeModelChooserListView ?? modelListView)?.updateVisibleRows()
    }
}

private final class NativeModelChooserListView: NSView {
    private struct Row {
        let title: String
        let foldedTitle: String
        var selected: Bool
    }
    private var rows: [Row]
    private var visibleRowIndexes: [Int]
    private var searchQuery = ""
    private var minimumDocumentHeight: CGFloat = 0
    private let emptyLabel: String
    private let noMatchesLabel: String
    private var rowButtons: [Int: NSButton] = [:]
    private var emptyStateLabel: NSTextField?
    private var updatingVisibleRows = false
    var stateDidChange: (() -> Void)?
    let rowHeight: CGFloat = 28

    override class var isCompatibleWithResponsiveScrolling: Bool { true }
    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }

    init(models: [String], width: CGFloat, emptyLabel: String, noMatchesLabel: String) {
        rows = models.map {
            Row(
                title: $0,
                foldedTitle: $0.folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: .current),
                selected: false
            )
        }
        visibleRowIndexes = Array(models.indices)
        self.emptyLabel = emptyLabel
        self.noMatchesLabel = noMatchesLabel
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: max(rowHeight, CGFloat(models.count) * rowHeight)))
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func setMinimumDocumentHeight(_ height: CGFloat) { minimumDocumentHeight = max(0, height); updateDocumentHeight() }
    func setSearchQuery(_ query: String) {
        searchQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let terms = searchQuery.split(whereSeparator: { $0.isWhitespace }).map { String($0).folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: .current) }
        visibleRowIndexes = terms.isEmpty ? Array(rows.indices) : rows.indices.filter { index in terms.allSatisfy(rows[index].foldedTitle.contains) }
        updateDocumentHeight()
        updateVisibleRows()
        stateDidChange?()
    }
    func selectAll() { for index in visibleRowIndexes { rows[index].selected = true }; updateVisibleRows(); stateDidChange?() }
    func invertSelection() { for index in visibleRowIndexes { rows[index].selected.toggle() }; updateVisibleRows(); stateDidChange?() }
    private func updateDocumentHeight() { setFrameSize(NSSize(width: frame.width, height: max(minimumDocumentHeight, CGFloat(max(1, visibleRowIndexes.count)) * rowHeight))) }
    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        updateVisibleRows()
    }

    func updateVisibleRows() {
        guard !updatingVisibleRows else { return }
        updatingVisibleRows = true
        defer { updatingVisibleRows = false }
        let visibleRect = self.visibleRect
        let firstVisible = max(0, Int(floor(visibleRect.minY / rowHeight)) - 2)
        let lastVisible = min(visibleRowIndexes.count, Int(ceil(visibleRect.maxY / rowHeight)) + 2)
        let neededIndexes = visibleRowIndexes.isEmpty ? Set<Int>() : Set(firstVisible..<max(firstVisible, lastVisible))
        let staleIndexes = rowButtons.keys.filter { !neededIndexes.contains($0) }
        for visibleIndex in staleIndexes {
            guard let button = rowButtons[visibleIndex] else { continue }
            button.removeFromSuperview()
            rowButtons.removeValue(forKey: visibleIndex)
        }
        for visibleIndex in neededIndexes {
            guard visibleIndex >= 0, visibleIndex < visibleRowIndexes.count else { continue }
            let rowIndex = visibleRowIndexes[visibleIndex]
            let checkbox = rowButtons[visibleIndex] ?? makeRowButton(rowIndex: rowIndex)
            checkbox.tag = rowIndex
            checkbox.title = rows[rowIndex].title
            checkbox.state = rows[rowIndex].selected ? .on : .off
            checkbox.toolTip = rows[rowIndex].title
            checkbox.setAccessibilityLabel(rows[rowIndex].title)
            checkbox.frame = NSRect(x: 10, y: CGFloat(visibleIndex) * rowHeight + 2, width: max(0, bounds.width - 20), height: rowHeight - 4)
            checkbox.autoresizingMask = [.width]
            if rowButtons[visibleIndex] == nil {
                rowButtons[visibleIndex] = checkbox
                addSubview(checkbox)
            }
        }
        let label = emptyStateLabel ?? makeEmptyStateLabel()
        label.stringValue = searchQuery.isEmpty ? emptyLabel : noMatchesLabel
        let labelHeight = min(40, max(rowHeight, bounds.height))
        label.frame = NSRect(x: 12, y: max(0, floor((bounds.height - labelHeight) / 2)), width: max(0, bounds.width - 24), height: labelHeight)
        label.isHidden = !visibleRowIndexes.isEmpty
    }

    private func makeRowButton(rowIndex: Int) -> NSButton {
        let checkbox = NSButton(checkboxWithTitle: rows[rowIndex].title, target: self, action: #selector(toggleRow(_:)))
        checkbox.font = NSFont.systemFont(ofSize: nativeUIFontSize)
        checkbox.lineBreakMode = .byTruncatingMiddle
        return checkbox
    }

    private func makeEmptyStateLabel() -> NSTextField {
        let label = NSTextField(labelWithString: "")
        label.textColor = .secondaryLabelColor
        label.alignment = .center
        label.usesSingleLineMode = false
        label.maximumNumberOfLines = 2
        label.lineBreakMode = .byWordWrapping
        label.cell?.wraps = true
        label.cell?.isScrollable = false
        label.autoresizingMask = [.width, .height]
        emptyStateLabel = label
        addSubview(label)
        return label
    }

    override func layout() {
        super.layout()
        updateVisibleRows()
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
    private var didFinish = false
    var onFinish: (([String]?) -> Void)?
    /// The chooser's window, so its search field is focused once the window is
    /// key. The host retains both the window and this controller while the
    /// chooser is up.
    weak var chooserWindow: NSWindow?
    weak var searchField: NativeInstantFocusSearchField?
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

    func configureControls(searchField: NativeInstantFocusSearchField, scrollView: NSScrollView, resultCountLabel: NSTextField, selectAllButton: NSButton, invertSelectionButton: NSButton, addButton: NSButton, minimumListHeight: CGFloat) {
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

    func focusSearchField() {
        guard let window = chooserWindow, let field = searchField else { return }
        window.makeFirstResponder(field)
    }

    func windowDidBecomeKey(_ notification: Notification) {
        guard let window = notification.object as? NSWindow, window === chooserWindow else { return }
        DispatchQueue.main.async { [weak self] in self?.focusSearchField() }
    }

    func controlTextDidEndEditing(_ obj: Notification) {
        guard let field = obj.object as? NativeInstantFocusSearchField, field === searchField else { return }
        field.showsInstantFocusBorder = false
    }

    func controlTextDidChange(_ obj: Notification) {
        guard let field = obj.object as? NSSearchField, field === searchField else { return }
        // Filtering uses cached folded model names and only updates the rows
        // inside the viewport, so it is safe to run in AppKit's text callback.
        // Keeping this synchronous also preserves NSSearchField's editing
        // transaction and its insertion caret while the chooser window is key.
        listView.setSearchQuery(field.stringValue)
        if let scrollView = self.scrollView {
            scrollView.contentView.scroll(to: .zero)
            scrollView.reflectScrolledClipView(scrollView.contentView)
        }
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
    @objc func addSelectedAction(_ sender: Any?) { finish([String](listView.selectedModels)) }
    @objc func cancelAction(_ sender: Any?) { finish(nil) }
    func windowWillClose(_ notification: Notification) { finish(nil) }

    /// Settle the pending chooser exactly once, whichever way its window went
    /// away: the Add button carries the selection, Cancel and the title-bar
    /// close button carry nothing. The host ends the child surface from this
    /// one completion.
    private func finish(_ selection: [String]?) {
        guard !didFinish else { return }
        didFinish = true
        let completion = onFinish
        onFinish = nil
        completion?(selection)
    }
    var selectedModels: [String] { listView.selectedModels }
}

private func readOnlyCodeEditorHTML(html: String, text: String, language: String) -> String {
    let payload: [String: Any] = [
        "type": "replace",
        "documentKey": "readonly",
        "value": text,
        "baseline": text,
        "language": language,
        "readOnly": true,
        "showDiff": false,
    ]
    let data = try! JSONSerialization.data(withJSONObject: payload)
    let json = String(data: data, encoding: .utf8)!
        .replacingOccurrences(of: "</script", with: "<\\/script", options: .caseInsensitive)
    let bodyEnd = html.range(of: "</body>", options: [.caseInsensitive, .backwards])!
    let command = "<script>window.LiteLLMCodeEditor && window.LiteLLMCodeEditor.receive(\(json));</script>"
    return html.replacingCharacters(in: bodyEnd, with: "\(command)</body>")
}

private final class NativeReadOnlyCodeController: NSObject, NSWindowDelegate {
    let panel: NSPanel

    private let webView: WKWebView
    private var documentHTML: String
    private var onClose: ((NativeReadOnlyCodeController) -> Void)?
    private var stopped = false

    init(
        title: String,
        text: String,
        closeTitle: String,
        language: String,
        html: String,
        onClose: @escaping (NativeReadOnlyCodeController) -> Void
    ) {
        let configuration = WKWebViewConfiguration()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        webView = WKWebView(frame: .zero, configuration: configuration)
        documentHTML = readOnlyCodeEditorHTML(html: html, text: text, language: language)
        self.onClose = onClose

        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        super.init()

        configureImmediatePresentation(panel)
        panel.title = title
        panel.minSize = NSSize(width: 560, height: 380)
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        panel.center()

        let content = NSView()
        panel.contentView = content

        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = nativeHeadingFont
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let editorFrame = NSView()
        editorFrame.wantsLayer = true
        editorFrame.layer?.borderColor = NSColor.separatorColor.cgColor
        editorFrame.layer?.borderWidth = 1
        webView.translatesAutoresizingMaskIntoConstraints = false
        editorFrame.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: editorFrame.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: editorFrame.trailingAnchor),
            webView.topAnchor.constraint(equalTo: editorFrame.topAnchor),
            webView.bottomAnchor.constraint(equalTo: editorFrame.bottomAnchor),
        ])

        let closeButton = NSButton(title: closeTitle, target: self, action: #selector(closeAction(_:)))
        closeButton.bezelStyle = .rounded
        closeButton.keyEquivalent = "\u{1b}"

        for view in [titleLabel, editorFrame, closeButton] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        NSLayoutConstraint.activate([
            titleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: nativePanelInset),
            titleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            titleLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: nativePanelInset),
            editorFrame.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            editorFrame.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor),
            editorFrame.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 12),
            editorFrame.bottomAnchor.constraint(equalTo: closeButton.topAnchor, constant: -14),
            // One action bar for every child window: the lone dismissal sits at
            // the trailing edge, exactly where a window with two actions puts
            // them, instead of floating in the middle of the panel.
            closeButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            closeButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -nativePanelInset),
        ])
        panel.initialFirstResponder = webView
        webView.setAccessibilityLabel(title)
    }

    /// Lay the viewer out and hand the document to its embedded editor.  The
    /// host owns the window's presentation — one child window with the app
    /// locked until it closes — so this only prepares the content.
    func loadContent() {
        guard !stopped else { return }
        panel.contentView?.layoutSubtreeIfNeeded()
        webView.loadHTMLString(documentHTML, baseURL: nil)
        documentHTML = ""
    }

    @objc private func closeAction(_ sender: Any?) {
        close()
    }

    func windowWillClose(_ notification: Notification) {
        finish()
    }

    func close() {
        finish()
        withoutAnimations { panel.close() }
    }

    private func finish() {
        guard !stopped else { return }
        stopped = true
        panel.delegate = nil
        webView.stopLoading()
        webView.loadHTMLString("", baseURL: nil)
        AppKitNativeLeaf.shared.endChildPanel(panel)
        let completion = onClose
        onClose = nil
        completion?(self)
    }
}

/// URL policy for the official device-code pages. Keep this intentionally
/// narrow: provider authentication may show a login page, but it must never
/// become a general-purpose embedded browser or receive arbitrary local URLs.
private enum NativeProviderAuthPolicy {
    static func validCode(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= 128,
              !value.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f }) else {
            return false
        }
        return value.range(of: #"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"#, options: .regularExpression) != nil
    }

    static func url(provider: String, string: String) -> URL? {
        guard let url = URL(string: string), allows(provider: provider, url: url),
              url.user == nil, url.password == nil, url.fragment == nil else {
            return nil
        }
        return url
    }

    static func callbackURL(provider: String, string: String?) -> URL? {
        guard let string, !string.isEmpty,
              let url = URL(string: string),
              provider == "claude",
              url.scheme?.lowercased() == "http",
              let host = url.host?.lowercased(), host == "localhost" || host == "127.0.0.1",
              url.user == nil, url.password == nil, url.fragment == nil,
              url.path == "/callback",
              let port = url.port, port > 0, port <= 65_535 else {
            return nil
        }
        return url
    }

    static func allows(provider: String, url: URL, callbackURL: URL? = nil) -> Bool {
        if let callbackURL,
           url.scheme?.lowercased() == callbackURL.scheme?.lowercased(),
           url.host?.lowercased() == callbackURL.host?.lowercased(),
           url.port == callbackURL.port,
           url.path == callbackURL.path {
            return true
        }
        guard url.scheme?.lowercased() == "https",
              let host = url.host?.lowercased(),
              !host.isEmpty,
              url.port == nil || url.port == 443 else { return false }
        let path = url.path.isEmpty ? "/" : url.path
        switch provider {
        case "openai":
            // Device-code issuance starts at auth.openai.com/codex/device;
            // login.openai.com and ChatGPT are only accepted for the official
            // first-party redirect chain.
            if host == "auth.openai.com" {
                return path == "/codex/device" || path.hasPrefix("/api/accounts/") || path == "/oauth/authorize" || path == "/oauth/authorize/" || path == "/deviceauth/callback" || path == "/log-in" || path == "/login" || path == "/consent" || path.hasPrefix("/cdn-cgi/")
            }
            if host == "login.openai.com" {
                return path == "/" || path.hasPrefix("/login") || path.hasPrefix("/log-in") || path.hasPrefix("/authorize")
            }
            if host == "chatgpt.com" {
                return path == "/" || path.hasPrefix("/auth") || path.hasPrefix("/login")
            }
            if host == "challenges.cloudflare.com" {
                return path.hasPrefix("/cdn-cgi/") || path.hasPrefix("/turnstile/")
            }
            return false
        case "claude":
            if host == "claude.com" || host == "claude.ai" {
                return path == "/" || path == "/cai/oauth/authorize" || path.hasPrefix("/login") || path.hasPrefix("/oauth") || path.hasPrefix("/auth") || path.hasPrefix("/cai/")
            }
            if host == "platform.claude.com" || host == "console.anthropic.com" || host == "auth.anthropic.com" {
                return path == "/" || path.hasPrefix("/login") || path.hasPrefix("/oauth") || path.hasPrefix("/auth")
            }
            return false
        default:
            return false
        }
    }
}

/// Native official-provider login surface. This deliberately has no script
/// message bridge and no JavaScript that reads page values: the Core
/// auth workflow owns token exchange/polling, while this panel only displays
/// the provider page and the one-time code.
private final class NativeProviderAuthController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let panel: NSPanel

    private let provider: String
    private let url: URL
    private let userCode: String?
    private let callbackURL: URL?
    private let webView: WKWebView
    private var onClose: ((NativeProviderAuthController) -> Void)?
    private var stopped = false

    init(
        provider: String,
        url: URL,
        userCode: String?,
        callbackURL: String?,
        title: String,
        closeTitle: String,
        instructionText: String,
        codeLabelText: String,
        copyLabelText: String,
        blockedMessage: String,
        onClose: @escaping (NativeProviderAuthController) -> Void
    ) {
        self.provider = provider
        self.url = url
        self.userCode = userCode
        self.callbackURL = NativeProviderAuthPolicy.callbackURL(provider: provider, string: callbackURL)
        self.onClose = onClose

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        // Cloudflare's managed challenge and the provider's OAuth page may
        // use a first-party popup/target=_blank during the redirect. Keep it
        // inside this isolated WebView instead of silently dropping it.
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = true
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.customUserAgent = RelayBrowserIdentity.userAgent

        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 860, height: 700),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        super.init()

        configureImmediatePresentation(panel)
        panel.title = title
        panel.minSize = NSSize(width: 640, height: 500)
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        panel.center()

        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.setAccessibilityLabel("Official provider authentication")

        let content = NSView()
        panel.contentView = content

        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = nativeHeadingFont
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let instructionLabel = NSTextField(wrappingLabelWithString: instructionText)
        instructionLabel.font = NSFont.systemFont(ofSize: nativeUIFontSize)
        instructionLabel.textColor = .secondaryLabelColor
        instructionLabel.maximumNumberOfLines = 2

        let codeLabel = NSTextField(labelWithString: codeLabelText)
        codeLabel.font = nativeHeadingFont

        let codeField = NSTextField(labelWithString: userCode ?? "")
        codeField.isSelectable = true
        codeField.isEditable = false
        codeField.font = NSFont.monospacedSystemFont(ofSize: 17, weight: .medium)
        codeField.alignment = .center
        codeField.lineBreakMode = .byTruncatingMiddle
        codeField.wantsLayer = true
        codeField.layer?.cornerRadius = 6
        codeField.layer?.borderWidth = 1
        codeField.layer?.borderColor = NSColor.separatorColor.cgColor
        codeField.layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
        codeField.setAccessibilityLabel("Device code")

        let copyButton = NSButton(title: copyLabelText, target: nil, action: nil)
        copyButton.bezelStyle = .rounded
        copyButton.target = self
        copyButton.action = #selector(copyCode(_:))
        copyButton.setAccessibilityLabel("Copy device code")

        let browserFrame = NSView()
        browserFrame.wantsLayer = true
        browserFrame.layer?.borderColor = NSColor.separatorColor.cgColor
        browserFrame.layer?.borderWidth = 1
        browserFrame.layer?.cornerRadius = 6
        browserFrame.addSubview(webView)
        webView.toolTip = blockedMessage

        let closeButton = NSButton(title: closeTitle, target: self, action: #selector(closeAction(_:)))
        closeButton.bezelStyle = .rounded
        closeButton.keyEquivalent = "\u{1b}"

        let hasUserCode = !(userCode?.isEmpty ?? true)
        if !hasUserCode {
            codeLabel.isHidden = true
            codeField.isHidden = true
            copyButton.isHidden = true
        }
        var controls: [NSView] = [titleLabel, instructionLabel, browserFrame, closeButton]
        if hasUserCode {
            controls.insert(codeLabel, at: 2)
            controls.insert(codeField, at: 3)
            controls.insert(copyButton, at: 4)
        }
        controls.forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview($0)
        }
        var constraints: [NSLayoutConstraint] = [
            titleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: nativePanelInset),
            titleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            titleLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: nativePanelInset),
            instructionLabel.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            instructionLabel.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor),
            instructionLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 4),
            browserFrame.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            browserFrame.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor),
            browserFrame.bottomAnchor.constraint(equalTo: closeButton.topAnchor, constant: -14),
            webView.leadingAnchor.constraint(equalTo: browserFrame.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: browserFrame.trailingAnchor),
            webView.topAnchor.constraint(equalTo: browserFrame.topAnchor),
            webView.bottomAnchor.constraint(equalTo: browserFrame.bottomAnchor),
            // One action bar for every child window: the lone dismissal sits at
            // the trailing edge, exactly where a window with two actions puts
            // them, instead of floating in the middle of the panel.
            closeButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -nativePanelInset),
            closeButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -nativePanelInset),
        ]
        if hasUserCode {
            constraints += [
                codeLabel.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
                codeLabel.topAnchor.constraint(equalTo: instructionLabel.bottomAnchor, constant: 12),
                codeField.leadingAnchor.constraint(equalTo: codeLabel.trailingAnchor, constant: 10),
                codeField.centerYAnchor.constraint(equalTo: codeLabel.centerYAnchor),
                codeField.widthAnchor.constraint(greaterThanOrEqualToConstant: 250),
                codeField.heightAnchor.constraint(equalToConstant: 32),
                copyButton.leadingAnchor.constraint(equalTo: codeField.trailingAnchor, constant: 8),
                copyButton.trailingAnchor.constraint(lessThanOrEqualTo: titleLabel.trailingAnchor),
                copyButton.centerYAnchor.constraint(equalTo: codeField.centerYAnchor),
                browserFrame.topAnchor.constraint(equalTo: codeField.bottomAnchor, constant: 14),
            ]
        } else {
            constraints.append(browserFrame.topAnchor.constraint(equalTo: instructionLabel.bottomAnchor, constant: 14))
        }
        NSLayoutConstraint.activate(constraints)
        panel.initialFirstResponder = webView
    }

    /// Lay the panel out and start the provider page.  The host owns the
    /// window's presentation — one child window with the app locked until it
    /// closes — so this only prepares the content.
    func loadContent() {
        guard !stopped else { return }
        panel.contentView?.layoutSubtreeIfNeeded()
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    @objc private func copyCode(_ sender: Any?) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        guard let userCode, !userCode.isEmpty else { return }
        _ = pasteboard.setString(userCode, forType: .string)
    }

    @objc private func closeAction(_ sender: Any?) { close() }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        // Cloudflare's managed challenge is a first-party iframe. Validate
        // every frame URL, but do not require it to be the main frame.
        guard let targetURL = navigationAction.request.url,
              NativeProviderAuthPolicy.allows(provider: provider, url: targetURL, callbackURL: callbackURL) else {
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        guard navigationResponse.isForMainFrame else {
            decisionHandler(.allow)
            return
        }
        guard let responseURL = navigationResponse.response.url,
              NativeProviderAuthPolicy.allows(provider: provider, url: responseURL, callbackURL: callbackURL) else {
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        guard let targetURL = navigationAction.request.url,
              NativeProviderAuthPolicy.allows(provider: provider, url: targetURL, callbackURL: callbackURL) else {
            return nil
        }
        webView.load(navigationAction.request)
        return nil
    }

    func windowWillClose(_ notification: Notification) { finish() }

    func close() {
        finish()
        withoutAnimations { panel.close() }
    }

    private func finish() {
        guard !stopped else { return }
        stopped = true
        panel.delegate = nil
        webView.navigationDelegate = nil
        webView.stopLoading()
        webView.loadHTMLString("", baseURL: nil)
        AppKitNativeLeaf.shared.endChildPanel(panel)
        let completion = onClose
        onClose = nil
        completion?(self)
    }
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

private final class NativeRelayLoginController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler, URLSessionTaskDelegate {
    private static let embeddedHeaderHeight: CGFloat = 76
    // Embedded login is a region of the React step page. Keep the step
    // progress and actions visible while the native browser occupies only
    // the middle form area.
    private static let embeddedStepTopInset: CGFloat = 92
    private static let embeddedStepBottomInset: CGFloat = 54
    private static let embeddedInitialHeight: CGFloat = 620
    private static let embeddedMinimumWebHeight: CGFloat = 420
    private static let embeddedMaximumWebHeight: CGFloat = 980
    private static let panelActionBarHeight: CGFloat = 44

    /// The login page is supplied by the relay station, so its form and
    /// agreement layout cannot be represented by one fixed window height. The
    /// probe finds the username input through the agreement row and returns
    /// only that interval's height. Page headers, footers, and unrelated
    /// content must not inflate the embedded login window.
    private static let embeddedContentHeightScript = """
    (() => {
      // The station's own announcement dialog covers the form; hide it before
      // measuring so the geometry is the real sign-in layout.
      try { window.__youngRouterLoginSurface?.prepare?.(); } catch {}
      const visible = (node) => {
        if (!(node instanceof Element)) return false;
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      const containsSignInForm = (node) => Boolean(node.querySelector?.('input[type=password],input[autocomplete=current-password]'));
      const attributes = (node) => [
        node.getAttribute?.('name'),
        node.getAttribute?.('id'),
        node.getAttribute?.('placeholder'),
        node.getAttribute?.('autocomplete'),
        node.getAttribute?.('aria-label'),
      ].filter(Boolean).join(' ');
      const textOf = (node) => `${attributes(node)} ${(node.innerText || node.textContent || '')}`.trim();
      const anchorText = (node) => {
        if (!node) return '';
        const scope = node.matches?.('input[type="checkbox"],[role="checkbox"]')
          ? node.closest('label,li,p,section') || node.parentElement || node
          : node;
        return textOf(scope);
      };
      const bounds = (node) => {
        if (!visible(node)) return 0;
        const rect = node.getBoundingClientRect();
        return { top: Math.ceil(rect.top + window.scrollY), bottom: Math.ceil(rect.bottom + window.scrollY) };
      };
      const all = (selector) => Array.from(document.querySelectorAll(selector)).filter(visible);
      const usernamePattern = /username|e-mail|email|user name|用户名|邮箱|账号|账户/iu;
      const passwordPattern = /password|passcode|密码/iu;
      const loginPattern = /sign[ -]?in|log[ -]?in|login|submit|continue|登录|登陆|提交|进入/iu;
      const agreementPattern = /agree|terms|privacy|consent|协议|隐私|同意|用户协议|服务条款|支持地区|专项条款/iu;
      const inputs = all('input,textarea,select');
      const username = inputs.find((node) => {
        if (node.type === 'password' || passwordPattern.test(textOf(node))) return false;
        const hint = `${node.getAttribute?.('autocomplete') || ''} ${textOf(node)}`;
        return usernamePattern.test(hint);
      }) || inputs.find((node) => node.type !== 'password' && !passwordPattern.test(textOf(node)));
      const password = inputs.find((node) => node.type === 'password' || passwordPattern.test(textOf(node)));
      const login = all('button,input[type="submit"],input[type="button"],[role="button"]').find((node) => loginPattern.test(textOf(node)));
      const agreement = [
        ...all('label'),
        ...all('input[type="checkbox"],[role="checkbox"]'),
        ...all('a,p,span'),
      ].find((node) => agreementPattern.test(anchorText(node)));
      const topAnchor = bounds(username || password);
      const submitAnchor = bounds(login);
      const agreementAnchor = bounds(agreement);
      // The window must show the whole sign-in form: from the first field down
      // to the submit button, extended to the agreement row that stations print
      // directly under it (登录即代表同意…). Page headers, footers, and nested
      // content outside that interval must not inflate the login window.
      let lastAnchor = submitAnchor || agreementAnchor;
      if (submitAnchor && agreementAnchor && agreementAnchor.bottom > submitAnchor.bottom && agreementAnchor.top - submitAnchor.bottom <= 160) {
        lastAnchor = agreementAnchor;
      }
      const fixedHeaders = Array.from(document.querySelectorAll('header,nav,[class*="header"],[class*="Header"],[class*="navbar"],[class*="Navbar"]'))
        .filter((node) => visible(node) && !containsSignInForm(node) && ['fixed', 'sticky'].includes(getComputedStyle(node).position));
      // A fixed site header overlays the scrolled form, so the first field is
      // kept clear of the tallest one instead of butting against the top edge.
      const fixedHeaderHeight = fixedHeaders.reduce((height, node) => {
        const rect = node.getBoundingClientRect();
        if (rect.top > 4 || rect.height > 240) return height;
        return Math.max(height, rect.height);
      }, 0);
      const interval = topAnchor && lastAnchor && lastAnchor.bottom > topAnchor.top
        ? lastAnchor.bottom - topAnchor.top + 40
        : 640;
      if (topAnchor && lastAnchor && lastAnchor.bottom > topAnchor.top) {
        const bodyHeight = Math.max(document.body?.scrollHeight || 0, document.documentElement?.scrollHeight || 0);
        const maxScroll = Math.max(0, bodyHeight - Math.max(window.innerHeight, 1));
        const targetScroll = Math.min(maxScroll, Math.max(0, topAnchor.top - fixedHeaderHeight - 12));
        window.scrollTo({ top: targetScroll, left: 0, behavior: 'auto' });
      }
      // A sign-in form is a floor, not a target: the window keeps the first
      // field through the button visible without any scrolling.
      const minimum = topAnchor ? 520 : 420;
      return Math.max(minimum, Math.min(980, Math.ceil(interval)));
    })();
    """

    /// Station sign-in pages are single-page apps: their own announcements
    /// (系统公告 and similar blockers) cover the form, and a successful sign-in
    /// routes client-side without ever reloading the document. This script
    /// hides those blocking overlays, publishes the very same helper to the
    /// height probe, and reports client-side navigation so the controller can
    /// re-run its readiness and sign-in checks.
    private static let relayLoginSurfaceScript = """
    (() => {
      const state = { lastDismissal: 0 };
      const visible = (node) => {
        if (!(node instanceof Element)) return false;
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      const coveredArea = (node) => {
        const rect = node.getBoundingClientRect();
        return rect.width * rect.height;
      };
      // Swift interprets backslash escapes inside its own string literals, so
      // this JS source keeps every regex free of them.
      const whitespacePattern = new RegExp('[' + String.fromCharCode(9, 10, 11, 12, 13, 32) + ']+', 'g');
      const elementText = (node) => `${node.innerText || node.textContent || ''} ${node.getAttribute?.('aria-label') || ''} ${node.getAttribute?.('title') || ''}`.replace(whitespacePattern, ' ').trim();
      // A station announcement often offers both a persistent "close for today"
      // action and a plain close: prefer the persistent one, then the explicit
      // dismissal, and fall back to a bare close affordance last.
      const persistentDismissPattern = /^(?:今日关闭|不再提示|今天不再提示|don't show again|dont show again)$/iu;
      const dismissPattern = /^(?:关闭公告|关闭|我知道了|知道了|明白|确定|好的|暂不|稍后再说|close|dismiss|got it|ok|no thanks)$/iu;
      const signInActionPattern = /登录|登陆|注册|继续|提交|进入|sign in|sign up|log in|login|submit|continue|register/iu;
      const closeAffordancePattern = /(close|dismiss|icon-close)/i;
      const containsSignInForm = (node) => Boolean(node.querySelector?.('input[type=password],input[autocomplete=current-password]'));
      const dismissalControl = (root) => {
        let best = null;
        for (const node of root.querySelectorAll('button,[role="button"],a,[class*="close"],[class*="Close"]')) {
          if (!visible(node) || node.disabled) continue;
          if (String(node.getAttribute?.('type') || '').toLowerCase() === 'submit') continue;
          const text = elementText(node);
          if (text.length > 24) continue;
          // Never press the station's own sign-in, registration, or consent
          // action: only an explicit dismissal affordance is safe here.
          if (text && signInActionPattern.test(text)) continue;
          let rank = 0;
          if (persistentDismissPattern.test(text)) rank = 3;
          else if (dismissPattern.test(text)) rank = 2;
          else {
            const signature = `${node.className || ''} ${node.getAttribute?.('aria-label') || ''} ${node.getAttribute?.('title') || ''}`;
            if (closeAffordancePattern.test(signature)) rank = 1;
          }
          if (rank && (!best || rank > best.rank)) best = { node, rank };
        }
        return best?.node ?? null;
      };
      const revealed = new WeakMap();
      const dismissBlockingOverlays = (force) => {
        const now = Date.now();
        if (!force && now - state.lastDismissal < 250) return false;
        state.lastDismissal = now;
        const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
        const selectors = '[role="dialog"],[aria-modal="true"],[class*="backdrop"],[class*="Backdrop"],[class*="mask"],[class*="Mask"],[class*="modal"],[class*="Modal"],[class*="dialog"],[class*="Dialog"],[class*="overlay"],[class*="Overlay"],[class*="popup"],[class*="Popup"],[class*="notice"],[class*="Notice"]';
        let changed = false;
        for (const node of document.querySelectorAll(selectors)) {
          if (!visible(node) || containsSignInForm(node)) continue;
          const position = getComputedStyle(node).position;
          if (position !== 'fixed' && position !== 'absolute') continue;
          const covered = coveredArea(node);
          if (covered < viewportArea * 0.12) continue;
          const seen = (revealed.get(node) ?? 0) + 1;
          revealed.set(node, seen);
          const control = dismissalControl(node);
          if (control && seen <= 2) {
            try { control.click(); } catch {}
            changed = true;
            continue;
          }
          // A station can reopen an announcement right after it is dismissed
          // (its own close action does not always persist), so a layer that
          // survives the dismissal attempts is hidden outright.
          if (covered >= viewportArea * 0.3) {
            try { node.style.setProperty('display', 'none', 'important'); } catch {}
            changed = true;
          }
        }
        return changed;
      };
      try {
        window.__youngRouterLoginSurface = {
          dismiss: () => dismissBlockingOverlays(false),
          prepare: () => dismissBlockingOverlays(true),
        };
      } catch {}
      const notify = () => {
        try { window.webkit.messageHandlers.litellmRelayPage.postMessage({ kind: 'navigation' }); } catch {}
      };
      const wrap = (name) => {
        const original = window.history?.[name];
        if (typeof original !== 'function') return;
        window.history[name] = function (...args) {
          const result = original.apply(this, args);
          notify();
          return result;
        };
      };
      wrap('pushState');
      wrap('replaceState');
      window.addEventListener('popstate', notify);
      window.addEventListener('hashchange', notify);
      const prepare = () => dismissBlockingOverlays(true);
      prepare();
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', prepare, { once: true });
      }
      new MutationObserver(() => dismissBlockingOverlays(false)).observe(document.documentElement, { childList: true, subtree: true });
      window.setInterval(() => dismissBlockingOverlays(false), 1000);
    })();
    """

    /// The station keeps its session in local storage, and a single-page sign-in
    /// never reloads the document, so no navigation callback announces it. The
    /// watcher polls that storage and returns only a compact signature: a new
    /// credential fingerprint starts one server-side verification.
    private static let loginStateScript = """
    (() => {
      const read = (key) => {
        try {
          const value = window.localStorage.getItem(key);
          return typeof value === 'string' ? value : '';
        } catch { return ''; }
      };
      let token = read('auth_token') || read('access_token') || '';
      if (!token) {
        try {
          const user = JSON.parse(read('user') || 'null');
          const candidate = user && typeof user === 'object' ? (user.token || user.access_token) : '';
          if (typeof candidate === 'string') token = candidate;
        } catch {}
      }
      const loginField = document.querySelector('input[type=password],input[autocomplete=current-password]');
      return {
        token: token ? `${token.length}:${token.slice(-8)}` : '',
        path: String(window.location.pathname || '') + String(window.location.search || ''),
        form: Boolean(loginField && loginField.getBoundingClientRect().height > 0),
      };
    })();
    """

    private static let immediateWebPresentationScript = """
    (() => {
      const styleID = '__young_router_immediate_presentation';
      const gradientPattern = /gradient[(]/i;
      const imageProperties = ['background-image', 'border-image-source', 'mask-image'];
      const splitImageLayers = (value) => {
        const layers = [];
        let depth = 0;
        let start = 0;
        for (let index = 0; index < value.length; index += 1) {
          const character = value[index];
          if (character === '(') depth += 1;
          else if (character === ')') depth = Math.max(0, depth - 1);
          else if (character === ',' && depth === 0) {
            layers.push(value.slice(start, index).trim());
            start = index + 1;
          }
        }
        layers.push(value.slice(start).trim());
        return layers;
      };
      const stripGradients = (root = document.documentElement) => {
        const elements = root instanceof Element
          ? [root, ...root.querySelectorAll('*')]
          : [...document.querySelectorAll('*')];
        for (const element of elements) {
          const computed = getComputedStyle(element);
          for (const property of imageProperties) {
            const image = computed.getPropertyValue(property);
            if (!gradientPattern.test(image)) continue;
            const retained = splitImageLayers(image).filter((layer) => !gradientPattern.test(layer));
            element.style.setProperty(property, retained.length ? retained.join(', ') : 'none', 'important');
          }
        }
      };
      const install = () => {
        if (document.getElementById(styleID)) return;
        const style = document.createElement('style');
        style.id = styleID;
        style.textContent = `
          html { scroll-behavior: auto !important; }
          html, body, * { scrollbar-width: none !important; }
          *::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }
          *, *::before, *::after {
            animation: none !important;
            transition: none !important;
            scroll-behavior: auto !important;
            view-transition-name: none !important;
          }
        `;
        (document.head || document.documentElement).appendChild(style);
      };
      install();
      stripGradients();
      document.addEventListener('DOMContentLoaded', () => { install(); stripGradients(); }, { once: true });
      new MutationObserver((records) => {
        install();
        records.forEach((record) => {
          if (record.type === 'attributes') stripGradients(record.target);
          else record.addedNodes.forEach((node) => { if (node.nodeType === Node.ELEMENT_NODE) stripGradients(node); });
        });
      }).observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['class', 'style'],
        childList: true,
        subtree: true,
      });
    })();
    """

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
    private let mode: NativeRelayBrowserMode
    private lazy var session = URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)
    private let panel: NSPanel?
    private weak var embeddedWindow: NSWindow?
    /// The window this sign-in browser locks while it is up; nil for the
    /// embedded step, which lives inside the wizard's own window.
    private weak var presentationParent: NSWindow?
    private let embeddedClose: (() -> Void)?
    private let pendingAccount: Bool
    private let stationID: String?
    private let stationName: String?
    private let stationType: String?
    private let stationOrigin: String?
    private var embeddedContent: NSView?
    private var embeddedCloseObserver: NSObjectProtocol?
    private let webView: WKWebView
    private let loadingOverlay = NSVisualEffectView()
    private let loadingLabel = NSTextField(labelWithString: "")
    private let statusLabel = NSTextField(labelWithString: "")
    private let accountLabel = NSTextField(labelWithString: "")
    private let signInButton = NSButton(title: "Sign In", target: nil, action: nil)
    private let cancelButton = NSButton(title: "", target: nil, action: nil)
    private var finished = false
    private var checking = false
    private var result: CoreIPCBridge.RelayLoginResult?
    private var capturedAccessToken: String?
    private var capturedRefreshToken: String?
    private var capturedUserID: String?
    private var capturedPassword: String?
    private var restoredSession: NativeRelaySession?
    private var didRestoreSession = false
    private var didLoadInitialPage = false
    private var didProbeRestoredSession = false
    private var pageReadinessProbe: DispatchWorkItem?
    private var loginFormRevealProbe: DispatchWorkItem?
    private var loginFormRevealAttempts = 0
    private var didRevealLoginField = false
    private var agreementRevealProbe: DispatchWorkItem?
    private var agreementRevealAttempts = 0
    private var embeddedResizeProbe: DispatchWorkItem?
    private var embeddedResizeAttempts = 0
    private var lastEmbeddedContentHeight: CGFloat?
    private var automaticCheckProbe: DispatchWorkItem?
    private var loginWatchProbe: DispatchWorkItem?
    private var observedLoginSignature: String?
    private var observedLoginForm: Bool?
    private var checkStartedAt: Date?
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
        embeddedWindow: NSWindow? = nil,
        embeddedClose: (() -> Void)? = nil,
        presentationParent: NSWindow? = nil,
        pendingAccount: Bool = false,
        stationID: String? = nil,
        stationName: String? = nil,
        stationType: String? = nil,
        stationOrigin: String? = nil,
        mode: NativeRelayBrowserMode = .login
    ) {
        self.accountID = accountID
        self.type = type
        self.label = label
        self.originURL = originURL
        self.language = language
        self.presetUsername = username?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.mode = mode
        self.embeddedWindow = embeddedWindow
        self.embeddedClose = embeddedClose
        self.presentationParent = presentationParent
        self.pendingAccount = pendingAccount
        self.stationID = stationID
        self.stationName = stationName
        self.stationType = stationType
        self.stationOrigin = stationOrigin

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.userContentController.addUserScript(
            WKUserScript(
                source: Self.immediateWebPresentationScript,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: false
            )
        )
        if mode == .login {
            configuration.userContentController.addUserScript(
                WKUserScript(
                    source: """
                    (() => {
                      const selector = 'input[type=password],input[autocomplete=current-password]';
                      const capture = (node) => {
                        const value = node?.value;
                        if (typeof value === 'string' && value.length) {
                          const password = value.slice(0, 4096);
                          sessionStorage.setItem('__young_router_relay_password', password);
                          try { window.webkit.messageHandlers.litellmRelayPassword.postMessage(password); } catch {}
                        }
                      };
                      document.addEventListener('input', (event) => {
                        if (event.target?.matches?.(selector)) capture(event.target);
                      }, true);
                      document.addEventListener('change', (event) => {
                        if (event.target?.matches?.(selector)) capture(event.target);
                      }, true);
                      document.addEventListener('submit', (event) => {
                        capture(event.target?.querySelector?.(selector));
                      }, true);
                    })();
                    """,
                    injectionTime: .atDocumentStart,
                    forMainFrameOnly: true
                )
            )
            configuration.userContentController.addUserScript(
                WKUserScript(
                    source: Self.relayLoginSurfaceScript,
                    injectionTime: .atDocumentStart,
                    forMainFrameOnly: true
                )
            )
        }
        webView = WKWebView(frame: .zero, configuration: configuration)
        // The one browser identity this app presents.  A station can bind a
        // session to its IP and User-Agent (sub2api's session binding), so the
        // page that creates the session must send the same identity as every
        // later probe, dashboard read, and usage call — otherwise the session
        // it established is revoked on the first request this app makes.
        webView.customUserAgent = RelayBrowserIdentity.userAgent
        panel = embeddedWindow == nil ? NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 700),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        ) : nil
        super.init()
        if mode == .login {
            configuration.userContentController.add(self, name: "litellmRelayPassword")
            configuration.userContentController.add(self, name: "litellmRelayPage")
        }
        buildPanel()
    }

    func start(completion: @escaping (CoreIPCBridge.RelayLoginResult?) -> Void) {
        self.completion = completion
        beginBrowserFlow()
        if let panel {
            // Child window presentation: its own movable window over the app,
            // which stays locked until the sign-in flow ends. The page loads
            // first and the modal session runs last, so the station's form is
            // already on its way while the window settles.
            panel.center()
            configureImmediatePresentation(panel)
            withoutAnimations { panel.makeKeyAndOrderFront(nil) }
        } else if let embeddedWindow {
            // Start with a generous fallback, then resize from the actual
            // page geometry once the station's login form has rendered.
            embeddedWindow.setContentSize(NSSize(width: 900, height: Self.embeddedInitialHeight))
            configureImmediatePresentation(embeddedWindow)
            embeddedCloseObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification,
                object: embeddedWindow,
                queue: .main
            ) { [weak self] _ in
                self?.cancel(nil)
            }
            withoutAnimations { embeddedWindow.makeKeyAndOrderFront(nil) }
        }
        restoreSessionAndLoad()
        scheduleEmbeddedBrowserResize()
        if let panel {
            AppKitNativeLeaf.shared.presentChildPanel(panel, in: presentationParent ?? AppKitNativeLeaf.shared.activeWindow())
        }
    }

    /// Each controller represents one browser login flow. Clear temporary
    /// session captures before its initial navigation.
    private func beginBrowserFlow() {
        _ = activeCheck?.requestCancellation()
        activeCheck = nil
        loginWatchProbe?.cancel()
        loginWatchProbe = nil
        observedLoginSignature = nil
        observedLoginForm = nil
        checkStartedAt = nil
        automaticCheckProbe?.cancel()
        automaticCheckProbe = nil
        embeddedResizeProbe?.cancel()
        embeddedResizeProbe = nil
        embeddedResizeAttempts = 0
        lastEmbeddedContentHeight = nil
        clearCapturedCredentials()
    }

    private var loginURL: URL {
        if mode == .logs {
            return relayURL(path: type == "newapi" ? "usage-logs" : "usage") ?? originURL
        }
        // Both relay families expose the sign-in form at /login; landing
        // there skips the marketing home page and its separate 登录 link.
        return originURL.appendingPathComponent("login")
    }

    private var localizedChinese: Bool {
        language == "zh-Hans" || (language == "system" && Locale.preferredLanguages.first?.lowercased().hasPrefix("zh") == true)
    }

    private func text(_ english: String, _ chinese: String) -> String {
        localizedChinese ? chinese : english
    }

    private var waitingForSignInStatus: String {
        text("Waiting for sign-in...", "等待完成登录...")
    }

    private func setStatus(_ value: String) {
        guard statusLabel.stringValue != value else { return }
        statusLabel.stringValue = value
    }

    private var isEmbeddedPresentation: Bool { embeddedWindow != nil }

    /// The sign-in browser is a region the app sizes around the station's own
    /// page: the embedded step page and the child window laid out over the
    /// workspace. Each measures that page's geometry so the first field through
    /// the submit button stays visible.
    private var measuresPageGeometry: Bool { panel != nil || isEmbeddedPresentation }

    private func scheduleEmbeddedBrowserResize(delay: TimeInterval = 0.15) {
        guard measuresPageGeometry, isBrowserFlowLive, embeddedResizeAttempts < 16 else { return }
        embeddedResizeProbe?.cancel()
        embeddedResizeAttempts += 1
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.isBrowserFlowLive, self.measuresPageGeometry else { return }
            self.webView.evaluateJavaScript(Self.embeddedContentHeightScript) { [weak self] value, _ in
                guard let self, self.isBrowserFlowLive, self.measuresPageGeometry else { return }
                if let number = value as? NSNumber, number.doubleValue.isFinite {
                    self.resizeEmbeddedBrowser(contentHeight: CGFloat(number.doubleValue))
                }
                if self.embeddedResizeAttempts < 16 {
                    self.scheduleEmbeddedBrowserResize(delay: 0.35)
                }
            }
        }
        embeddedResizeProbe = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func resizeEmbeddedBrowser(contentHeight: CGFloat) {
        guard contentHeight.isFinite else { return }
        let webViewHeight = min(Self.embeddedMaximumWebHeight, max(Self.embeddedMinimumWebHeight, ceil(contentHeight)))
        if isEmbeddedPresentation {
            guard let embeddedWindow else { return }
            let height = webViewHeight + Self.embeddedStepTopInset + Self.embeddedStepBottomInset
            guard acceptsMeasuredHeight(height) else { return }
            embeddedWindow.setContentSize(NSSize(width: 900, height: height))
            return
        }
        // The panel keeps its generous default height and only grows when the
        // station's own form needs more room than the window already shows, so
        // opening one never jumps in size.
        guard let panel else { return }
        let chrome = Self.embeddedHeaderHeight + Self.panelActionBarHeight
        let height = min(maximumPanelHeight(), webViewHeight + chrome)
        guard height > panel.contentLayoutRect.height + 8 else { return }
        guard acceptsMeasuredHeight(height) else { return }
        panel.setContentSize(NSSize(width: 900, height: height))
    }

    private func acceptsMeasuredHeight(_ height: CGFloat) -> Bool {
        if let lastEmbeddedContentHeight, abs(lastEmbeddedContentHeight - height) < 8 { return false }
        lastEmbeddedContentHeight = height
        return true
    }

    private func maximumPanelHeight() -> CGFloat {
        let visibleHeight = (panel?.screen ?? NSScreen.main)?.visibleFrame.height ?? 700
        return max(560, visibleHeight - 140)
    }

    private func buildPanel() {
        let content = NSView()
        let header = NSView()
        webView.navigationDelegate = self
        let titleLabel = NSTextField(labelWithString: isEmbeddedPresentation
            ? text("1 Relay URL  ›  2 Sign in", "1 中转站 URL  ›  2 登录")
            : label)
        let showsReloadAction = mode == .logs && !isEmbeddedPresentation
        // The React step owns the progress header and close action. The
        // embedded browser must not add a second native panel header.
        let showsEmbeddedClose = false
        // The window carries the title-bar close button every child surface
        // has; the explicit Close action stays beside it as the login flow's
        // own way out (and its Esc shortcut), exactly like the model chooser's
        // Cancel.
        let showsPanelClose = panel != nil && mode == .login && !isEmbeddedPresentation
        let showsPanelActions = showsReloadAction || showsEmbeddedClose || showsPanelClose
        titleLabel.font = nativeHeadingFont
        titleLabel.lineBreakMode = .byTruncatingTail
        let host = originURL.host ?? ""
        // The host subtitle is redundant when the title already shows the URL.
        let showsHostSubtitle = isEmbeddedPresentation || mode == .logs || host.isEmpty || !label.localizedCaseInsensitiveContains(host)
        accountLabel.stringValue = isEmbeddedPresentation
            ? text("Sign-in is detected automatically", "登录成功后自动检测")
            : (originURL.host ?? originURL.absoluteString)
        accountLabel.isHidden = !showsHostSubtitle
        accountLabel.textColor = .secondaryLabelColor
        statusLabel.stringValue = isEmbeddedPresentation
            ? waitingForSignInStatus
            : mode == .logs
                ? text("Showing the relay site's usage logs.", "正在显示中转站用量日志。")
                : text("Complete sign-in in the page; success is detected automatically.", "请在页面中完成登录，成功后将自动检测。")
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.lineBreakMode = .byTruncatingTail
        signInButton.isHidden = !showsReloadAction
        cancelButton.isHidden = !showsPanelActions
        if showsReloadAction {
            signInButton.title = text("Reload", "刷新")
            signInButton.target = self
            signInButton.action = #selector(reloadBrowser(_:))
            signInButton.bezelStyle = .rounded
            signInButton.keyEquivalent = "\r"
        }
        if showsPanelActions {
            cancelButton.title = text("Close", "关闭")
            cancelButton.target = self
            cancelButton.action = showsEmbeddedClose ? #selector(closeEmbeddedWindow(_:)) : #selector(cancel(_:))
            cancelButton.bezelStyle = .rounded
            cancelButton.keyEquivalent = "\u{1b}"
        }
        [titleLabel, accountLabel, statusLabel].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            header.addSubview($0)
        }
        // The explicit Close action lives in a fixed bottom-right action bar,
        // matching the route-window convention; only the logs header keeps
        // inline header buttons.
        let panelActionBar = showsPanelClose ? NSView() : nil
        let panelActionSeparator = showsPanelClose ? NSBox() : nil
        panelActionSeparator?.boxType = .separator
        if showsReloadAction {
            ([signInButton, cancelButton] as [NSButton?]).compactMap { $0 }.forEach {
                $0.translatesAutoresizingMaskIntoConstraints = false
                header.addSubview($0)
            }
        } else if let actionBar = panelActionBar {
            actionBar.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(actionBar)
            cancelButton.translatesAutoresizingMaskIntoConstraints = false
            actionBar.addSubview(cancelButton)
            if let separator = panelActionSeparator {
                separator.translatesAutoresizingMaskIntoConstraints = false
                actionBar.addSubview(separator)
            }
        }
        webView.translatesAutoresizingMaskIntoConstraints = false
        loadingOverlay.material = .contentBackground
        loadingOverlay.blendingMode = .withinWindow
        loadingOverlay.state = .active
        loadingOverlay.translatesAutoresizingMaskIntoConstraints = false
        loadingLabel.stringValue = mode == .logs
            ? text("Loading usage logs...", "正在加载用量日志...")
            : text("Loading sign-in page...", "正在加载登录页面...")
        loadingLabel.textColor = .secondaryLabelColor
        loadingLabel.alignment = .center
        loadingLabel.translatesAutoresizingMaskIntoConstraints = false
        loadingOverlay.addSubview(loadingLabel)
        if !isEmbeddedPresentation {
            content.addSubview(header)
        }
        content.addSubview(webView)
        content.addSubview(loadingOverlay)
        header.translatesAutoresizingMaskIntoConstraints = false
        if let panel {
            configureImmediatePresentation(panel)
            panel.title = mode == .logs
                ? text("Relay Usage Logs", "中转站用量日志")
                : text("Relay Account Sign In", "中转站账号登录")
            panel.minSize = NSSize(width: 720, height: 560)
            panel.isReleasedWhenClosed = false
            panel.delegate = self
            panel.contentView = content
        } else if let embeddedWindow, let hostContent = embeddedWindow.contentView {
            content.wantsLayer = true
            content.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
            content.translatesAutoresizingMaskIntoConstraints = false
            hostContent.addSubview(content, positioned: .above, relativeTo: nil)
            NSLayoutConstraint.activate([
                content.leadingAnchor.constraint(equalTo: hostContent.leadingAnchor),
                content.trailingAnchor.constraint(equalTo: hostContent.trailingAnchor),
                content.topAnchor.constraint(equalTo: hostContent.topAnchor, constant: Self.embeddedStepTopInset),
                content.bottomAnchor.constraint(equalTo: hostContent.bottomAnchor, constant: -Self.embeddedStepBottomInset),
            ])
            embeddedContent = content
        }

        var constraints: [NSLayoutConstraint] = [
            webView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            loadingOverlay.leadingAnchor.constraint(equalTo: webView.leadingAnchor),
            loadingOverlay.trailingAnchor.constraint(equalTo: webView.trailingAnchor),
            loadingOverlay.topAnchor.constraint(equalTo: webView.topAnchor),
            loadingOverlay.bottomAnchor.constraint(equalTo: webView.bottomAnchor),
            loadingLabel.centerXAnchor.constraint(equalTo: loadingOverlay.centerXAnchor),
            loadingLabel.centerYAnchor.constraint(equalTo: loadingOverlay.centerYAnchor),
        ]
        if let actionBar = panelActionBar {
            constraints += [
                webView.bottomAnchor.constraint(equalTo: actionBar.topAnchor),
                actionBar.leadingAnchor.constraint(equalTo: content.leadingAnchor),
                actionBar.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                actionBar.bottomAnchor.constraint(equalTo: content.bottomAnchor),
                actionBar.heightAnchor.constraint(equalToConstant: Self.panelActionBarHeight),
                cancelButton.leadingAnchor.constraint(greaterThanOrEqualTo: actionBar.leadingAnchor, constant: nativePanelInset),
                cancelButton.trailingAnchor.constraint(equalTo: actionBar.trailingAnchor, constant: -nativePanelInset),
                cancelButton.centerYAnchor.constraint(equalTo: actionBar.centerYAnchor),
                cancelButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 76),
            ]
            if let separator = panelActionSeparator {
                constraints += [
                    separator.leadingAnchor.constraint(equalTo: actionBar.leadingAnchor),
                    separator.trailingAnchor.constraint(equalTo: actionBar.trailingAnchor),
                    separator.topAnchor.constraint(equalTo: actionBar.topAnchor),
                ]
            }
        } else {
            constraints.append(webView.bottomAnchor.constraint(equalTo: content.bottomAnchor))
        }
        if isEmbeddedPresentation {
            constraints.append(webView.topAnchor.constraint(equalTo: content.topAnchor))
        } else {
            let headerHeight: CGFloat = Self.embeddedHeaderHeight
            constraints += [
                header.leadingAnchor.constraint(equalTo: content.leadingAnchor),
                header.trailingAnchor.constraint(equalTo: content.trailingAnchor),
                header.topAnchor.constraint(equalTo: content.topAnchor),
                header.heightAnchor.constraint(equalToConstant: headerHeight),
                webView.topAnchor.constraint(equalTo: header.bottomAnchor),
            ]
        }
        if showsReloadAction {
            constraints += [
                titleLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: nativePanelInset),
                titleLabel.topAnchor.constraint(equalTo: header.topAnchor, constant: 12),
                titleLabel.trailingAnchor.constraint(lessThanOrEqualTo: cancelButton.leadingAnchor, constant: -16),
                accountLabel.leadingAnchor.constraint(equalTo: titleLabel.trailingAnchor, constant: 10),
                accountLabel.centerYAnchor.constraint(equalTo: titleLabel.centerYAnchor),
                accountLabel.trailingAnchor.constraint(lessThanOrEqualTo: cancelButton.leadingAnchor, constant: -16),
                statusLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: nativePanelInset),
                statusLabel.trailingAnchor.constraint(lessThanOrEqualTo: signInButton.leadingAnchor, constant: -16),
                statusLabel.bottomAnchor.constraint(equalTo: header.bottomAnchor, constant: -12),
                signInButton.trailingAnchor.constraint(equalTo: cancelButton.leadingAnchor, constant: -8),
                signInButton.centerYAnchor.constraint(equalTo: header.centerYAnchor),
                signInButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 104),
                cancelButton.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -nativePanelInset),
                cancelButton.centerYAnchor.constraint(equalTo: header.centerYAnchor),
                cancelButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 76),
            ]
        } else if showsEmbeddedClose {
            constraints += [
                titleLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: nativePanelInset),
                titleLabel.topAnchor.constraint(equalTo: header.topAnchor, constant: 12),
                titleLabel.trailingAnchor.constraint(lessThanOrEqualTo: cancelButton.leadingAnchor, constant: -16),
                accountLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: nativePanelInset),
                accountLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 4),
                accountLabel.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -nativePanelInset),
                statusLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: nativePanelInset),
                statusLabel.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -nativePanelInset),
                statusLabel.bottomAnchor.constraint(equalTo: header.bottomAnchor, constant: -12),
                cancelButton.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -nativePanelInset),
                cancelButton.topAnchor.constraint(equalTo: header.topAnchor, constant: 10),
                cancelButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 76),
            ]
        } else if !isEmbeddedPresentation {
            constraints += [
                titleLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: nativePanelInset),
                titleLabel.topAnchor.constraint(equalTo: header.topAnchor, constant: 12),
                titleLabel.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -nativePanelInset),
                accountLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: nativePanelInset),
                accountLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 4),
                accountLabel.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -nativePanelInset),
                statusLabel.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: nativePanelInset),
                statusLabel.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -nativePanelInset),
                statusLabel.bottomAnchor.constraint(equalTo: header.bottomAnchor, constant: -12),
            ]
        }
        NSLayoutConstraint.activate(constraints)
    }

    private func restoreSessionAndLoad() {
        guard isBrowserFlowLive, !didLoadInitialPage else { return }
        didLoadInitialPage = true
        restoredSession = NativeRelaySessionMemoryStore.readSession(
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
          const username = \(jsonLiteral(presetUsername ?? ""));
          if (accessToken) {
            localStorage.setItem('auth_token', accessToken);
            localStorage.setItem('access_token', accessToken);
            try {
              const current = JSON.parse(localStorage.getItem('user') || 'null');
              const user = current && typeof current === 'object' && !Array.isArray(current) ? current : {};
              user.token = accessToken;
              if (username && !user.username) user.username = username;
              localStorage.setItem('user', JSON.stringify(user));
            } catch {}
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
                self.prefillLoginWhenReady()
                self.probeRestoredSession()
            } else {
                self.webView.reload()
            }
        }
    }

    private func probeRestoredSession() {
        guard mode == .login, restoredSession != nil, !didProbeRestoredSession, !checking else { return }
        didProbeRestoredSession = true
        startSignInCheck(automatically: true)
    }

    private func prefillLoginWhenReady() {
        guard mode == .login, isBrowserFlowLive, let username = presetUsername, !username.isEmpty else { return }
        // Let a restored session seed local storage and reload before looking
        // for a login form. Otherwise a restored session could be sent straight
        // back to the unauthenticated form during its first page load.
        guard restoredSession == nil || didRestoreSession else { return }
        let safeUser = jsonLiteral(username)
        let script = """
        (() => {
          const user = document.querySelector('input[type=email], input[type=text], input:not([type]), input[name=email], input[name=username], input[autocomplete=username], input[placeholder*="用户名"], input[placeholder*="email" i]');
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
          return Boolean(user?.value);
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
        startSignInCheck(automatically: false)
    }

    private func startSignInCheck(automatically: Bool) {
        guard !finished, !checking else { return }
        let attempt = NativeRelayLoginAttempt()
        activeCheck = attempt
        checking = true
        checkStartedAt = Date()
        panelClosedDuringCommit = false
        automaticCheckProbe?.cancel()
        automaticCheckProbe = nil
        capturedAccessToken = nil
        capturedRefreshToken = nil
        signInButton.isEnabled = false
        cancelButton.isEnabled = true
        if !automatically {
            setStatus(text("Checking sign-in...", "正在验证登录..."))
        }
        captureBrowserCredentials(attempt: attempt) { [weak self, weak attempt] in
            guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
            self.collectCookies(attempt: attempt) { [weak self, weak attempt] cookieHeader in
                guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                guard !automatically || cookieHeader != nil || self.capturedAccessToken != nil else {
                    self.finishCheckingFailure(
                        text("Waiting for sign-in...", "等待完成登录..."),
                        attempt: attempt,
                        quiet: true
                    )
                    return
                }
                self.probe(index: 0, cookieHeader: cookieHeader, attempt: attempt, automatically: automatically)
            }
        }
    }

    private func isCurrentCheck(_ attempt: NativeRelayLoginAttempt) -> Bool {
        !finished && checking && activeCheck === attempt && attempt.isActive()
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard mode == .login, message.frameInfo.isMainFrame else { return }
        if message.name == "litellmRelayPage" {
            // The page routed itself: the sign-in step may just have been
            // replaced by the station console, and the form geometry changed
            // with it.
            embeddedResizeAttempts = 0
            scheduleEmbeddedBrowserResize(delay: 0.05)
            schedulePageReadinessProbe()
            scheduleAgreementReveal()
            scheduleLoginFormReveal()
            scheduleLoginWatch(delay: 0.6)
            return
        }
        // The capture always runs so the post-login prompt can offer to keep
        // the typed password; whether it is persisted is decided later.
        guard message.name == "litellmRelayPassword",
              message.frameInfo.isMainFrame,
              message.frameInfo.securityOrigin.host.lowercased() == originURL.host?.lowercased(),
              let password = message.body as? String,
              !password.isEmpty,
              password.utf8.count <= 4_096 else { return }
        capturedPassword = password
    }

    private var isBrowserFlowLive: Bool {
        !finished && !panelClosedDuringCommit
    }

    private func captureBrowserCredentials(attempt: NativeRelayLoginAttempt, completion: @escaping () -> Void) {
        let passwordExpression = mode == .login
            ? "sessionStorage.getItem('__young_router_relay_password') || document.querySelector('input[type=password],input[autocomplete=current-password]')?.value || ''"
            : "''"
        let script = """
        (() => ({
          accessToken: (() => {
            const direct = localStorage.getItem('auth_token') || localStorage.getItem('access_token') || '';
            if (direct) return direct;
            try {
              const user = JSON.parse(localStorage.getItem('user') || 'null');
              return user && typeof user.token === 'string' ? user.token : '';
            } catch { return ''; }
          })(),
          refreshToken: localStorage.getItem('refresh_token') || '',
          userID: (() => {
            try {
              const user = JSON.parse(localStorage.getItem('user') || 'null');
              const value = user && typeof user === 'object' ? user.id : null;
              return typeof value === 'number' || typeof value === 'string' ? String(value).slice(0, 32) : '';
            } catch { return ''; }
          })(),
          password: \(passwordExpression)
        }))();
        """
        webView.evaluateJavaScript(script) { [weak self, weak attempt] value, _ in
            DispatchQueue.main.async {
                guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                if let fields = value as? [String: Any] {
                    self.capturedAccessToken = (fields["accessToken"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                    self.capturedRefreshToken = (fields["refreshToken"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                    self.capturedUserID = (fields["userID"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                    self.capturedPassword = (fields["password"] as? String).flatMap { $0.isEmpty ? nil : $0 }
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

    private func probe(index: Int, cookieHeader: String?, attempt: NativeRelayLoginAttempt, automatically: Bool = false) {
        guard isCurrentCheck(attempt) else { return }
        guard index < probes.count else {
            finishCheckingFailure(
                text("No valid sign-in was found. Complete sign-in in the page and try again.", "未检测到有效登录状态。请完成登录后重试。"),
                attempt: attempt,
                quiet: automatically
            )
            return
        }
        let probe = probes[index]
        guard let url = relayURL(path: probe.path),
              sameOrigin(url) else {
            self.probe(index: index + 1, cookieHeader: cookieHeader, attempt: attempt, automatically: automatically)
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = probe.path.hasSuffix("auth/refresh") ? "POST" : "GET"
        request.timeoutInterval = 12
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(RelayBrowserIdentity.acceptLanguage, forHTTPHeaderField: "Accept-Language")
        request.setValue(RelayBrowserIdentity.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(originHeader, forHTTPHeaderField: "Origin")
        request.setValue(originHeader, forHTTPHeaderField: "Referer")
        if let cookieHeader, !cookieHeader.isEmpty { request.setValue(cookieHeader, forHTTPHeaderField: "Cookie") }
        let probeAccessToken = capturedAccessToken ?? restoredSession?.accessToken
        if let probeAccessToken, !probeAccessToken.isEmpty {
            request.setValue("Bearer \(probeAccessToken)", forHTTPHeaderField: "Authorization")
        }
        // New API forks that require the account header reject the request
        // without it; the id comes from the page's own session record, so it
        // always matches the account the token belongs to.
        if type == "newapi", let capturedUserID, !capturedUserID.isEmpty {
            request.setValue(capturedUserID, forHTTPHeaderField: "New-Api-User")
        }
        session.dataTask(with: request) { [weak self, weak attempt] data, response, _ in
            guard let self, let attempt, attempt.isActive() else { return }
            guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode),
                  let data, data.count <= 2 * 1024 * 1024,
                  let object = try? JSONSerialization.jsonObject(with: data) else {
                DispatchQueue.main.async { [weak self, weak attempt] in
                    guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                    self.probe(index: index + 1, cookieHeader: cookieHeader, attempt: attempt, automatically: automatically)
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
                    self.probe(index: index + 1, cookieHeader: cookieHeader, attempt: attempt, automatically: automatically)
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
        let capturedPassword = self.capturedPassword
        if let capturedPassword, !capturedPassword.isEmpty {
            // The typed password is a decision point, not a default: ask the
            // user whether to keep it after the sign-in was verified.
            presentRememberPasswordPrompt { [weak self, weak attempt] rememberPassword in
                guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                self.commitVerifiedLogin(
                    username: username,
                    cookie: cookie,
                    accessToken: accessToken,
                    refreshToken: refreshToken,
                    rememberPassword: rememberPassword,
                    capturedPassword: capturedPassword,
                    attempt: attempt
                )
            }
            return
        }
        // No password was typed (cookie/token sign-in). Nothing to keep, so
        // the account's existing preference stands and no prompt appears.
        commitVerifiedLogin(
            username: username,
            cookie: cookie,
            accessToken: accessToken,
            refreshToken: refreshToken,
            rememberPassword: nil,
            capturedPassword: nil,
            attempt: attempt
        )
    }

    /// Post-login subordinate prompt on the sign-in surface: keep the typed
    /// password on this device, or keep only the current login state.  It is the
    /// app's decision surface rather than an attached sheet — the same panel every
    /// other question uses — and the sign-in window is locked while it is up, so
    /// the answer always lands before the commit begins.
    private func presentRememberPasswordPrompt(completion: @escaping (Bool) -> Void) {
        guard let promptParent = embeddedWindow ?? panel else {
            completion(false)
            return
        }
        AppKitNativeLeaf.shared.presentDecisionPanel(
            text("Remember the password?", "是否记住密码？"),
            message: text(
                "Save the password on this device to enable automatic sign-in next time. Choose “Session only” to keep just the current sign-in state.",
                "密码将保存到本机，下次可自动登录。选择「仅记住登录态」则只保留本次登录状态。"
            ),
            answers: [
                NativeDecisionAnswer(id: "session", title: text("Session Only", "仅记住登录态"), isCancel: true),
                NativeDecisionAnswer(id: "remember", title: text("Remember Password", "记住密码"), isDefault: true)
            ],
            in: promptParent
        ) { answer in
            completion(answer == "remember")
        }
    }

    private func commitVerifiedLogin(
        username: String,
        cookie: String?,
        accessToken: String?,
        refreshToken: String?,
        rememberPassword: Bool?,
        capturedPassword: String?,
        attempt: NativeRelayLoginAttempt
    ) {
        guard isCurrentCheck(attempt) else { return }
        let accountID = self.accountID
        let accountType = self.type
        let accountLabel = self.label
        let origin = self.originURL.absoluteString
        let session = NativeRelaySession(
            accountType: accountType,
            origin: origin,
            cookie: cookie ?? "",
            accessToken: accessToken ?? "",
            refreshToken: refreshToken ?? ""
        )
        let attachFailure = text("The signed-in session could not be saved.", "无法保存登录状态。")
        let pendingAccount = self.pendingAccount
        let stationID = self.stationID
        let stationName = self.stationName
        let stationType = self.stationType
        let stationOrigin = self.stationOrigin
        DispatchQueue.global(qos: .userInitiated).async { [weak self, weak attempt] in
            guard let attempt, attempt.isActive() else { return }
            guard attempt.beginCommit() else { return }
            DispatchQueue.main.async { [weak self, weak attempt] in
                guard let self, let attempt, self.isCurrentCheck(attempt) else { return }
                self.cancelButton.isEnabled = false
                self.statusLabel.stringValue = self.text("Saving sign-in...", "正在保存登录状态...")
            }
            let previousSession = NativeRelaySessionMemoryStore.readSession(
                accountID: accountID,
                accountType: accountType,
                origin: origin
            )
            NativeRelaySessionMemoryStore.writeSession(session, accountID: accountID)
            do {
                let accepted = try CoreIPCBridge.shared.acceptRelayLogin(
                    accountID: accountID,
                    type: accountType,
                    label: accountLabel,
                    origin: origin,
                    username: username,
                    cookie: cookie,
                    accessToken: accessToken,
                    refreshToken: refreshToken,
                    password: rememberPassword == true ? capturedPassword : nil,
                    stationID: stationID,
                    stationName: stationName,
                    stationType: stationType,
                    stationOrigin: stationOrigin,
                    rememberPassword: rememberPassword,
                    pendingAccount: pendingAccount
                )
                DispatchQueue.main.async { [weak self, weak attempt] in
                    guard let self, let attempt else { return }
                    self.finish(accepted, session: session, attempt: attempt)
                }
            } catch {
                if let previousSession {
                    NativeRelaySessionMemoryStore.writeSession(previousSession, accountID: accountID)
                } else {
                    NativeRelaySessionMemoryStore.clear(accountID: accountID)
                }
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
        checkStartedAt = nil
        finished = true
        embeddedResizeProbe?.cancel()
        embeddedResizeProbe = nil
        clearCapturedCredentials()
        let callback = completion
        completion = nil
        self.session.finishTasksAndInvalidate()
        dismissPresentation()
        callback?(value)
    }

    @objc private func cancel(_ sender: Any?) {
        guard !finished else { return }
        // A React-side Back must restore the wizard immediately. Even when a
        // credential commit is in flight, resolve this presentation now so
        // the pending relayLogin promise cannot leave the wizard busy with a
        // stale browser covering the restored form. The later commit result
        // is discarded because the controller is already finished.
        if let activeCheck, activeCheck.requestCancellation() == .committing {
            panelClosedDuringCommit = true
        }
        activeCheck = nil
        finished = true
        checking = false
        embeddedResizeProbe?.cancel()
        embeddedResizeProbe = nil
        signInButton.isEnabled = false
        cancelButton.isEnabled = false
        clearCapturedCredentials()
        session.invalidateAndCancel()
        dismissPresentation()
        let callback = completion
        completion = nil
        callback?(nil)
    }

    func cancelFromReact() {
        cancel(nil)
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

    private func finishCheckingFailure(_ message: String, attempt: NativeRelayLoginAttempt, quiet: Bool = false) {
        guard isCurrentCheck(attempt) else { return }
        attempt.finish()
        activeCheck = nil
        checking = false
        checkStartedAt = nil
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
        if quiet {
            setStatus(waitingForSignInStatus)
            scheduleAutomaticSignInCheck()
        } else {
            statusLabel.stringValue = message
        }
    }

    private func scheduleAutomaticSignInCheck() {
        guard mode == .login, isBrowserFlowLive, !checking else { return }
        automaticCheckProbe?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.isBrowserFlowLive, !self.checking else { return }
            self.startSignInCheck(automatically: true)
        }
        automaticCheckProbe = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: work)
    }

    /// A station sign-in completes inside the page, so no navigation callback
    /// announces it. The watcher polls the page's own credential storage while
    /// the sign-in surface is open and starts one verification per new
    /// credential fingerprint; the quiet automatic check keeps retrying behind
    /// it, so a slow or temporarily failing station still settles.
    private func scheduleLoginWatch(delay: TimeInterval = 1) {
        guard mode == .login, isBrowserFlowLive else { return }
        loginWatchProbe?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.pollLoginState() }
        loginWatchProbe = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func pollLoginState() {
        guard mode == .login, isBrowserFlowLive else { return }
        webView.evaluateJavaScript(Self.loginStateScript) { [weak self] value, _ in
            guard let self, self.isBrowserFlowLive else { return }
            let fields = value as? [String: Any]
            let token = (fields?["token"] as? String) ?? ""
            let path = (fields?["path"] as? String) ?? ""
            let formPresent = (fields?["form"] as? Bool) ?? false
            if token.isEmpty {
                self.observedLoginSignature = nil
                // A sign-in form that disappeared means the station accepted
                // the credentials, and a cookie-only station stores no token.
                if self.observedLoginForm == true, !formPresent {
                    self.recoverStalledCheck()
                    self.startSignInCheck(automatically: true)
                }
            } else {
                let signature = "\(token)@\(path)"
                if signature != self.observedLoginSignature {
                    self.observedLoginSignature = signature
                    self.recoverStalledCheck()
                    self.startSignInCheck(automatically: true)
                }
            }
            self.observedLoginForm = formPresent
            self.scheduleLoginWatch(delay: token.isEmpty ? 1 : 1.5)
        }
    }

    /// A verification that never resolves would silently stop the automatic
    /// loop. A check that has neither committed nor finished for a long time is
    /// dropped so the next poll can retry; a commit in flight is left alone.
    private func recoverStalledCheck() {
        guard checking, let activeCheck, !activeCheck.isCommitting() else { return }
        guard let checkStartedAt, Date().timeIntervalSince(checkStartedAt) > 30 else { return }
        activeCheck.finish()
        self.activeCheck = nil
        self.checkStartedAt = nil
        checking = false
    }

    private func clearCapturedCredentials() {
        capturedAccessToken = nil
        capturedRefreshToken = nil
        capturedUserID = nil
        capturedPassword = nil
    }

    private func dismissPresentation() {
        automaticCheckProbe?.cancel()
        automaticCheckProbe = nil
        loginWatchProbe?.cancel()
        loginWatchProbe = nil
        observedLoginSignature = nil
        observedLoginForm = nil
        checkStartedAt = nil
        pageReadinessProbe?.cancel()
        pageReadinessProbe = nil
        loginFormRevealProbe?.cancel()
        loginFormRevealProbe = nil
        agreementRevealProbe?.cancel()
        agreementRevealProbe = nil
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "litellmRelayPassword")
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "litellmRelayPage")
        if let observer = embeddedCloseObserver {
            NotificationCenter.default.removeObserver(observer)
            embeddedCloseObserver = nil
        }
        embeddedContent?.removeFromSuperview()
        embeddedContent = nil
        if let panel {
            // The project forbids window animations: end the child window's
            // modal session and close it inside a zero-duration transaction so
            // dismissal is instant and the workspace behind it is live again.
            withoutAnimations {
                AppKitNativeLeaf.shared.endChildPanel(panel)
                panel.close()
            }
        }
    }

    @objc private func reloadBrowser(_ sender: Any?) {
        guard isBrowserFlowLive else { return }
        showBrowserLoading()
        webView.reload()
    }

    @objc private func closeEmbeddedWindow(_ sender: Any?) {
        guard let embeddedClose else { return cancel(sender) }
        embeddedClose()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard isBrowserFlowLive else { return }
        scheduleEmbeddedBrowserResize()
        schedulePageReadinessProbe()
        scheduleAgreementReveal()
        scheduleLoginWatch(delay: 1)
        if mode == .logs {
            if !didRestoreSession {
                restoreLocalStorageWhenReady()
            }
            return
        }
        scheduleLoginFormReveal()
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
        scheduleEmbeddedBrowserResize()
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
                    withoutAnimations { self.loadingOverlay.isHidden = true }
                    self.scheduleEmbeddedBrowserResize()
                    self.scheduleAgreementReveal()
                    if self.mode == .logs {
                        if !self.didRestoreSession {
                            self.restoreLocalStorageWhenReady()
                        }
                    } else {
                        self.scheduleLoginFormReveal()
                        self.prefillLoginWhenReady()
                        if self.didRestoreSession {
                            self.probeRestoredSession()
                        } else if !self.didRestoreSession {
                            self.restoreLocalStorageWhenReady()
                        }
                        self.scheduleAutomaticSignInCheck()
                        self.scheduleLoginWatch(delay: 1)
                    }
                } else {
                    self.schedulePageReadinessProbe()
                }
            }
        }
        pageReadinessProbe = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4, execute: work)
    }

    private func scheduleLoginFormReveal() {
        guard mode == .login,
              isBrowserFlowLive,
              !didRevealLoginField,
              loginFormRevealAttempts < 12 else { return }
        loginFormRevealProbe?.cancel()
        loginFormRevealAttempts += 1
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.isBrowserFlowLive, !self.didRevealLoginField else { return }
            let script = """
            (() => {
              const visible = (node) => {
                if (!(node instanceof HTMLInputElement) || node.disabled || node.readOnly) return false;
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const selectors = [
                'input[autocomplete=username]',
                'input[type=email]',
                'input[name=username]',
                'input[name=email]',
                'input[placeholder*="用户名"]',
                'input[placeholder*="邮箱"]',
                'input[placeholder*="email" i]',
                'input[type=text]',
                'input:not([type])'
              ];
              let user = null;
              for (const selector of selectors) {
                user = Array.from(document.querySelectorAll(selector)).find(visible) || null;
                if (user) break;
              }
              if (!user) return false;
              user.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });
              const active = document.activeElement;
              if (!active || active === document.body || !(active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement || active.isContentEditable)) {
                try { user.focus({ preventScroll: true }); } catch { user.focus(); }
              }
              return true;
            })();
            """
            self.webView.evaluateJavaScript(script) { [weak self] value, _ in
                guard let self, self.isBrowserFlowLive else { return }
                if value as? Bool == true {
                    self.didRevealLoginField = true
                    self.scheduleEmbeddedBrowserResize()
                    self.loginFormRevealProbe = nil
                } else {
                    self.scheduleLoginFormReveal()
                }
            }
        }
        loginFormRevealProbe = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: work)
    }

    private func scheduleAgreementReveal() {
        guard mode == .login, isBrowserFlowLive, agreementRevealAttempts < 16 else { return }
        agreementRevealProbe?.cancel()
        agreementRevealAttempts += 1
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.isBrowserFlowLive else { return }
            let script = """
            (() => {
              const terms = /agree|terms|privacy|consent|协议|同意|隐私|用户协议/iu;
              const visible = (node) => {
                if (!(node instanceof Element)) return false;
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const checkbox = Array.from(document.querySelectorAll('input[type="checkbox"],[role="checkbox"]')).find((node) => {
                const scope = node.closest('label,li,form,section,div') || node.parentElement || node;
                return terms.test((scope.innerText || scope.textContent || '').trim());
              });
              if (!checkbox || !visible(checkbox)) return false;
              (checkbox.closest('label,li,form,section') || checkbox).scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' });
              return true;
            })();
            """
            self.webView.evaluateJavaScript(script) { [weak self] value, _ in
                guard let self, self.isBrowserFlowLive else { return }
                if value as? Bool == true {
                    self.scheduleEmbeddedBrowserResize()
                    self.agreementRevealProbe = nil
                } else {
                    self.scheduleAgreementReveal()
                }
            }
        }
        agreementRevealProbe = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45, execute: work)
    }

    private func showBrowserLoading() {
        pageReadinessProbe?.cancel()
        embeddedResizeProbe?.cancel()
        embeddedResizeProbe = nil
        embeddedResizeAttempts = 0
        lastEmbeddedContentHeight = nil
        loginFormRevealProbe?.cancel()
        loginFormRevealProbe = nil
        loginFormRevealAttempts = 0
        didRevealLoginField = false
        agreementRevealProbe?.cancel()
        agreementRevealProbe = nil
        agreementRevealAttempts = 0
        automaticCheckProbe?.cancel()
        automaticCheckProbe = nil
        loadingLabel.stringValue = mode == .logs
            ? text("Loading usage logs...", "正在加载用量日志...")
            : text("Loading sign-in page...", "正在加载登录页面...")
        withoutAnimations { loadingOverlay.isHidden = false }
    }

    private func showBrowserFailure() {
        pageReadinessProbe?.cancel()
        loginFormRevealProbe?.cancel()
        loginFormRevealProbe = nil
        automaticCheckProbe?.cancel()
        automaticCheckProbe = nil
        loadingLabel.stringValue = mode == .logs
            ? text("The usage log page could not be loaded.", "用量日志页面加载失败。")
            : text("The sign-in page could not be loaded.", "登录页面加载失败。")
        withoutAnimations { loadingOverlay.isHidden = false }
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
            request.setValue(RelayBrowserIdentity.acceptLanguage, forHTTPHeaderField: "Accept-Language")
            request.setValue(RelayBrowserIdentity.userAgent, forHTTPHeaderField: "User-Agent")
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

private enum NativeRelaySessionMemoryStore {
    private static let lock = NSLock()
    private static var sessions: [String: NativeRelaySession] = [:]

    static func readSession(accountID: String, accountType: String, origin: String) -> NativeRelaySession? {
        lock.lock()
        defer { lock.unlock() }
        guard let value = sessions[accountID],
              value.accountType == accountType,
              value.origin == origin,
              value.cookie.utf8.count <= 32_768,
              value.accessToken.utf8.count <= 32_768,
              value.refreshToken.utf8.count <= 32_768,
              !value.cookie.isEmpty || !value.accessToken.isEmpty else { return nil }
        return value
    }

    static func writeSession(_ session: NativeRelaySession, accountID: String) {
        guard session.cookie.utf8.count <= 32_768,
              session.accessToken.utf8.count <= 32_768,
              session.refreshToken.utf8.count <= 32_768,
              !session.cookie.isEmpty || !session.accessToken.isEmpty else { return }
        lock.lock()
        sessions[accountID] = session
        lock.unlock()
    }

    static func clear(accountID: String) {
        lock.lock()
        sessions.removeValue(forKey: accountID)
        lock.unlock()
    }
}
