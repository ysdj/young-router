import Cocoa

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    enum AutoStartState {
        case enabled
        case incomplete
        case disabled
    }

    enum ServiceState {
        case running
        case starting
        case unhealthy
        case stopped

        var title: String {
            switch self {
            case .running:
                return "Status: Running"
            case .starting:
                return "Status: Starting"
            case .unhealthy:
                return "Status: Unhealthy"
            case .stopped:
                return "Status: Stopped"
            }
        }
    }

    struct MenuState {
        let serviceState: ServiceState
        let autoStartState: AutoStartState
        let routeRecoverySummary: String
        let routeRecovery: RouteRecoveryStatus
        let webdavSyncEnabled: Bool
        let webdavLastStatus: WebDAVLastStatus

    }

    struct MenuStatusPayload: Decodable {
        let serviceState: String
        let autoStartState: String
        let routeRecoverySummary: String
        let routeRecovery: RouteRecoveryStatus?
        let webdavSyncEnabled: Bool
        let webdavLastStatus: WebDAVLastStatus

        enum CodingKeys: String, CodingKey {
            case serviceState = "service_state"
            case autoStartState = "auto_start_state"
            case routeRecoverySummary = "route_recovery_summary"
            case routeRecovery = "route_recovery"
            case webdavSyncEnabled = "webdav_sync_enabled"
            case webdavLastStatus = "webdav_last_status"
        }
    }

    struct RouteRecoveryStatus: Decodable {
        struct Current: Decodable {
            let status: String
            let activity: String
            let kind: String
            let title: String
            let detail: String
            let attempt: Int?
            let heartbeatAgeSeconds: Double?
            let cooldownRemainingSeconds: Double?

            enum CodingKeys: String, CodingKey {
                case status
                case activity
                case kind
                case title
                case detail
                case attempt
                case heartbeatAgeSeconds = "heartbeat_age_seconds"
                case cooldownRemainingSeconds = "cooldown_remaining_seconds"
            }
        }

        let summary: String
        let recovering: Int
        let cooldown: Int
        let overdue: Int
        let current: Current?

        static let empty = RouteRecoveryStatus(
            summary: "0 recovering / 0 cooldown",
            recovering: 0,
            cooldown: 0,
            overdue: 0,
            current: nil
        )
    }

    struct WebDAVSyncSettings: Codable {
        var url: String? = nil
        var username: String? = nil
        var remoteName: String? = nil
        var syncIntervalMinutes: Int? = nil
        var timeoutSeconds: Double? = nil
        var hasPassword: Bool? = nil

        enum CodingKeys: String, CodingKey {
            case url
            case username
            case remoteName = "remote_name"
            case syncIntervalMinutes = "sync_interval_minutes"
            case timeoutSeconds = "timeout_seconds"
            case hasPassword = "has_password"
        }
    }

    struct WebDAVLastStatus: Codable {
        var action: String? = nil
        var ok: Bool? = nil
        var exitCode: Int? = nil
        var checkedAt: String? = nil
        var enabled: Bool? = nil
        var output: String? = nil

        enum CodingKeys: String, CodingKey {
            case action
            case ok
            case exitCode = "exit_code"
            case checkedAt = "checked_at"
            case enabled
            case output
        }
    }

    static func bundledAppRoot() -> String {
        if let resourcesURL = Bundle.main.resourceURL {
            let appURL = resourcesURL.appendingPathComponent("App", isDirectory: true)
            if FileManager.default.fileExists(atPath: appURL.appendingPathComponent("service.sh").path) {
                return appURL.path
            }
            if resourcesURL.path.contains(".app/Contents/Resources") {
                return appURL.path
            }
        }

        let environment = ProcessInfo.processInfo.environment
        if let override = environment["LITELLM_TEMPLATE_ROOT"], !override.isEmpty {
            return (override as NSString).expandingTildeInPath
        }
        return FileManager.default.currentDirectoryPath
    }

    static func runtimeRoot() -> String {
        let environment = ProcessInfo.processInfo.environment
        if let override = environment["LITELLM_RUNTIME_ROOT"], !override.isEmpty {
            return (override as NSString).expandingTildeInPath
        }
        if let override = environment["LITELLM_MENU_HOME"], !override.isEmpty {
            return (override as NSString).expandingTildeInPath
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".litellm-menu", isDirectory: true)
            .path
    }

    let bundleRoot = AppDelegate.bundledAppRoot()
    let root = AppDelegate.runtimeRoot()
    var controlPath: String { "\(bundleRoot)/service.sh" }
    var menuLogPath: String { "\(root)/menu-actions.log" }
    let statusItemAutosaveName = "menu.litellm.menu.status-item"
    var isHeadlessIsolatedTest: Bool {
        let environment = ProcessInfo.processInfo.environment
        return environment["LITELLM_MENU_TEST_HEADLESS"] == "1"
            && !(environment["LITELLM_RUNTIME_ROOT"] ?? "").isEmpty
    }

    var statusItem: NSStatusItem!
    var statusItemVisibilityObservation: NSKeyValueObservation?
    var statusItemVisibilityRecoveryAttempted = false
    var statusMenuItem = NSMenuItem(title: "Status: Checking", action: nil, keyEquivalent: "")
    var autoStartMenuItem = NSMenuItem()
    var codexConfigurationMenuItem = NSMenuItem()
    var modelConfigEditorMenuItem = NSMenuItem()
    var runtimeSettingsMenuItem = NSMenuItem()
    var configurationPackageMenuItem = NSMenuItem()
    var routeRecoveryStatusMenuItem = NSMenuItem()
    var webdavStatusMenuItem = NSMenuItem()
    var webdavEnabledMenuItem = NSMenuItem()
    var webdavConfigureMenuItem = NSMenuItem()
    var logsMenuItem = NSMenuItem()
    var logWindowController: LogWindowController?
    var versionMenuItem = NSMenuItem()
    var refreshTimer: Timer?
    var busy = false
    var statusRefreshInFlight = false
    var statusRefreshGeneration = 0
    var serviceShouldBeRunning = false
    var serviceStartInFlight = false
    var terminationCleanupInFlight = false
    var lastServiceRecoveryAttempt: Date?
    var modelConfigEditor: ModelConfigEditorController?
    var codexConfigDialog: CodexConfigDialogController?
    var lastFailedWebDAVSettings: WebDAVSettingsDialogResult?
    let lifecycleProcessLock = NSLock()
    var lifecycleProcess: Process?
    var lifecycleCancellationRequested = false
    let lifecycleQueue = DispatchQueue(label: "menu.litellm.lifecycle", qos: .userInitiated)
    let terminationQueue = DispatchQueue(label: "menu.litellm.termination", qos: .userInitiated)
    let configWatchQueue = DispatchQueue(label: "menu.litellm.config-watch", qos: .utility)
    let serviceRecoveryRetryInterval: TimeInterval = 15.0
    let statusCommandTimeout: TimeInterval = 5.0
    let statusRefreshTimeout: TimeInterval = 12.0

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        installMainMenu()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.autosaveName = statusItemAutosaveName
        configureStatusButton()
        buildMenu()
        if isHeadlessIsolatedTest {
            statusItem.isVisible = false
        }
        if !isHeadlessIsolatedTest {
            statusItemVisibilityObservation = statusItem.observe(\.isVisible, options: [.initial, .new]) { [weak self] _, _ in
                DispatchQueue.main.async {
                    self?.appendStatusItemDiagnostic(stage: "visibility-change")
                }
            }
            DispatchQueue.main.async { [weak self] in
                self?.appendStatusItemDiagnostic(stage: "next-run-loop")
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
                self?.appendStatusItemDiagnostic(stage: "one-second")
            }
            scheduleStatusItemVisibilityRecoveryCheck()
        }
        startServiceOnLaunch()
        startStatusRefreshTimer()
    }

    func startStatusRefreshTimer() {
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            self?.updateStatus()
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminationCleanupInFlight {
            return .terminateLater
        }
        terminationCleanupInFlight = true
        refreshTimer?.invalidate()
        refreshTimer = nil
        statusItemVisibilityObservation = nil
        serviceShouldBeRunning = false
        serviceStartInFlight = false
        terminationQueue.async { [weak self] in
            guard let self else {
                DispatchQueue.main.async {
                    sender.reply(toApplicationShouldTerminate: true)
                }
                return
            }
            self.modelConfigEditor?.cancelRuntimeApplyInFlight()
            self.cancelLifecycleControl()
            self.appendLog("application quit requested; stopping LiteLLM service")
            let stopResult = self.control("stop", timeoutSeconds: 12)
            let serviceStopped = stopResult.0 == 0
            if serviceStopped {
                self.appendLog("LiteLLM service stopped on application quit")
            } else {
                self.appendLog("LiteLLM service stop on quit failed: \(elidedDisplayText(stopResult.1, limit: 240))")
            }
            self.appendLog("application quit requested; disabling config watcher")
            let watchResult = self.configWatchQueue.sync {
                self.control("config-watch-disable", timeoutSeconds: 8)
            }
            if watchResult.0 != 0 {
                self.appendLog("config watcher disable on quit failed: \(elidedDisplayText(watchResult.1, limit: 240))")
            }
            DispatchQueue.main.async {
                guard serviceStopped, watchResult.0 == 0 else {
                    self.terminationCleanupInFlight = false
                    self.resumeLifecycleControl()
                    self.serviceShouldBeRunning = true
                    self.startStatusRefreshTimer()
                    if !self.isHeadlessIsolatedTest {
                        self.showAlert(
                            title: "LiteLLM Menu is still running",
                            message: "The local service or its config watcher did not stop cleanly. The app remains open and will recover the service."
                        )
                    }
                    self.beginServiceStart(
                        logMessage: "termination cleanup failed; restoring LiteLLM service",
                        failureTitle: "LiteLLM service recovery failed"
                    )
                    sender.reply(toApplicationShouldTerminate: false)
                    return
                }
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }

    func installMainMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu(title: "LiteLLM Menu")
        let quitItem = appMenu.addItem(withTitle: "Quit", action: #selector(quitLiteLLM), keyEquivalent: "q")
        quitItem.target = self
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Paste and Match Style", action: #selector(NSTextView.pasteAsPlainText(_:)), keyEquivalent: "V")
        editMenu.addItem(withTitle: "Delete", action: #selector(NSText.delete(_:)), keyEquivalent: "")
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        NSApp.mainMenu = mainMenu
    }

    func buildMenu() {
        let menu = NSMenu()
        menu.delegate = self
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        menu.addItem(NSMenuItem.separator())

        autoStartMenuItem = menuItem("Auto Start at Login", #selector(toggleAutoStart))
        menu.addItem(autoStartMenuItem)
        menu.addItem(NSMenuItem.separator())

        codexConfigurationMenuItem = menuItem("Codex Settings...", #selector(configureCodexSettings))
        modelConfigEditorMenuItem = menuItem("Providers & Models...", #selector(editModelsConfig))
        runtimeSettingsMenuItem = menuItem("Runtime Settings...", #selector(configureRuntimeSettings))
        configurationPackageMenuItem = menuItem("Import / Export Config...", #selector(showConfigurationPackageDialog))
        menu.addItem(modelConfigEditorMenuItem)
        menu.addItem(runtimeSettingsMenuItem)
        menu.addItem(codexConfigurationMenuItem)
        menu.addItem(configurationPackageMenuItem)
        menu.addItem(NSMenuItem.separator())

        webdavStatusMenuItem = NSMenuItem(title: "WebDAV: Checking...", action: nil, keyEquivalent: "")
        webdavStatusMenuItem.isEnabled = false
        webdavEnabledMenuItem = menuItem("Enable WebDAV Sync", #selector(toggleWebDAVSync))
        webdavConfigureMenuItem = menuItem("WebDAV Sync Settings...", #selector(configureWebDAVSync))
        menu.addItem(webdavStatusMenuItem)
        menu.addItem(webdavEnabledMenuItem)
        menu.addItem(webdavConfigureMenuItem)
        menu.addItem(NSMenuItem.separator())

        routeRecoveryStatusMenuItem = menuItem("Recovery: 0 recovering / 0 cooldown", #selector(showRouteRecoveryDetails))
        menu.addItem(routeRecoveryStatusMenuItem)

        logsMenuItem = menuItem("View Logs", #selector(openLogs))
        menu.addItem(logsMenuItem)
        menu.addItem(NSMenuItem.separator())

        versionMenuItem = NSMenuItem(title: appVersionMenuTitle(), action: nil, keyEquivalent: "")
        versionMenuItem.isEnabled = false
        menu.addItem(versionMenuItem)
        menu.addItem(menuItem("Quit", #selector(quitLiteLLM)))
        statusItem.menu = menu
    }

    func appVersionMenuTitle() -> String {
        let info = Bundle.main.infoDictionary ?? [:]
        let version = (info["CFBundleShortVersionString"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let build = (info["CFBundleVersion"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

        if version.isEmpty && build.isEmpty {
            return "Version: Unknown"
        }
        if build.isEmpty || build == version {
            return "Version: \(version)"
        }
        if version.isEmpty {
            return "Version: Build \(build)"
        }
        return "Version: \(version) (build \(build))"
    }

    func menuItem(_ title: String, _ action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        return item
    }

    func configureStatusButton() {
        guard let button = statusItem.button else { return }
        statusItem.length = 32
        button.image = nil
        button.imagePosition = .noImage
        renderStatusButton(.empty)
    }

    /// Keep the menu-bar affordance stable. Recovery is intentionally reported
    /// inside the menu (and in its tooltip), never by changing the icon, color,
    /// or text shown in the system menu bar.
    func renderStatusButton(_ status: RouteRecoveryStatus) {
        guard let button = statusItem.button else { return }
        let active = status.recovering > 0 || status.cooldown > 0
        button.title = "LL"
        button.toolTip = active
            ? routeRecoveryStatusTooltip(status)
            : "LiteLLM Menu Service"
        button.setAccessibilityLabel("LiteLLM Menu")
    }

    func appendStatusItemDiagnostic(stage: String) {
        guard let item = statusItem else {
            appendLog("status item \(stage): unavailable")
            return
        }
        let button = item.button
        let window = button?.window
        let autosaveName = item.autosaveName ?? "none"
        appendLog(
            "status item \(stage): visible=\(item.isVisible), button=\(button != nil), "
                + "image=\(button?.image != nil), window=\(window != nil), "
                + "window-visible=\(window?.isVisible ?? false), screen=\(window?.screen != nil), "
                + "autosave=\(autosaveName)"
        )
    }

    func statusItemLooksOffscreen() -> Bool {
        guard let item = statusItem, item.isVisible, let button = item.button else {
            return false
        }
        guard button.frame.width > 0 else {
            return true
        }
        return button.window?.screen == nil
    }

    func scheduleStatusItemVisibilityRecoveryCheck() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            guard let self else { return }
            self.appendStatusItemDiagnostic(stage: "two-seconds")
            guard self.statusItemLooksOffscreen() else { return }

            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
                guard let self else { return }
                self.appendStatusItemDiagnostic(stage: "three-seconds")
                guard !self.statusItemVisibilityRecoveryAttempted,
                      self.statusItemLooksOffscreen()
                else {
                    return
                }
                self.recreateStatusItemAfterVisibilityFailure()
            }
        }
    }

    func recreateStatusItemAfterVisibilityFailure() {
        guard let oldItem = statusItem, oldItem.isVisible else { return }

        statusItemVisibilityRecoveryAttempted = true
        let menu = oldItem.menu
        statusItemVisibilityObservation = nil
        NSStatusBar.system.removeStatusItem(oldItem)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.autosaveName = statusItemAutosaveName
        configureStatusButton()
        statusItem.menu = menu
        statusItemVisibilityObservation = statusItem.observe(\.isVisible, options: [.initial, .new]) { [weak self] _, _ in
            DispatchQueue.main.async {
                self?.appendStatusItemDiagnostic(stage: "visibility-change-after-recovery")
            }
        }
        appendLog("status item remained offscreen; recreated once without changing system visibility settings")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.appendStatusItemDiagnostic(stage: "one-second-after-recovery")
        }
    }

}
