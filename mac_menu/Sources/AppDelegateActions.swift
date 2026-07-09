import Cocoa

extension AppDelegate {
    func performControl(_ action: String, requiresRunning: Bool? = nil) {
        setBusy(true, title: "Status: Working: \(action)")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            if let requiresRunning {
                let running = self.isRunning()
                if requiresRunning && !running {
                    DispatchQueue.main.async {
                        self.setBusy(false, title: self.displayedServiceState(.stopped).title)
                        self.updateStatus()
                    }
                    return
                }
                if !requiresRunning && running {
                    DispatchQueue.main.async {
                        self.setBusy(false, title: "Status: Running")
                        self.updateStatus()
                    }
                    return
                }
            }

            let result = self.control(action)
            let state = self.currentMenuState(timeoutSeconds: self.statusRefreshTimeout)
            DispatchQueue.main.async {
                self.setBusy(false, title: self.displayedServiceState(state.serviceState).title)
                self.renderState(state)
                if result.0 != 0 {
                    self.showAlert(title: "LiteLLM \(action) failed", message: result.1)
                }
            }
        }
    }

    func performAutoStartToggle() {
        setBusy(true, title: "Status: Checking auto start")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let action = self.readAutoStartState() == .enabled ? "autostart-disable" : "autostart-enable"
            let result = self.control(action)
            DispatchQueue.main.async {
                self.setBusy(false, title: "Status: Checking")
                self.updateStatus()
                if result.0 != 0 {
                    self.showAlert(title: "LiteLLM \(action) failed", message: result.1)
                }
            }
        }
    }

    func performRouteTraceStartupToggle() {
        setBusy(true, title: "Status: Updating route trace")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let action = self.isRouteTraceEnabled() ? "route-trace-disable" : "route-trace-enable"
            let result = self.control(action)
            DispatchQueue.main.async {
                self.setBusy(false, title: "Status: Checking")
                self.updateStatus()
                if result.0 != 0 {
                    self.showAlert(title: "LiteLLM \(action) failed", message: result.1)
                }
            }
        }
    }

    func performCodexConfigUpdate(action: String, workingTitle: String, successTitle: String) {
        setBusy(true, title: "Status: \(workingTitle)")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.control(action)
            DispatchQueue.main.async {
                self.setBusy(false, title: "Status: Checking")
                self.updateStatus()
                self.showAlert(
                    title: result.0 == 0 ? successTitle : "Codex config update failed",
                    message: result.1
                )
            }
        }
    }

    func readWebDAVSyncSettings() -> WebDAVSyncSettings {
        let result = control("webdav-settings", logCommand: false)
        guard result.0 == 0, let data = result.1.data(using: .utf8) else {
            return WebDAVSyncSettings()
        }
        return (try? JSONDecoder().decode(WebDAVSyncSettings.self, from: data)) ?? WebDAVSyncSettings()
    }

    func readWebDAVLastStatus(deadline: Date? = nil) -> WebDAVLastStatus {
        let result = control("webdav-last-status", logCommand: false, timeoutSeconds: statusTimeout(deadline: deadline))
        guard result.0 == 0, let data = result.1.data(using: .utf8) else {
            return WebDAVLastStatus()
        }
        return (try? JSONDecoder().decode(WebDAVLastStatus.self, from: data)) ?? WebDAVLastStatus()
    }

    func isWebDAVConfigured(_ settings: WebDAVSyncSettings? = nil) -> Bool {
        let current = settings ?? readWebDAVSyncSettings()
        return !(current.url ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    func performWebDAVControl(
        action: String,
        workingTitle: String,
        successTitle: String,
        input: String? = nil,
        showsSuccess: Bool = true
    ) {
        setBusy(true, title: "Status: \(workingTitle)")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.control(arguments: [action], input: input)
            DispatchQueue.main.async {
                self.setBusy(false, title: "Status: Checking")
                self.updateStatus()
                if result.0 == 0 {
                    if showsSuccess {
                        self.showAlert(title: successTitle, message: result.1)
                    }
                } else {
                    self.showAlert(title: "WebDAV sync failed", message: result.1)
                }
            }
        }
    }

    func showWebDAVConfigureDialog(enableAfterSave: Bool = false) {
        let settings = readWebDAVSyncSettings()
        let draft = lastFailedWebDAVSettings
        let dialog = WebDAVSettingsDialogController(
            url: draft?.url ?? settings.url ?? "",
            username: draft?.username ?? settings.username ?? "",
            remoteName: webDAVRemoteNameForDialog(draft?.remoteName ?? settings.remoteName),
            syncIntervalMinutes: draft?.syncIntervalMinutes ?? settings.syncIntervalMinutes ?? defaultWebDAVSyncIntervalMinutes,
            timeoutSeconds: draft?.timeoutSeconds ?? settings.timeoutSeconds ?? Double(defaultWebDAVTimeoutSeconds),
            password: draft?.password ?? "",
            hasExistingPassword: (settings.hasPassword ?? false) || (draft?.keepPassword ?? false),
            probeHandler: { [weak self] dialogResult, completion in
                guard let self else {
                    completion(1, "WebDAV probe could not start.")
                    return
                }
                self.probeWebDAVSettings(dialogResult, completion: completion)
            }
        )
        guard let dialogResult = dialog.runModal() else { return }
        saveWebDAVSettings(dialogResult, enableAfterSave: enableAfterSave)
    }

    func webDAVRemoteNameForDialog(_ value: String?) -> String {
        let raw = (value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if raw.isEmpty {
            return defaultWebDAVRemoteName
        }
        return raw
    }

    func webDAVSettingsInput(_ dialogResult: WebDAVSettingsDialogResult) throws -> String {
        var payload: [String: Any] = [
            "url": dialogResult.url,
            "username": dialogResult.username,
            "remote_name": dialogResult.remoteName,
            "sync_interval_minutes": dialogResult.syncIntervalMinutes,
            "timeout_seconds": dialogResult.timeoutSeconds,
        ]
        if let password = dialogResult.password {
            payload["password"] = password
        } else if dialogResult.keepPassword {
            payload["keep_password"] = true
        }

        let data = try JSONSerialization.data(withJSONObject: payload, options: [])
        guard let input = String(data: data, encoding: .utf8) else {
            throw AppError(message: "Could not encode WebDAV settings.")
        }
        return input
    }

    func saveWebDAVSettings(_ dialogResult: WebDAVSettingsDialogResult, enableAfterSave: Bool) {
        do {
            let input = try webDAVSettingsInput(dialogResult)
            let alreadyEnabled = isWebDAVSyncEnabled()
            setBusy(true, title: "Status: Configuring WebDAV sync")
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                guard let self else { return }
                let configureResult = self.control(arguments: ["webdav-configure"], input: input + "\n")
                var finalExit = configureResult.0
                var finalOutput = configureResult.1
                var successTitle = "WebDAV sync configured"

                if configureResult.0 == 0 && enableAfterSave {
                    let enableResult = self.control(arguments: ["webdav-enable"])
                    finalExit = enableResult.0
                    finalOutput = [configureResult.1, enableResult.1]
                        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                        .filter { !$0.isEmpty }
                        .joined(separator: "\n")
                    successTitle = "WebDAV sync enabled"
                }

                DispatchQueue.main.async {
                    self.setBusy(false, title: "Status: Checking")
                    self.updateStatus()
                    if finalExit == 0 {
                        self.lastFailedWebDAVSettings = nil
                        if enableAfterSave || !alreadyEnabled {
                            self.showAlert(title: successTitle, message: finalOutput)
                        }
                    } else {
                        self.lastFailedWebDAVSettings = dialogResult
                        self.showAlert(title: "WebDAV sync failed", message: finalOutput)
                    }
                }
            }
        } catch {
            lastFailedWebDAVSettings = dialogResult
            showAlert(title: "WebDAV sync failed", message: String(describing: error))
        }
    }

    func probeWebDAVSettings(
        _ dialogResult: WebDAVSettingsDialogResult,
        completion: @escaping (Int32, String) -> Void
    ) {
        let input: String
        do {
            input = try webDAVSettingsInput(dialogResult)
        } catch {
            lastFailedWebDAVSettings = dialogResult
            completion(1, String(describing: error))
            return
        }

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else {
                completion(1, "WebDAV probe could not start.")
                return
            }
            let result = self.control(arguments: ["webdav-probe"], input: input + "\n")
            DispatchQueue.main.async {
                if result.0 == 0 {
                    self.lastFailedWebDAVSettings = nil
                } else {
                    self.lastFailedWebDAVSettings = dialogResult
                }
                completion(result.0, result.1)
            }
        }
    }

    func requireRunningForMenuAction(_ title: String) -> Bool {
        let state = currentMenuState(timeoutSeconds: statusRefreshTimeout)
        renderState(state)
        if state.running {
            return true
        }
        showAlert(title: "\(title) unavailable", message: "LiteLLM service is stopped. Start the service first.")
        return false
    }

    func setBusy(_ busy: Bool, title: String) {
        if busy {
            statusRefreshGeneration += 1
            statusRefreshInFlight = false
        }
        self.busy = busy
        statusMenuItem.title = title
        for item in actionMenuItems {
            item.isEnabled = !busy
        }
    }

    var actionMenuItems: [NSMenuItem] {
        [
            startMenuItem,
            stopMenuItem,
            restartServiceMenuItem,
            autoStartMenuItem,
            routeTraceStartupMenuItem,
            codexLocalMenuItem,
            codexPreSwitchReapplyMenuItem,
            modelConfigEditorMenuItem,
            runtimeSettingsMenuItem,
            routeRecoveryDetailsMenuItem,
            webdavEnabledMenuItem,
            webdavConfigureMenuItem,
            logsMenuItem,
        ]
    }

    @objc func startLiteLLMService() {
        beginServiceStart(
            logMessage: "manual LiteLLM service start requested",
            failureTitle: "LiteLLM start failed"
        )
    }
    @objc func stopLiteLLMService() {
        serviceShouldBeRunning = false
        serviceStartInFlight = false
        performControl("stop")
    }
    @objc func restartLiteLLMService() {
        serviceShouldBeRunning = true
        performControl("restart")
    }
    @objc func toggleAutoStart() {
        performAutoStartToggle()
    }
    @objc func toggleRouteTraceStartup() {
        performRouteTraceStartupToggle()
    }
    @objc func applyCodexLocalConfig() {
        let codexState = readCodexConfigState()
        if codexState.configuredForLiteLLM {
            codexLocalMenuItem.isEnabled = false
            codexPreSwitchReapplyMenuItem.isEnabled = codexState.preSwitchReapplyAvailable
            showAlert(
                title: "Codex already configured",
                message: "Codex is already using the local LiteLLM service. No Codex config switch is needed."
            )
            return
        }
        performCodexConfigUpdate(
            action: "codex-local-config",
            workingTitle: "Configuring Codex for LiteLLM",
            successTitle: "Codex Configured"
        )
    }
    @objc func reapplyCodexPreSwitchConfig() {
        let codexState = readCodexConfigState()
        if !codexState.configuredForLiteLLM {
            codexLocalMenuItem.isEnabled = true
            codexPreSwitchReapplyMenuItem.isEnabled = false
            showAlert(
                title: "Reapply unavailable",
                message: "Codex is not currently configured for the local LiteLLM service."
            )
            return
        }
        if !codexState.preSwitchReapplyAvailable {
            codexPreSwitchReapplyMenuItem.isEnabled = false
            showAlert(
                title: "Reapply unavailable",
                message: "No active pre-switch Codex config state was found."
            )
            return
        }
        performCodexConfigUpdate(
            action: "codex-reapply-pre-switch-config",
            workingTitle: "Reapplying pre-switch Codex config",
            successTitle: "Codex Config Reapplied"
        )
    }
    @objc func editModelsConfig() {
        if let editor = modelConfigEditor {
            editor.showWindow()
            return
        }

        setBusy(true, title: "Status: Preparing config editor")
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.control("config-editor-bootstrap")
            DispatchQueue.main.async { [self] in
                self.setBusy(false, title: "Status: Checking")
                self.updateStatus()
                if result.0 != 0 {
                    self.showAlert(title: "Open config editor failed", message: result.1)
                    return
                }
                let editor = ModelConfigEditorController(
                    root: self.root,
                    bundleRoot: self.bundleRoot,
                    environment: self.controlEnvironment(),
                    onSaved: { [weak self] _ in
                        self?.updateStatus()
                    },
                    onClose: { [weak self] in
                        self?.modelConfigEditor = nil
                    }
                )
                self.modelConfigEditor = editor
                editor.showWindow()
            }
        }
    }
    @objc func showRouteRecoveryDetails() {
        writeCommandToTempFile(
            action: "route-recovery-html",
            filename: "route_recovery.html",
            title: "View Recovery Details failed"
        )
    }

    @objc func configureRuntimeSettings() {
        setBusy(true, title: "Status: Loading runtime settings")
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.control("runtime-settings", logCommand: false)
            DispatchQueue.main.async {
                self.setBusy(false, title: "Status: Checking")
                self.updateStatus()
                guard result.0 == 0, let data = result.1.data(using: .utf8) else {
                    self.showAlert(title: "Runtime settings unavailable", message: result.1)
                    return
                }
                do {
                    let payload = try JSONDecoder().decode(RuntimeSettingsPayload.self, from: data)
                    guard !payload.settings.isEmpty else {
                        self.showAlert(title: "Runtime settings unavailable", message: "No editable runtime settings were returned.")
                        return
                    }
                    let dialog = RuntimeSettingsDialogController(settings: payload.settings)
                    guard let values = dialog.runModal() else { return }
                    self.saveRuntimeSettings(values)
                } catch {
                    self.showAlert(title: "Runtime settings unavailable", message: String(describing: error))
                }
            }
        }
    }

    func runtimeSettingsInput(_ values: [String: String]) throws -> String {
        let payload = RuntimeSettingsSavePayload(values: values)
        let data = try JSONEncoder().encode(payload)
        guard let input = String(data: data, encoding: .utf8) else {
            throw AppError(message: "Could not encode runtime settings.")
        }
        return input
    }

    func saveRuntimeSettings(_ values: [String: String]) {
        do {
            let input = try runtimeSettingsInput(values)
            setBusy(true, title: "Status: Saving runtime settings")
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                guard let self else { return }
                let configureResult = self.control(arguments: ["runtime-settings-configure"], input: input + "\n")
                var finalExit = configureResult.0
                var outputs = [configureResult.1]

                if configureResult.0 == 0 {
                    let watchResult = self.control("config-watch-ensure", logCommand: false)
                    outputs.append(watchResult.1)
                    let restartResult = self.control("restart")
                    outputs.append(restartResult.1)
                    if restartResult.0 != 0 {
                        finalExit = restartResult.0
                    } else if watchResult.0 != 0 {
                        finalExit = watchResult.0
                    }
                }

                let output = outputs
                    .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                    .filter { !$0.isEmpty }
                    .joined(separator: "\n")

                DispatchQueue.main.async {
                    self.setBusy(false, title: "Status: Checking")
                    self.updateStatus()
                    self.showAlert(
                        title: finalExit == 0 ? "Runtime settings saved" : "Runtime settings failed",
                        message: output
                    )
                }
            }
        } catch {
            showAlert(title: "Runtime settings failed", message: String(describing: error))
        }
    }

    @objc func configureWebDAVSync() {
        showWebDAVConfigureDialog()
    }

    @objc func toggleWebDAVSync() {
        let enabled = isWebDAVSyncEnabled()
        if !enabled && !isWebDAVConfigured() {
            showWebDAVConfigureDialog(enableAfterSave: true)
            return
        }

        performWebDAVControl(
            action: enabled ? "webdav-disable" : "webdav-enable",
            workingTitle: enabled ? "Disabling WebDAV sync" : "Enabling WebDAV sync",
            successTitle: enabled ? "WebDAV sync disabled" : "WebDAV sync enabled"
        )
    }

    @objc func openRouteTraceVisual() {
        writeCommandToTempFile(
            action: "route-trace-html",
            filename: "route_trace.html",
            title: "View Route Trace Log failed"
        )
    }
    @objc func openServiceLog() {
        writeCommandToVisualFile(
            action: "tail",
            filename: "service_log.html",
            pageTitle: "Service Log",
            title: "View Service Log failed"
        )
    }
    @objc func openConfigWatchLog() {
        writeCommandToVisualFile(
            action: "config-watch-tail",
            filename: "config_watch_log.html",
            pageTitle: "Config Watch Log",
            title: "View Config Watch Log failed"
        )
    }
    @objc func quitLiteLLM() { NSApp.terminate(nil) }
}
