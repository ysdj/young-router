import Cocoa

extension AppDelegate {
    func initialMenuState(serviceState: ServiceState) -> MenuState {
        MenuState(
            serviceState: serviceState,
            autoStartState: .disabled,
            routeRecoverySummary: "0 recovering / 0 cooldown",
            routeRecovery: .empty,
            webdavSyncEnabled: false,
            webdavLastStatus: WebDAVLastStatus()
        )
    }

    func menuWillOpen(_ menu: NSMenu) {
        guard !busy, !terminationCleanupInFlight else { return }
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
                guard !self.busy,
                      !self.terminationCleanupInFlight,
                      generation == self.statusRefreshGeneration else { return }
                self.statusRefreshInFlight = false
                self.renderState(state)
            }
        }
    }

    func updateStatus() {
        guard !busy, !statusRefreshInFlight, !terminationCleanupInFlight else { return }
        statusRefreshInFlight = true
        statusRefreshGeneration += 1
        let generation = statusRefreshGeneration
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let state = self.currentMenuState(timeoutSeconds: self.statusRefreshTimeout)
            DispatchQueue.main.async {
                guard !self.busy,
                      !self.terminationCleanupInFlight,
                      generation == self.statusRefreshGeneration else { return }
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
        let text = routeRecoveryDisplayText(summary).trimmingCharacters(in: .whitespacesAndNewlines)
        return "Recovery: \(text.isEmpty ? "0 recovering / 0 cooldown" : text)"
    }

    func routeRecoveryDisplayText(_ text: String) -> String {
        text.replacingOccurrences(
            of: "billing",
            with: "credit",
            options: .caseInsensitive
        )
    }

    func routeRecoveryStatusTooltip(_ status: RouteRecoveryStatus) -> String {
        guard let current = status.current else {
            if status.cooldown > 0 {
                return "\(status.cooldown) route deployment(s) are cooling down before they can be retried."
            }
            return "No active recovery polling."
        }
        let displayTitle = routeRecoveryDisplayText(current.title)
        var lines = [displayTitle]
        if !current.detail.isEmpty {
            lines.append(routeRecoveryDisplayText(current.detail))
        }
        let kind = routeRecoveryKindLabel(current.kind)
        if !kind.isEmpty {
            lines.append("Cause: \(kind)")
        }
        let likelyStuck = current.activity == "overdue" || status.overdue > 0
        let heartbeatUnavailable = current.heartbeatAgeSeconds == nil
            || current.activity.localizedCaseInsensitiveContains("unavailable")
        if likelyStuck {
            lines.append("Verdict: no fresh heartbeat; recovery may be stuck.")
        } else if heartbeatUnavailable {
            lines.append("Verdict: heartbeat unavailable; recovery progress is unknown.")
        } else {
            lines.append("Verdict: heartbeat is fresh; recovery is still working.")
        }
        lines.append("State: \(routeRecoveryDisplayText(current.status))")
        if let attempt = current.attempt {
            lines.append("Attempt: \(attempt)")
        }
        if let heartbeatAge = current.heartbeatAgeSeconds {
            let age = Int(heartbeatAge.rounded())
            lines.append(age > 45 ? "Heartbeat is stale (\(age)s)." : "Heartbeat: \(age)s ago.")
        }
        if current.status == "retry scheduled", let cooldown = current.cooldownRemainingSeconds {
            lines.append("Next cooldown check: \(Int(cooldown.rounded(.up)))s.")
        }
        if status.overdue > 0 {
            lines.append("\(status.overdue) recovery item(s) need attention.")
        }
        return lines.joined(separator: "\n")
    }

    func routeRecoveryKindLabel(_ kind: String) -> String {
        switch kind.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "billing": return "Credit or quota"
        case "auth": return "Authentication"
        case "network": return "Network"
        case "rate_limit": return "Rate limit"
        case "timeout": return "Timeout"
        case "upstream": return "Upstream service"
        case "routing": return "Routing"
        case "": return ""
        default: return kind == "unknown" ? "" : kind
        }
    }

    func startServiceOnLaunch() {
        beginServiceStart(
            logMessage: "application launched; starting LiteLLM service",
            failureTitle: "LiteLLM service start failed",
            showFailureAlert: !isHeadlessIsolatedTest
        )
    }

    func beginServiceStart(logMessage: String, failureTitle: String) {
        beginServiceStart(logMessage: logMessage, failureTitle: failureTitle, showFailureAlert: true)
    }

    func beginServiceStart(logMessage: String, failureTitle: String, showFailureAlert: Bool) {
        guard !terminationCleanupInFlight else { return }
        serviceShouldBeRunning = true
        guard !serviceStartInFlight else { return }
        serviceStartInFlight = true
        lastServiceRecoveryAttempt = Date()
        statusRefreshGeneration += 1
        statusRefreshInFlight = false
        renderState(initialMenuState(serviceState: .starting))
        appendLog(logMessage)
        lifecycleQueue.async { [weak self] in
            guard let self else { return }
            let result = self.lifecycleControl("start")
            let state = self.initialMenuState(serviceState: result.0 == 0 ? .running : .unhealthy)
            DispatchQueue.main.async {
                self.serviceStartInFlight = false
                guard !self.terminationCleanupInFlight else { return }
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
        guard !terminationCleanupInFlight else { return }
        configWatchQueue.async { [weak self] in
            guard let self else { return }
            guard !self.terminationCleanupInFlight else { return }
            let result = self.control("config-watch-ensure", logCommand: false, timeoutSeconds: 5)
            DispatchQueue.main.async {
                guard !self.terminationCleanupInFlight else { return }
                if result.0 != 0 {
                    self.appendLog("config watch ensure failed: \(result.1)")
                }
                self.updateStatus()
            }
        }
    }

    func renderState(_ state: MenuState) {
        let serviceState = displayedServiceState(state.serviceState)
        statusMenuItem.title = serviceState.title

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

        webdavStatusMenuItem.title = webDAVStatusTitle(status: state.webdavLastStatus)
        webdavStatusMenuItem.toolTip = state.webdavLastStatus.output
        webdavStatusMenuItem.isEnabled = false
        webdavEnabledMenuItem.isEnabled = true
        webdavEnabledMenuItem.state = state.webdavSyncEnabled ? .on : .off
        webdavConfigureMenuItem.isEnabled = true

        routeRecoveryStatusMenuItem.title = routeRecoveryStatusTitle(state.routeRecoverySummary)
        routeRecoveryStatusMenuItem.toolTip = routeRecoveryStatusTooltip(state.routeRecovery)
        routeRecoveryStatusMenuItem.isHidden = false
        routeRecoveryStatusMenuItem.isEnabled = true
        renderStatusButton(state.routeRecovery)

        codexConfigurationMenuItem.isEnabled = true

        logsMenuItem.isEnabled = true
    }
}
