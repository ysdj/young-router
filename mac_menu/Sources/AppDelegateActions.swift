import Cocoa
import UniformTypeIdentifiers

extension AppDelegate {
    func performAutoStartToggle() {
        setBusy(true, title: "Status: Checking auto start")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let action = self.readAutoStartState() == .enabled ? "autostart-disable" : "autostart-enable"
            let result = self.control(action)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.setBusy(false, title: "Status: Checking")
                self.updateStatus()
                if result.0 != 0 {
                    self.showAlert(title: "LiteLLM \(action) failed", message: result.1)
                }
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

    func isWebDAVConfigured(_ settings: WebDAVSyncSettings) -> Bool {
        !(settings.url ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
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

    func showWebDAVConfigureDialog(
        enableAfterSave: Bool = false,
        initialSettings: WebDAVSyncSettings? = nil
    ) {
        let draft = lastFailedWebDAVSettings
        let dialog = WebDAVSettingsDialogController(
            url: draft?.url ?? initialSettings?.url ?? "",
            username: draft?.username ?? initialSettings?.username ?? "",
            remoteName: webDAVRemoteNameForDialog(draft?.remoteName ?? initialSettings?.remoteName),
            syncIntervalMinutes: draft?.syncIntervalMinutes ?? initialSettings?.syncIntervalMinutes ?? defaultWebDAVSyncIntervalMinutes,
            timeoutSeconds: draft?.timeoutSeconds ?? initialSettings?.timeoutSeconds ?? Double(defaultWebDAVTimeoutSeconds),
            password: draft?.password ?? "",
            hasExistingPassword: (initialSettings?.hasPassword ?? false) || (draft?.keepPassword ?? false),
            probeHandler: { [weak self] dialogResult, completion in
                guard let self else {
                    completion(1, "WebDAV probe could not start.")
                    return
                }
                self.probeWebDAVSettings(dialogResult, completion: completion)
            }
        )
        if draft == nil && initialSettings == nil {
            dialog.setSavedSettingsLoading(true)
            DispatchQueue.global(qos: .utility).async { [weak self, weak dialog] in
                guard let self else { return }
                let settings = self.readWebDAVSyncSettings()
                DispatchQueue.main.async {
                    dialog?.applyLoadedSettings(settings)
                }
            }
        }
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
            setBusy(true, title: "Status: Configuring WebDAV sync")
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                guard let self else { return }
                let alreadyEnabled = self.isWebDAVSyncEnabled()
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
            autoStartMenuItem,
            codexConfigurationMenuItem,
            modelConfigEditorMenuItem,
            runtimeSettingsMenuItem,
            configurationPackageMenuItem,
            webdavEnabledMenuItem,
            webdavConfigureMenuItem,
            logsMenuItem,
        ]
    }

    @objc func toggleAutoStart() {
        performAutoStartToggle()
    }
    @objc func configureCodexSettings() {
        guard !busy, !terminationCleanupInFlight else { return }
        if let dialog = codexConfigDialog {
            dialog.showWindow()
            return
        }
        let dialog = CodexConfigDialogController(
            root: root,
            bundleRoot: bundleRoot,
            environment: controlEnvironment(),
            onApplied: { [weak self] in
                self?.updateStatus()
            },
            onClose: { [weak self] in
                self?.codexConfigDialog = nil
            }
        )
        codexConfigDialog = dialog
        dialog.showWindow()
    }
    @objc func editModelsConfig() {
        presentModelConfigEditor()
    }

    func presentModelConfigEditor(
        loadSavedConfiguration: Bool = true,
        afterOpening: @escaping (ModelConfigEditorController) -> Void = { _ in }
    ) {
        if let editor = modelConfigEditor {
            editor.showWindow()
            afterOpening(editor)
            if loadSavedConfiguration {
                editor.loadConfigurationInBackground()
            }
            return
        }

        let editor = ModelConfigEditorController(
            root: root,
            bundleRoot: bundleRoot,
            environment: controlEnvironment(),
            onSaved: { [weak self] _ in
                self?.updateStatus()
            },
            onClose: { [weak self] in
                self?.modelConfigEditor = nil
            }
        )
        modelConfigEditor = editor
        editor.showWindow()
        afterOpening(editor)
        if loadSavedConfiguration {
            editor.loadConfigurationInBackground()
        }
    }

    func stageImportedProvidersModels(
        _ imported: ConfigurationPackageProvidersModelsPayload,
        sourceDescription: String,
        completion: @escaping (Bool) -> Void
    ) {
        if let editor = modelConfigEditor {
            editor.commitEditor()
            if editor.hasPendingChanges {
                let alert = NSAlert()
                alert.messageText = "Replace unsaved provider changes?"
                alert.informativeText = "The package will replace the current Providers & Models draft. Nothing is written until you choose Apply."
                alert.alertStyle = .warning
                alert.addButton(withTitle: "Replace Draft")
                alert.addButton(withTitle: "Cancel")
                guard runSettingsModal(alert) == .alertFirstButtonReturn else {
                    completion(false)
                    return
                }
            }
        }

        presentModelConfigEditor(loadSavedConfiguration: false) { editor in
            editor.adoptImportedConfiguration(
                providers: imported.providers,
                document: imported.document,
                sourceDescription: sourceDescription
            )
            completion(true)
        }
    }

    @objc func showConfigurationPackageDialog() {
        let dialog = ConfigurationPackageDialogController()
        switch dialog.runModal() {
        case .importPackage:
            importConfigurationPackage()
        case .export(let sections):
            exportConfigurationPackageFromMenu(sections: sections)
        case nil:
            return
        }
    }

    func importConfigurationPackage() {
        guard !busy, !terminationCleanupInFlight else { return }
        let panel = NSOpenPanel()
        panel.title = "Import Configuration Package"
        panel.message = "Choose a LiteLLM Menu configuration package. Each included section opens in its normal editor, in sequence, and remains a draft until you save it."
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.json]
        guard runSettingsModal(panel) == .OK, let url = panel.url else { return }

        setBusy(true, title: "Status: Validating configuration package")
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.control(
                arguments: ["configuration-package-import", "--input", url.path],
                logCommand: false,
                timeoutSeconds: 60,
                outputLimitBytes: 16 * 1_024 * 1_024
            )
            let payload: ConfigurationPackageImportPayload?
            if result.0 == 0, let data = result.1.data(using: .utf8) {
                payload = try? JSONDecoder().decode(ConfigurationPackageImportPayload.self, from: data)
            } else {
                payload = nil
            }

            DispatchQueue.main.async {
                self.setBusy(false, title: "Status: Checking")
                self.updateStatus()
                guard result.0 == 0, let payload else {
                    self.showAlert(title: "Configuration package import failed", message: result.1)
                    return
                }
                self.presentImportedConfigurationPackage(
                    payload,
                    sourceDescription: url.lastPathComponent
                )
            }
        }
    }

    func presentImportedConfigurationPackage(
        _ payload: ConfigurationPackageImportPayload,
        sourceDescription: String
    ) {
        let allowedSections: Set<String> = ["runtime_settings", "providers_models"]
        let sections = Set(payload.sections)
        guard !sections.isEmpty,
              sections.isSubset(of: allowedSections),
              (sections.contains("runtime_settings") == (payload.runtimeSettings != nil)),
              (sections.contains("providers_models") == (payload.providersModels != nil)) else {
            showAlert(
                title: "Configuration package import failed",
                message: "The validated package response was incomplete."
            )
            return
        }

        let runtimeValues = payload.runtimeSettings?.values
        guard let providersModels = payload.providersModels else {
            if let runtimeValues {
                presentRuntimeSettings(stagedValues: runtimeValues)
            }
            return
        }

        let presentProviders: () -> Void = { [weak self] in
            guard let self else { return }
            self.stageImportedProvidersModels(
                providersModels,
                sourceDescription: sourceDescription
            ) { _ in }
        }
        if let runtimeValues {
            presentRuntimeSettings(stagedValues: runtimeValues) { accepted in
                if accepted {
                    presentProviders()
                }
            }
        } else {
            presentProviders()
        }
    }

    @objc func showRouteRecoveryDetails() {
        showLogs(tab: .recovery)
    }

    @objc func configureRuntimeSettings() {
        presentRuntimeSettings()
    }

    func presentRuntimeSettings(
        stagedValues: [String: String]? = nil,
        completion: ((Bool) -> Void)? = nil
    ) {
        setBusy(true, title: "Status: Loading runtime settings")
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.control("runtime-settings", logCommand: false)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.setBusy(false, title: "Status: Checking")
                self.updateStatus()
                guard result.0 == 0, let data = result.1.data(using: .utf8) else {
                    self.showAlert(title: "Runtime settings unavailable", message: result.1)
                    completion?(false)
                    return
                }
                do {
                    let payload = try JSONDecoder().decode(RuntimeSettingsPayload.self, from: data)
                    guard !payload.settings.isEmpty else {
                        self.showAlert(title: "Runtime settings unavailable", message: "No editable runtime settings were returned.")
                        completion?(false)
                        return
                    }
                    let dialog = RuntimeSettingsDialogController(
                        settings: payload.settings,
                        saveHandler: { [weak self] values, completion in
                            guard let self else {
                                completion(false, "LiteLLM Menu is no longer available.")
                                return
                            }
                            self.saveRuntimeSettings(
                                values,
                                completion: completion
                            )
                        }
                    )
                    if let stagedValues {
                        guard dialog.stageImportedValues(stagedValues) else {
                            completion?(false)
                            return
                        }
                        dialog.window.title = "Runtime Settings: Imported Draft"
                        if dialog.dirtyKeys.isEmpty {
                            completion?(true)
                            return
                        }
                    }
                    performOnMainRunLoop {
                        let savedValues = dialog.runModal()
                        completion?(savedValues != nil)
                    }
                } catch {
                    self.showAlert(title: "Runtime settings unavailable", message: String(describing: error))
                    completion?(false)
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

    func exportConfigurationPackageFromMenu(sections selected: [String]) {
        guard !selected.isEmpty else { return }
        let exportsAll = selected.count == 2
        exportConfigurationPackage(
            sections: exportsAll ? "all" : selected[0],
            title: exportsAll ? "Export All Configuration" : (selected[0] == "runtime_settings" ? "Export Runtime Settings" : "Export Providers & Models"),
            filename: exportsAll ? "litellm-menu-configuration.json" : (selected[0] == "runtime_settings" ? "litellm-menu-runtime-settings.json" : "litellm-menu-providers-models.json")
        ) { [weak self] result in
            self?.showConfigurationPackageExportResult(result)
        }
    }

    func exportConfigurationPackage(
        sections: String,
        title: String,
        filename: String,
        completion: @escaping (Result<Bool, Error>) -> Void
    ) {
        let panel = NSSavePanel()
        panel.title = title
        panel.message = "The package may include credentials and other sensitive configuration values."
        panel.nameFieldStringValue = filename
        panel.allowedContentTypes = [.json]
        guard runSettingsModal(panel) == .OK, let url = panel.url else {
            completion(.success(false))
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.control(
                arguments: [
                    "configuration-package-export",
                    "--sections", sections,
                    "--output", url.path,
                ],
                logCommand: false
            )
            DispatchQueue.main.async {
                result.0 == 0
                    ? completion(.success(true))
                    : completion(.failure(AppError(message: result.1)))
            }
        }
    }

    func showConfigurationPackageExportResult(_ result: Result<Bool, Error>) {
        switch result {
        case .success(true):
            showAlert(title: "Configuration package exported", message: "The selected configuration was written with restricted file permissions.", style: .informational)
        case .success(false):
            break
        case .failure(let error):
            showAlert(title: "Configuration package export failed", message: error.localizedDescription)
        }
    }

    func saveRuntimeSettings(
        _ values: [String: String],
        completion: @escaping (_ succeeded: Bool, _ message: String) -> Void
    ) {
        do {
            let input = try runtimeSettingsInput(values)
            setBusy(true, title: "Status: Saving runtime settings")
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                guard let self else {
                    completion(false, "LiteLLM Menu is no longer available.")
                    return
                }
                let result = self.control(
                    arguments: ["runtime-settings-apply"],
                    input: input + "\n"
                )
                let succeeded = result.0 == 0
                let message = result.1.trimmingCharacters(in: .whitespacesAndNewlines)

                performOnMainRunLoop { [weak self] in
                    guard let self else { return }
                    self.setBusy(false, title: "Status: Checking")
                    self.updateStatus()
                    if succeeded,
                       let editor = self.modelConfigEditor,
                       editor.window.isVisible {
                        editor.configureProviderBillingRefreshTimer(refreshImmediately: true)
                    }
                    completion(succeeded, message)
                }
            }
        } catch {
            completion(false, String(describing: error))
        }
    }

    @objc func configureWebDAVSync() {
        showWebDAVConfigureDialog()
    }

    @objc func toggleWebDAVSync() {
        guard !busy, !terminationCleanupInFlight else { return }
        setBusy(true, title: "Status: Checking WebDAV sync")
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let enabled = self.isWebDAVSyncEnabled()
            let settings = enabled ? nil : self.readWebDAVSyncSettings()
            DispatchQueue.main.async {
                guard !self.terminationCleanupInFlight else { return }
                self.setBusy(false, title: "Status: Checking")
                if !enabled,
                   let settings,
                   !self.isWebDAVConfigured(settings) {
                    self.showWebDAVConfigureDialog(
                        enableAfterSave: true,
                        initialSettings: settings
                    )
                    return
                }
                self.performWebDAVControl(
                    action: enabled ? "webdav-disable" : "webdav-enable",
                    workingTitle: enabled ? "Disabling WebDAV sync" : "Enabling WebDAV sync",
                    successTitle: enabled ? "WebDAV sync disabled" : "WebDAV sync enabled"
                )
            }
        }
    }

    @objc func openLogs() {
        showLogs(tab: .requests)
    }

    func showLogs(tab: LogWindowTab) {
        let controller: LogWindowController
        if let existing = logWindowController {
            controller = existing
        } else {
            controller = LogWindowController(runtimeRoot: root, bundleRoot: bundleRoot)
            logWindowController = controller
        }
        controller.show(initialTab: tab)
    }

    func showAlert(
        title: String,
        message: String,
        style: NSAlert.Style = .warning
    ) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = shortAlertMessage(message)
        alert.alertStyle = style
        _ = runSettingsModal(alert)
    }

    @objc func quitLiteLLM() { NSApp.terminate(nil) }
}
