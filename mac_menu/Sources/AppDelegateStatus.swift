import Cocoa

extension AppDelegate {
    func initialMenuState(serviceState: ServiceState) -> MenuState {
        MenuState(
            serviceState: serviceState,
            autoStartState: .disabled,
            routeTraceEnabled: false,
            routeRecoverySummary: "0 recovering / 0 cooldown",
            webdavSyncEnabled: false,
            webdavLastStatus: WebDAVLastStatus(),
            codexConfigState: CodexConfigState(configuredForLiteLLM: false, preSwitchReapplyAvailable: false)
        )
    }

    func menuWillOpen(_ menu: NSMenu) {
        guard !busy else { return }
        refreshStatusForMenuOpen()
    }

    func refreshStatusForMenuOpen() {
        statusRefreshGeneration += 1
        let generation = statusRefreshGeneration
        statusRefreshInFlight = true
        statusMenuItem.title = "Status: Checking..."

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let state = self.currentMenuState(timeoutSeconds: self.statusRefreshTimeout)
            DispatchQueue.main.async {
                guard !self.busy, generation == self.statusRefreshGeneration else { return }
                self.statusRefreshInFlight = false
                self.renderState(state)
            }
        }
    }

    func updateStatus() {
        guard !busy, !statusRefreshInFlight else { return }
        statusRefreshInFlight = true
        statusRefreshGeneration += 1
        let generation = statusRefreshGeneration
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let state = self.currentMenuState(timeoutSeconds: self.statusRefreshTimeout)
            DispatchQueue.main.async {
                guard !self.busy, generation == self.statusRefreshGeneration else { return }
                self.statusRefreshInFlight = false
                self.renderState(state)
            }
        }
    }

    func formattedWebDAVStatusTime(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        let parser = ISO8601DateFormatter()
        guard let date = parser.date(from: value) else { return value }
        let formatter = DateFormatter()
        formatter.timeZone = .current
        let calendar = Calendar.current
        if calendar.isDateInToday(date) {
            formatter.dateFormat = "HH:mm"
        } else if calendar.component(.year, from: date) == calendar.component(.year, from: Date()) {
            formatter.dateFormat = "MM-dd HH:mm"
        } else {
            formatter.dateFormat = "yyyy-MM-dd HH:mm"
        }
        return formatter.string(from: date)
    }

    func webDAVActionTitle(_ action: String) -> String {
        switch action {
        case "push":
            return "Push"
        case "sync":
            return "Sync"
        case "sync-push":
            return "Sync push"
        case "sync-pull":
            return "Sync pull"
        case "pull":
            return "Pull"
        case "probe":
            return "Probe"
        case "disable":
            return "Disabled"
        case "configure":
            return "Config"
        default:
            return action.prefix(1).uppercased() + action.dropFirst()
        }
    }

    func webDAVStatusTitle(status: WebDAVLastStatus) -> String {
        guard let action = status.action, !action.isEmpty else {
            return "WebDAV: No sync yet"
        }
        let actionText = webDAVActionTitle(action)
        let body: String
        if let ok = status.ok {
            if action == "disable" && ok {
                body = actionText
            } else {
                body = "\(actionText) \(ok ? "OK" : "failed")"
            }
        } else {
            body = "\(actionText) unknown"
        }
        if let time = formattedWebDAVStatusTime(status.checkedAt) {
            return "WebDAV: \(body) \(time)"
        }
        return "WebDAV: \(body)"
    }

    func routeRecoveryStatusTitle(_ summary: String) -> String {
        let text = summary.trimmingCharacters(in: .whitespacesAndNewlines)
        return "Recovery: \(text.isEmpty ? "0 recovering / 0 cooldown" : text)"
    }

    func startServiceOnLaunch() {
        beginServiceStart(
            logMessage: "application launched; starting LiteLLM service",
            failureTitle: "LiteLLM service start failed"
        )
    }

    func beginServiceStart(logMessage: String, failureTitle: String) {
        beginServiceStart(logMessage: logMessage, failureTitle: failureTitle, showFailureAlert: true)
    }

    func beginServiceStart(logMessage: String, failureTitle: String, showFailureAlert: Bool) {
        serviceShouldBeRunning = true
        guard !serviceStartInFlight else { return }
        serviceStartInFlight = true
        lastStoppedRecoveryAttempt = Date()
        statusRefreshGeneration += 1
        statusRefreshInFlight = false
        renderState(initialMenuState(serviceState: .starting))
        appendLog(logMessage)
        lifecycleQueue.async { [weak self] in
            guard let self else { return }
            let result = self.control("start")
            let state = self.initialMenuState(serviceState: result.0 == 0 ? .running : .unhealthy)
            DispatchQueue.main.async {
                self.serviceStartInFlight = false
                self.statusRefreshGeneration += 1
                self.statusRefreshInFlight = false
                self.renderState(state)
                if result.0 != 0 {
                    if showFailureAlert {
                        self.showAlert(title: failureTitle, message: result.1)
                    }
                    self.updateStatus()
                } else {
                    self.ensureConfigWatchEnabled()
                }
            }
        }
    }

    func ensureConfigWatchEnabled() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let result = self.control("config-watch-ensure", logCommand: false)
            DispatchQueue.main.async {
                if result.0 != 0 {
                    self.appendLog("config watch ensure failed: \(result.1)")
                }
                self.updateStatus()
            }
        }
    }

    func renderState(_ state: MenuState) {
        let serviceState = displayedServiceState(state.serviceState)
        let previousServiceState = lastRenderedServiceState
        lastRenderedServiceState = serviceState
        let starting = serviceState.isTransitional
        let canRecover = serviceState.canRecover
        statusMenuItem.title = serviceState.title

        startMenuItem.isHidden = canRecover || starting
        stopMenuItem.isHidden = !canRecover
        restartServiceMenuItem.isHidden = !canRecover

        startMenuItem.isEnabled = !canRecover && !starting
        stopMenuItem.isEnabled = canRecover
        restartServiceMenuItem.isEnabled = canRecover

        autoStartMenuItem.isEnabled = true
        switch state.autoStartState {
        case .enabled:
            autoStartMenuItem.title = "Auto Start at Login"
            autoStartMenuItem.state = .on
        case .incomplete:
            autoStartMenuItem.title = "Auto Start at Login (repair needed)"
            autoStartMenuItem.state = .mixed
        case .disabled:
            autoStartMenuItem.title = "Auto Start at Login"
            autoStartMenuItem.state = .off
        }

        routeTraceStartupMenuItem.isEnabled = true
        routeTraceStartupMenuItem.state = state.routeTraceEnabled ? .on : .off

        webdavStatusMenuItem.title = webDAVStatusTitle(status: state.webdavLastStatus)
        webdavStatusMenuItem.toolTip = state.webdavLastStatus.output
        webdavStatusMenuItem.isEnabled = false
        webdavEnabledMenuItem.isEnabled = true
        webdavEnabledMenuItem.state = state.webdavSyncEnabled ? .on : .off
        webdavConfigureMenuItem.isEnabled = true

        routeRecoveryStatusMenuItem.title = routeRecoveryStatusTitle(state.routeRecoverySummary)
        routeRecoveryStatusMenuItem.isEnabled = false
        routeRecoveryDetailsMenuItem.isEnabled = true

        codexLocalMenuItem.isEnabled = !state.codexConfigState.configuredForLiteLLM
        codexPreSwitchReapplyMenuItem.isEnabled = state.codexConfigState.configuredForLiteLLM
            && state.codexConfigState.preSwitchReapplyAvailable

        logsMenuItem.isEnabled = true

        scheduleStoppedRecheckIfNeeded(previousState: previousServiceState, currentState: serviceState)
    }

    func scheduleStoppedRecheckIfNeeded(previousState: ServiceState?, currentState: ServiceState) {
        guard currentState == .stopped else { return }
        guard previousState == .running || previousState == .starting || previousState == .unhealthy else { return }
        guard !busy, !stoppedRecheckPending else { return }

        stoppedRecheckPending = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self else { return }
            self.stoppedRecheckPending = false
            guard !self.busy else { return }
            self.updateStatus()
        }
    }
}
