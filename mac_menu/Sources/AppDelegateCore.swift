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

        var isRunning: Bool {
            self == .running
        }

        var canRecover: Bool {
            self == .running || self == .unhealthy
        }

        var isTransitional: Bool {
            self == .starting
        }

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
        let routeTraceEnabled: Bool
        let routeRecoverySummary: String
        let webdavSyncEnabled: Bool
        let webdavLastStatus: WebDAVLastStatus
        let codexConfigState: CodexConfigState

        var running: Bool {
            serviceState.isRunning
        }

        var canRecover: Bool {
            serviceState.canRecover
        }
    }

    struct CodexConfigState {
        let configuredForLiteLLM: Bool
        let preSwitchReapplyAvailable: Bool
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

    static func codexHome() -> String {
        let environment = ProcessInfo.processInfo.environment
        if let override = environment["CODEX_HOME"], !override.isEmpty {
            return (override as NSString).expandingTildeInPath
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".codex", isDirectory: true)
            .path
    }

    let bundleRoot = AppDelegate.bundledAppRoot()
    let root = AppDelegate.runtimeRoot()
    let codexHome = AppDelegate.codexHome()
    var controlPath: String { "\(bundleRoot)/service.sh" }
    var menuLogPath: String { "\(root)/menu-actions.log" }

    var statusItem: NSStatusItem!
    var statusMenuItem = NSMenuItem(title: "Status: Checking", action: nil, keyEquivalent: "")
    var startMenuItem = NSMenuItem()
    var stopMenuItem = NSMenuItem()
    var restartServiceMenuItem = NSMenuItem()
    var autoStartMenuItem = NSMenuItem()
    var routeTraceStartupMenuItem = NSMenuItem()
    var codexLocalMenuItem = NSMenuItem()
    var codexPreSwitchReapplyMenuItem = NSMenuItem()
    var modelConfigEditorMenuItem = NSMenuItem()
    var runtimeSettingsMenuItem = NSMenuItem()
    var routeRecoveryStatusMenuItem = NSMenuItem()
    var routeRecoveryDetailsMenuItem = NSMenuItem()
    var webdavStatusMenuItem = NSMenuItem()
    var webdavEnabledMenuItem = NSMenuItem()
    var webdavConfigureMenuItem = NSMenuItem()
    var logsMenuItem = NSMenuItem()
    var versionMenuItem = NSMenuItem()
    var refreshTimer: Timer?
    var busy = false
    var statusRefreshInFlight = false
    var statusRefreshGeneration = 0
    var lastRenderedServiceState: ServiceState?
    var stoppedRecheckPending = false
    var serviceShouldBeRunning = false
    var serviceStartInFlight = false
    var lastStoppedRecoveryAttempt: Date?
    var modelConfigEditor: ModelConfigEditorController?
    var lastFailedWebDAVSettings: WebDAVSettingsDialogResult?
    let lifecycleQueue = DispatchQueue(label: "menu.litellm.lifecycle", qos: .userInitiated)
    let stoppedRecoveryRetryInterval: TimeInterval = 15.0
    let statusCommandTimeout: TimeInterval = 5.0
    let statusRefreshTimeout: TimeInterval = 12.0

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        installMainMenu()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        configureStatusButton()
        buildMenu()
        startServiceOnLaunch()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            self?.updateStatus()
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        refreshTimer?.invalidate()
        refreshTimer = nil
        serviceShouldBeRunning = false
        appendLog("application quit requested; disabling config watcher")
        let watchResult = control("config-watch-disable")
        if watchResult.0 != 0 {
            appendLog("config watcher disable on quit failed: \(elidedDisplayText(watchResult.1, limit: 240))")
        }
        appendLog("application quit requested; stopping LiteLLM service")
        let result = control("stop")
        if result.0 == 0 {
            appendLog("LiteLLM service stopped on application quit")
        } else {
            appendLog("LiteLLM service stop on quit failed: \(elidedDisplayText(result.1, limit: 240))")
        }
        return .terminateNow
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

        startMenuItem = menuItem("Start LiteLLM Service", #selector(startLiteLLMService))
        stopMenuItem = menuItem("Stop LiteLLM Service", #selector(stopLiteLLMService))
        restartServiceMenuItem = menuItem("Restart LiteLLM Service", #selector(restartLiteLLMService))
        menu.addItem(startMenuItem)
        menu.addItem(stopMenuItem)
        menu.addItem(restartServiceMenuItem)
        menu.addItem(NSMenuItem.separator())

        autoStartMenuItem = menuItem("Auto Start at Login", #selector(toggleAutoStart))
        menu.addItem(autoStartMenuItem)
        routeTraceStartupMenuItem = menuItem("Route Trace", #selector(toggleRouteTraceStartup))
        menu.addItem(routeTraceStartupMenuItem)
        menu.addItem(NSMenuItem.separator())

        codexLocalMenuItem = menuItem("Configure Codex for LiteLLM", #selector(applyCodexLocalConfig))
        codexPreSwitchReapplyMenuItem = menuItem("Reapply Pre-Switch Codex Config", #selector(reapplyCodexPreSwitchConfig))
        menu.addItem(codexLocalMenuItem)
        menu.addItem(codexPreSwitchReapplyMenuItem)
        menu.addItem(NSMenuItem.separator())

        modelConfigEditorMenuItem = menuItem("Edit Models Config", #selector(editModelsConfig))
        runtimeSettingsMenuItem = menuItem("Runtime Settings...", #selector(configureRuntimeSettings))
        menu.addItem(modelConfigEditorMenuItem)
        menu.addItem(runtimeSettingsMenuItem)
        menu.addItem(NSMenuItem.separator())

        routeRecoveryStatusMenuItem = NSMenuItem(title: "Recovery: 0 recovering / 0 cooldown", action: nil, keyEquivalent: "")
        routeRecoveryStatusMenuItem.isEnabled = false
        routeRecoveryDetailsMenuItem = menuItem("View Recovery Details", #selector(showRouteRecoveryDetails))
        routeRecoveryDetailsMenuItem.isEnabled = true
        menu.addItem(routeRecoveryStatusMenuItem)
        menu.addItem(routeRecoveryDetailsMenuItem)
        menu.addItem(NSMenuItem.separator())

        webdavStatusMenuItem = NSMenuItem(title: "WebDAV: Checking...", action: nil, keyEquivalent: "")
        webdavStatusMenuItem.isEnabled = false
        webdavEnabledMenuItem = menuItem("Enable WebDAV Sync", #selector(toggleWebDAVSync))
        webdavConfigureMenuItem = menuItem("WebDAV Sync Settings...", #selector(configureWebDAVSync))
        menu.addItem(webdavStatusMenuItem)
        menu.addItem(webdavEnabledMenuItem)
        menu.addItem(webdavConfigureMenuItem)
        menu.addItem(NSMenuItem.separator())

        logsMenuItem = menuItem("View Route Trace Log", #selector(openRouteTraceVisual))
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
        button.title = ""
        button.image = makeStatusIcon()
        button.imagePosition = .imageOnly
        button.toolTip = "LiteLLM Menu Service"
    }

    func makeStatusIcon() -> NSImage {
        let size = NSSize(width: 22, height: 18)
        let image = NSImage(size: size)
        image.lockFocus()

        let scale: CGFloat = 1.0
        let transform = NSAffineTransform()
        transform.translateX(by: size.width * (1 - scale) / 2, yBy: size.height * (1 - scale) / 2)
        transform.scale(by: scale)
        transform.concat()

        NSColor.black.setFill()
        let bigFont = NSFont.systemFont(ofSize: 18, weight: .regular)
        let smallFont = NSFont.systemFont(ofSize: 13, weight: .regular)
        let attributes: [NSAttributedString.Key: Any] = [
            .foregroundColor: NSColor.black
        ]

        ("L" as NSString).draw(
            at: NSPoint(x: 2.5, y: -1.0),
            withAttributes: attributes.merging([.font: bigFont]) { _, new in new }
        )
        ("L" as NSString).draw(
            at: NSPoint(x: 13.0, y: 2.0),
            withAttributes: attributes.merging([.font: smallFont]) { _, new in new }
        )

        image.unlockFocus()
        image.isTemplate = true
        return image
    }
}
