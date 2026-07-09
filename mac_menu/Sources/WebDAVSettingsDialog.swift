import Cocoa

struct WebDAVSettingsDialogResult {
    var url: String
    var username: String
    var password: String?
    var keepPassword: Bool
    var remoteName: String
    var syncIntervalMinutes: Int
    var timeoutSeconds: Double
}

final class WebDAVSettingsDialogController: NSObject, NSWindowDelegate, NSTextFieldDelegate {
    var didStopModal = false
    var result: WebDAVSettingsDialogResult?
    let hasExistingPassword: Bool
    let probeHandler: (WebDAVSettingsDialogResult, @escaping (Int32, String) -> Void) -> Void
    let urlField: NSTextField
    let usernameField: NSTextField
    let passwordField: NSSecureTextField
    let remoteNameField: NSTextField
    let syncIntervalField: NSTextField
    let timeoutField: NSTextField
    let probeStatusLabel: NSTextField
    var probeButton: NSButton!
    var applyButton: NSButton!
    var closeButton: NSButton!
    var window: NSPanel!

    init(
        url: String,
        username: String,
        remoteName: String,
        syncIntervalMinutes: Int,
        timeoutSeconds: Double,
        password: String,
        hasExistingPassword: Bool,
        probeHandler: @escaping (WebDAVSettingsDialogResult, @escaping (Int32, String) -> Void) -> Void
    ) {
        self.hasExistingPassword = hasExistingPassword
        self.probeHandler = probeHandler
        urlField = NSTextField()
        usernameField = NSTextField()
        passwordField = NSSecureTextField()
        remoteNameField = NSTextField()
        syncIntervalField = NSTextField()
        timeoutField = NSTextField()
        probeStatusLabel = NSTextField(labelWithString: "")
        super.init()

        configureField(urlField, value: url, placeholder: "https://example.com/webdav/litellm/")
        configureField(usernameField, value: username, placeholder: "optional")
        configureField(
            passwordField,
            value: password,
            placeholder: hasExistingPassword ? "leave blank to keep current password" : "optional"
        )
        configureField(remoteNameField, value: remoteName, placeholder: defaultWebDAVRemoteName)
        configureField(syncIntervalField, value: String(syncIntervalMinutes), placeholder: String(defaultWebDAVSyncIntervalMinutes))
        configureField(timeoutField, value: formatSeconds(timeoutSeconds), placeholder: String(defaultWebDAVTimeoutSeconds))
        buildWindow()
    }

    func runModal() -> WebDAVSettingsDialogResult? {
        NSApp.activate(ignoringOtherApps: true)
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(urlField)
        let response = NSApp.runModal(for: window)
        window.orderOut(nil)
        return response == .OK ? result : nil
    }

    func windowWillClose(_ notification: Notification) {
        stopModal(with: .cancel)
    }

    @objc func applyAction(_ sender: Any?) {
        result = currentResult()
        stopModal(with: .OK)
    }

    @objc func probeAction(_ sender: Any?) {
        let probeResult = currentResult()
        setProbeInFlight(true)
        probeHandler(probeResult) { [weak self] exitCode, output in
            DispatchQueue.main.async {
                guard let self else { return }
                self.setProbeInFlight(false)
                if exitCode == 0 {
                    self.probeStatusLabel.textColor = .systemGreen
                    self.probeStatusLabel.stringValue = "Probe OK"
                } else {
                    self.probeStatusLabel.textColor = .systemRed
                    self.probeStatusLabel.stringValue = "Probe failed"
                }
                let title = exitCode == 0 ? "WebDAV probe OK" : "WebDAV probe failed"
                self.showAlert(title: title, message: output)
            }
        }
    }

    @objc func closeAction(_ sender: Any?) {
        stopModal(with: .cancel)
    }

    func currentResult() -> WebDAVSettingsDialogResult {
        let password = passwordField.stringValue
        return WebDAVSettingsDialogResult(
            url: trimmed(urlField.stringValue),
            username: trimmed(usernameField.stringValue),
            password: password.isEmpty ? nil : password,
            keepPassword: password.isEmpty && hasExistingPassword,
            remoteName: trimmed(remoteNameField.stringValue),
            syncIntervalMinutes: normalizedSyncIntervalMinutes(syncIntervalField.stringValue),
            timeoutSeconds: normalizedTimeoutSeconds(timeoutField.stringValue)
        )
    }

    func setProbeInFlight(_ inFlight: Bool) {
        probeButton.isEnabled = !inFlight
        applyButton.isEnabled = !inFlight
        closeButton.isEnabled = !inFlight
        probeStatusLabel.textColor = .secondaryLabelColor
        probeStatusLabel.stringValue = inFlight ? "Probing..." : probeStatusLabel.stringValue
    }

    func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = shortAlertMessage(message)
        alert.alertStyle = title.localizedCaseInsensitiveContains("failed") ? .warning : .informational
        alert.beginSheetModal(for: window)
    }

    func stopModal(with response: NSApplication.ModalResponse) {
        guard !didStopModal else { return }
        didStopModal = true
        NSApp.stopModal(withCode: response)
        window.orderOut(nil)
    }

    func trimmed(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func normalizedSyncIntervalMinutes(_ value: String) -> Int {
        let trimmedValue = trimmed(value)
        guard !trimmedValue.isEmpty, let minutes = Int(trimmedValue) else {
            return defaultWebDAVSyncIntervalMinutes
        }
        return max(0, min(minutes, 24 * 60))
    }

    func normalizedTimeoutSeconds(_ value: String) -> Double {
        let trimmedValue = trimmed(value)
        guard !trimmedValue.isEmpty, let seconds = Double(trimmedValue), seconds.isFinite else {
            return Double(defaultWebDAVTimeoutSeconds)
        }
        return max(1, min(seconds, 600))
    }

    func formatSeconds(_ value: Double) -> String {
        if value.rounded() == value {
            return String(Int(value))
        }
        return String(format: "%.3f", value).trimmingCharacters(in: CharacterSet(charactersIn: "0")).trimmingCharacters(in: CharacterSet(charactersIn: "."))
    }

    func configureField(_ field: NSTextField, value: String, placeholder: String) {
        field.stringValue = value
        field.placeholderString = placeholder
        field.usesSingleLineMode = true
        field.lineBreakMode = .byTruncatingMiddle
        field.delegate = self
        field.translatesAutoresizingMaskIntoConstraints = false
        field.heightAnchor.constraint(equalToConstant: 24).isActive = true
    }

    func buildWindow() {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 680, height: 386),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "WebDAV Sync Settings"
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        panel.animationBehavior = .none
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window = panel

        let content = NSView()
        panel.contentView = content

        let titleLabel = NSTextField(labelWithString: "WebDAV Sync Settings")
        titleLabel.font = NSFont.systemFont(ofSize: 16, weight: .semibold)

        let subtitleLabel = NSTextField(
            wrappingLabelWithString: "Syncs the current LiteLLM Menu config, including provider keys and model routes."
        )
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.font = NSFont.systemFont(ofSize: 13)

        let formStack = NSStackView()
        formStack.orientation = .vertical
        formStack.spacing = 10
        formStack.alignment = .leading

        formStack.addArrangedSubview(formRow("URL", urlField))
        formStack.addArrangedSubview(formRow("Username", usernameField))
        formStack.addArrangedSubview(formRow("Password", passwordField))
        formStack.addArrangedSubview(formRow("Remote File", remoteNameField))
        formStack.addArrangedSubview(formRow("Sync Every", syncIntervalField, suffix: "minutes"))
        formStack.addArrangedSubview(formRow("HTTP Timeout", timeoutField, suffix: "seconds"))

        probeButton = NSButton(title: "Test", target: self, action: #selector(probeAction(_:)))
        probeButton.bezelStyle = .rounded
        applyButton = NSButton(title: "Apply", target: self, action: #selector(applyAction(_:)))
        applyButton.bezelStyle = .rounded
        applyButton.keyEquivalent = "\r"
        closeButton = NSButton(title: "Close", target: self, action: #selector(closeAction(_:)))
        closeButton.bezelStyle = .rounded
        closeButton.keyEquivalent = "\u{1b}"
        probeStatusLabel.textColor = .secondaryLabelColor
        probeStatusLabel.font = NSFont.systemFont(ofSize: 12)
        probeStatusLabel.translatesAutoresizingMaskIntoConstraints = false

        let buttonRow = NSStackView()
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY
        buttonRow.spacing = 8
        buttonRow.addArrangedSubview(closeButton)
        buttonRow.addArrangedSubview(applyButton)

        let bottomStack = NSStackView()
        bottomStack.orientation = .horizontal
        bottomStack.alignment = .centerY
        bottomStack.spacing = 10
        bottomStack.addArrangedSubview(probeButton)
        bottomStack.addArrangedSubview(probeStatusLabel)
        bottomStack.addArrangedSubview(settingsSpacer())
        bottomStack.addArrangedSubview(buttonRow)

        let views: [NSView] = [titleLabel, subtitleLabel, formStack, bottomStack]
        for view in views {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }

        NSLayoutConstraint.activate([
            titleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            titleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            titleLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),

            subtitleLabel.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            subtitleLabel.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor),
            subtitleLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 6),

            formStack.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            formStack.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor),
            formStack.topAnchor.constraint(equalTo: subtitleLabel.bottomAnchor, constant: 18),

            bottomStack.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            bottomStack.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor),
            bottomStack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -18),
            bottomStack.heightAnchor.constraint(greaterThanOrEqualToConstant: 30),
            formStack.bottomAnchor.constraint(lessThanOrEqualTo: bottomStack.topAnchor, constant: -18),

            probeButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 92),
            applyButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 92),
            closeButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 92),
        ])
    }

    func settingsSpacer() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    func formRow(_ label: String, _ control: NSView, suffix: String? = nil) -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .top
        row.spacing = 10

        let labelView = NSTextField(labelWithString: label)
        labelView.alignment = .right
        labelView.translatesAutoresizingMaskIntoConstraints = false
        labelView.widthAnchor.constraint(equalToConstant: 94).isActive = true

        row.addArrangedSubview(labelView)
        row.addArrangedSubview(control)
        control.widthAnchor.constraint(equalToConstant: suffix == nil ? 510 : 140).isActive = true
        if let suffix {
            let suffixView = NSTextField(labelWithString: suffix)
            suffixView.textColor = .secondaryLabelColor
            row.addArrangedSubview(suffixView)
        }
        return row
    }
}
