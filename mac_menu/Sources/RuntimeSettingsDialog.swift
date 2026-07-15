import Cocoa

struct RuntimeSettingItem: Codable {
    var key: String
    var category: String
    var label: String
    var unit: String?
    var kind: String
    var defaultValue: String
    var value: String
    var options: [String]?
    var minimum: Double?
    var maximum: Double?
    var help: String?
    var configured: Bool?
    var secret: Bool?
    var retainExisting: String?

    enum CodingKeys: String, CodingKey {
        case key
        case category
        case label
        case unit
        case kind
        case defaultValue = "default"
        case value
        case options
        case minimum
        case maximum
        case help
        case configured
        case secret
        case retainExisting = "retain_existing"
    }
}

struct RuntimeSettingsPayload: Codable {
    var path: String?
    var settings: [RuntimeSettingItem]
}

struct RuntimeSettingsSavePayload: Codable {
    var values: [String: String]
}

typealias RuntimeSettingsSaveHandler = (
    [String: String],
    @escaping (_ succeeded: Bool, _ message: String) -> Void
) -> Void

final class RuntimeSettingsDialogController: NSObject, NSWindowDelegate, NSTextFieldDelegate {
    static let retainExistingValue = "__LITELLM_MENU_RETAIN_EXISTING__"

    var didStopModal = false
    var result: [String: String]?
    let settings: [RuntimeSettingItem]
    let serviceRunning: Bool
    let saveHandler: RuntimeSettingsSaveHandler?
    var fields: [String: NSView] = [:]
    var sensitiveClearButtons: [String: NSButton] = [:]
    var clearedSensitiveKeys: Set<String> = []
    var firstField: NSView?
    var applyButton: NSButton!
    var closeButton: NSButton!
    var resetButton: NSButton!
    var window: NSPanel!
    var initialValues: [String: String] = [:]
    var dirtyKeys: Set<String> = []
    var saveInFlight = false
    let labelColumnWidth: CGFloat = 155
    let actionColumnWidth: CGFloat = 76
    let unitColumnWidth: CGFloat = 70
    let formColumnSpacing: CGFloat = 8

    init(
        settings: [RuntimeSettingItem],
        serviceRunning: Bool = true,
        saveHandler: RuntimeSettingsSaveHandler? = nil
    ) {
        self.settings = settings
        self.serviceRunning = serviceRunning
        self.saveHandler = saveHandler
        super.init()
        buildWindow()
    }

    @discardableResult
    func runModal() -> [String: String]? {
        NSApp.activate(ignoringOtherApps: true)
        window.center()
        window.makeKeyAndOrderFront(nil)
        if let firstField {
            window.makeFirstResponder(firstField)
        }
        let response = NSApp.runModal(for: window)
        window.orderOut(nil)
        return response == .OK ? result : nil
    }

    func windowWillClose(_ notification: Notification) {
        stopModal(with: .cancel)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        requestClose()
        return false
    }

    @objc func applyAction(_ sender: Any?) {
        guard !saveInFlight, !dirtyKeys.isEmpty else { return }
        if let message = validationMessage() {
            showAlert(title: "Runtime settings invalid", message: message)
            return
        }
        let values = changedValues()
        guard let saveHandler else {
            result = values
            stopModal(with: .OK)
            return
        }

        setSaveInFlight(true)
        saveHandler(values) { [weak self] succeeded, message in
            DispatchQueue.main.async {
                guard let self, self.saveInFlight else { return }
                if succeeded {
                    self.showSaveSuccess(message: message) {
                        self.result = values
                        self.stopModal(with: .OK)
                    }
                    return
                }
                self.setSaveInFlight(false)
                self.showAlert(
                    title: "Runtime settings failed",
                    message: message.isEmpty ? "The settings could not be saved." : message
                )
            }
        }
    }

    @objc func resetAction(_ sender: Any?) {
        let alert = NSAlert()
        alert.messageText = "Restore all runtime defaults?"
        alert.informativeText = "The defaults will be staged in this window. They are not saved until you choose \(saveButtonTitle)."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Restore Defaults")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard response == .alertFirstButtonReturn else { return }
            self?.restoreDefaults()
        }
    }

    func restoreDefaults() {
        for item in settings {
            setValue(item.defaultValue, for: item, explicitlyClearingSensitiveValue: true)
        }
        refreshDirtyState()
    }

    @objc func closeAction(_ sender: Any?) {
        requestClose()
    }

    func currentValues() -> [String: String] {
        var values: [String: String] = [:]
        for item in settings {
            values[item.key] = value(for: item)
        }
        return values
    }

    func changedValues() -> [String: String] {
        let values = currentValues()
        return Dictionary(uniqueKeysWithValues: dirtyKeys.compactMap { key in
            guard let value = values[key] else { return nil }
            return (key, value)
        })
    }

    func validationMessage() -> String? {
        for item in settings {
            let value = trimmed(value(for: item))
            switch item.kind {
            case "bool", "bool_auto":
                continue
            case "enum":
                if let options = item.options, !options.contains(value) {
                    return "\(item.label) must be one of: \(options.joined(separator: ", "))."
                }
            case "string":
                if value.contains("\n") || value.contains("\r") || value.contains("#") {
                    return "\(item.label) cannot contain line breaks or #."
                }
                if item.key == "LITELLM_MENU_WEB_SEARCH_REGION",
                   value.rangeOfCharacter(from: .whitespacesAndNewlines) != nil {
                    return "\(item.label) cannot contain whitespace."
                }
                continue
            case "int":
                guard value.range(of: #"^[0-9]+$"#, options: .regularExpression) != nil,
                      let number = Double(value), number.isFinite else {
                    return "\(item.label) must be an integer."
                }
                if let minimum = item.minimum, number < minimum {
                    return "\(item.label) must be at least \(formatBound(minimum))."
                }
                if let maximum = item.maximum, number > maximum {
                    return "\(item.label) must be at most \(formatBound(maximum))."
                }
            case "float", "mb":
                guard value.range(of: #"^[0-9]+(?:\.[0-9]+)?$"#, options: .regularExpression) != nil,
                      let number = Double(value), number.isFinite else {
                    return "\(item.label) must be a number."
                }
                if let minimum = item.minimum, number < minimum {
                    return "\(item.label) must be at least \(formatBound(minimum))."
                }
                if let maximum = item.maximum, number > maximum {
                    return "\(item.label) must be at most \(formatBound(maximum))."
                }
            default:
                continue
            }
        }
        if let searchResults = numericValue(forKey: "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS"),
           let readablePages = numericValue(forKey: "LITELLM_MENU_WEB_SEARCH_READ_RESULTS"),
           readablePages > searchResults {
            return "Readable pages cannot be greater than Search results."
        }
        return nil
    }

    func numericValue(forKey key: String) -> Double? {
        guard let item = settings.first(where: { $0.key == key }) else { return nil }
        return Double(value(for: item))
    }

    var saveButtonTitle: String {
        serviceRunning ? "Save & Restart" : "Save"
    }

    var subtitleText: String {
        if serviceRunning {
            return "Saving these runtime defaults restarts the LiteLLM service."
        }
        return "These runtime defaults take effect the next time the LiteLLM service starts."
    }

    func isSensitiveSetting(_ item: RuntimeSettingItem) -> Bool {
        item.secret == true || item.key == "LITELLM_MENU_VISION_BRIDGE_API_KEY"
    }

    func retainExistingValue(for item: RuntimeSettingItem) -> String {
        item.retainExisting ?? Self.retainExistingValue
    }

    func hasRetainedSensitiveValue(_ item: RuntimeSettingItem) -> Bool {
        isSensitiveSetting(item) && item.value == retainExistingValue(for: item)
    }

    func accessibilityLabel(for item: RuntimeSettingItem) -> String {
        "\(item.category) \(item.label)"
    }

    func requestClose() {
        guard !saveInFlight else { return }
        guard !dirtyKeys.isEmpty else {
            stopModal(with: .cancel)
            return
        }

        let alert = NSAlert()
        alert.messageText = "Discard unsaved runtime settings?"
        alert.informativeText = "Your changes in this window have not been saved."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Discard Changes")
        alert.addButton(withTitle: "Keep Editing")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard response == .alertFirstButtonReturn else { return }
            self?.stopModal(with: .cancel)
        }
    }

    func setSaveInFlight(_ inFlight: Bool) {
        saveInFlight = inFlight
        for control in fields.values {
            if let control = control as? NSControl {
                control.isEnabled = !inFlight
            }
        }
        for button in sensitiveClearButtons.values {
            button.isEnabled = !inFlight
        }
        resetButton.isEnabled = !inFlight
        closeButton.isEnabled = !inFlight
        applyButton.isEnabled = !inFlight && !dirtyKeys.isEmpty
        window.standardWindowButton(.closeButton)?.isEnabled = !inFlight
        applyButton.title = inFlight ? "Saving…" : saveButtonTitle
        if !inFlight {
            refreshDirtyState()
        }
    }

    @objc func settingControlChanged(_ sender: Any?) {
        refreshDirtyState()
    }

    func controlTextDidChange(_ notification: Notification) {
        if let field = notification.object as? NSTextField,
           let item = settings.first(where: { fields[$0.key] === field }),
           isSensitiveSetting(item) {
            if !trimmed(field.stringValue).isEmpty {
                clearedSensitiveKeys.remove(item.key)
            }
            updateSensitiveClearButton(for: item)
        }
        refreshDirtyState()
    }

    func refreshDirtyState() {
        guard !saveInFlight else { return }
        let values = currentValues()
        dirtyKeys = Set(settings.compactMap { item in
            values[item.key] == initialValues[item.key] ? nil : item.key
        })
        let isDirty = !dirtyKeys.isEmpty
        applyButton.isEnabled = isDirty
        applyButton.title = saveButtonTitle
        window.isDocumentEdited = isDirty
    }

    func formatBound(_ value: Double) -> String {
        if value.rounded() == value {
            return String(Int(value))
        }
        return String(value)
    }

    func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = shortAlertMessage(message)
        alert.alertStyle = .warning
        alert.beginSheetModal(for: window)
    }

    func showSaveSuccess(message: String, completion: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "Runtime settings saved"
        alert.informativeText = shortAlertMessage(
            message.isEmpty ? "Runtime settings were saved." : message
        )
        alert.alertStyle = .informational
        alert.beginSheetModal(for: window) { _ in
            completion()
        }
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

    func defaultTipText(for item: RuntimeSettingItem) -> String {
        let defaultText: String
        if isSensitiveSetting(item), item.defaultValue.isEmpty {
            defaultText = "(not configured)"
        } else {
            defaultText = item.defaultValue.isEmpty ? "(empty)" : item.defaultValue
        }
        var lines = ["Default: \(defaultText)"]
        if let help = item.help?.trimmingCharacters(in: .whitespacesAndNewlines), !help.isEmpty {
            lines.append(help)
        }
        return lines.joined(separator: "\n")
    }

    func configureTextField(_ field: NSTextField, item: RuntimeSettingItem) {
        if isSensitiveSetting(item) {
            field.stringValue = ""
            field.placeholderString = hasRetainedSensitiveValue(item)
                ? "Configured — leave blank to keep"
                : "Optional"
        } else {
            field.stringValue = item.value
            field.placeholderString = item.defaultValue
        }
        field.usesSingleLineMode = true
        field.lineBreakMode = item.kind == "string" ? .byTruncatingMiddle : .byTruncatingTail
        field.delegate = self
        field.toolTip = item.help
        field.setAccessibilityLabel(accessibilityLabel(for: item))
        field.setAccessibilityHelp(item.help)
        field.translatesAutoresizingMaskIntoConstraints = false
        field.heightAnchor.constraint(equalToConstant: 24).isActive = true
        field.setContentHuggingPriority(.defaultLow, for: .horizontal)
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    }

    func settingControl(for item: RuntimeSettingItem) -> NSView {
        switch item.kind {
        case "bool", "bool_auto":
            let button = NSButton(
                checkboxWithTitle: "",
                target: self,
                action: #selector(settingControlChanged(_:))
            )
            button.state = boolValue(item.value) ? .on : .off
            button.toolTip = item.help
            button.setAccessibilityLabel(accessibilityLabel(for: item))
            button.setAccessibilityHelp(item.help)
            button.translatesAutoresizingMaskIntoConstraints = false
            button.widthAnchor.constraint(equalToConstant: 90).isActive = true
            return button
        case "enum":
            let popup = NSPopUpButton(frame: .zero, pullsDown: false)
            popup.target = self
            popup.action = #selector(settingControlChanged(_:))
            popup.translatesAutoresizingMaskIntoConstraints = false
            popup.toolTip = item.help
            popup.setAccessibilityLabel(accessibilityLabel(for: item))
            popup.setAccessibilityHelp(item.help)
            popup.addItems(withTitles: item.options ?? [])
            if let index = popup.itemTitles.firstIndex(of: item.value) {
                popup.selectItem(at: index)
            } else if popup.numberOfItems > 0 {
                popup.selectItem(at: 0)
            }
            popup.setContentHuggingPriority(.defaultLow, for: .horizontal)
            popup.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            return popup
        default:
            let field: NSTextField = isSensitiveSetting(item) ? NSSecureTextField() : NSTextField()
            configureTextField(field, item: item)
            return field
        }
    }

    func boolValue(_ value: String) -> Bool {
        switch value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "1", "true", "yes", "on", "auto", "enabled":
            return true
        default:
            return false
        }
    }

    func value(for item: RuntimeSettingItem) -> String {
        guard let control = fields[item.key] else {
            return item.defaultValue
        }
        if let button = control as? NSButton, item.kind == "bool" || item.kind == "bool_auto" {
            if item.kind == "bool_auto" {
                return button.state == .on ? "auto" : "off"
            }
            return button.state == .on ? "1" : "0"
        }
        if let popup = control as? NSPopUpButton, item.kind == "enum" {
            return popup.titleOfSelectedItem ?? item.defaultValue
        }
        if let field = control as? NSTextField {
            if isSensitiveSetting(item), trimmed(field.stringValue).isEmpty {
                if clearedSensitiveKeys.contains(item.key) {
                    return ""
                }
                if hasRetainedSensitiveValue(item) {
                    return retainExistingValue(for: item)
                }
            }
            return trimmed(field.stringValue)
        }
        return item.defaultValue
    }

    func setValue(
        _ value: String,
        for item: RuntimeSettingItem,
        explicitlyClearingSensitiveValue: Bool = false
    ) {
        guard let control = fields[item.key] else { return }
        if let button = control as? NSButton, item.kind == "bool" || item.kind == "bool_auto" {
            button.state = boolValue(value) ? .on : .off
            return
        }
        if let popup = control as? NSPopUpButton, item.kind == "enum" {
            if let index = popup.itemTitles.firstIndex(of: value) {
                popup.selectItem(at: index)
            } else if popup.numberOfItems > 0 {
                popup.selectItem(at: 0)
            }
            return
        }
        if let field = control as? NSTextField {
            if isSensitiveSetting(item) {
                let hadRetainedValue = hasRetainedSensitiveValue(item)
                field.stringValue = value == retainExistingValue(for: item) ? "" : value
                if explicitlyClearingSensitiveValue, hadRetainedValue {
                    clearedSensitiveKeys.insert(item.key)
                } else if !value.isEmpty {
                    clearedSensitiveKeys.remove(item.key)
                } else {
                    clearedSensitiveKeys.remove(item.key)
                }
                updateSensitiveClearButton(for: item)
            } else {
                field.stringValue = value
            }
        }
    }

    @objc func clearSensitiveSetting(_ sender: NSButton) {
        guard let item = settings.first(where: { sensitiveClearButtons[$0.key] === sender }),
              let field = fields[item.key] as? NSTextField else { return }
        field.stringValue = ""
        clearedSensitiveKeys.insert(item.key)
        updateSensitiveClearButton(for: item)
        refreshDirtyState()
    }

    func updateSensitiveClearButton(for item: RuntimeSettingItem) {
        guard let button = sensitiveClearButtons[item.key] else { return }
        button.title = clearedSensitiveKeys.contains(item.key) ? "Will Clear" : "Clear"
        button.isEnabled = !saveInFlight
            && (hasRetainedSensitiveValue(item) || !trimmed(value(for: item)).isEmpty)
    }

    func buildWindow() {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 620),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "Runtime Settings"
        panel.minSize = NSSize(width: 760, height: 500)
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        panel.animationBehavior = .none
        window = panel

        let content = NSView()
        panel.contentView = content

        let titleLabel = NSTextField(labelWithString: "Runtime Settings")
        titleLabel.font = NSFont.systemFont(ofSize: 16, weight: .semibold)

        let subtitleLabel = NSTextField(
            wrappingLabelWithString: subtitleText
        )
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.font = NSFont.systemFont(ofSize: 13)

        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.borderType = .bezelBorder
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        let formContainer = FlippedDocumentView()
        formContainer.translatesAutoresizingMaskIntoConstraints = false
        let formStack = NSStackView()
        formStack.orientation = .vertical
        formStack.spacing = 8
        formStack.alignment = .width
        formStack.translatesAutoresizingMaskIntoConstraints = false
        formStack.setContentHuggingPriority(.required, for: .vertical)
        formStack.setContentCompressionResistancePriority(.required, for: .vertical)
        formContainer.addSubview(formStack)
        scrollView.documentView = formContainer

        var currentCategory: String?
        for item in settings {
            if currentCategory != item.category {
                currentCategory = item.category
                let label = NSTextField(labelWithString: item.category)
                label.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
                label.alignment = .left
                label.textColor = .labelColor
                label.setContentHuggingPriority(.required, for: .horizontal)
                label.setContentCompressionResistancePriority(.required, for: .horizontal)
                label.translatesAutoresizingMaskIntoConstraints = false
                if #available(macOS 26.0, *) {
                    label.setAccessibilityRole(NSAccessibility.Role(rawValue: "AXHeading"))
                }
                formStack.addArrangedSubview(label)
            }
            formStack.addArrangedSubview(formRow(item))
        }

        for arrangedSubview in formStack.arrangedSubviews {
            arrangedSubview.widthAnchor.constraint(equalTo: formStack.widthAnchor).isActive = true
        }

        NSLayoutConstraint.activate([
            formContainer.widthAnchor.constraint(equalTo: scrollView.contentView.widthAnchor),
            formContainer.heightAnchor.constraint(greaterThanOrEqualTo: scrollView.contentView.heightAnchor),
            formStack.leadingAnchor.constraint(equalTo: formContainer.leadingAnchor, constant: 12),
            formStack.trailingAnchor.constraint(equalTo: formContainer.trailingAnchor, constant: -12),
            formStack.topAnchor.constraint(equalTo: formContainer.topAnchor, constant: 10),
            formStack.bottomAnchor.constraint(equalTo: formContainer.bottomAnchor, constant: -10),
        ])

        resetButton = NSButton(title: "Restore Defaults…", target: self, action: #selector(resetAction(_:)))
        resetButton.bezelStyle = .rounded
        applyButton = NSButton(title: saveButtonTitle, target: self, action: #selector(applyAction(_:)))
        applyButton.bezelStyle = .rounded
        applyButton.keyEquivalent = "\r"
        applyButton.isEnabled = false
        closeButton = NSButton(title: "Close", target: self, action: #selector(closeAction(_:)))
        closeButton.bezelStyle = .rounded
        closeButton.keyEquivalent = "\u{1b}"

        let buttonRow = NSStackView()
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY
        buttonRow.spacing = 8
        buttonRow.addArrangedSubview(closeButton)
        buttonRow.addArrangedSubview(applyButton)

        let bottomStack = NSStackView()
        bottomStack.orientation = .horizontal
        bottomStack.alignment = .centerY
        bottomStack.spacing = 8
        bottomStack.addArrangedSubview(resetButton)
        bottomStack.addArrangedSubview(settingsSpacer())
        bottomStack.addArrangedSubview(buttonRow)

        let views: [NSView] = [titleLabel, subtitleLabel, scrollView, bottomStack]
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

            scrollView.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: subtitleLabel.bottomAnchor, constant: 14),
            scrollView.bottomAnchor.constraint(equalTo: bottomStack.topAnchor, constant: -12),

            bottomStack.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor),
            bottomStack.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor),
            bottomStack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
            bottomStack.heightAnchor.constraint(greaterThanOrEqualToConstant: 30),

            resetButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 116),
            applyButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 92),
            closeButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 92),
        ])

        initialValues = currentValues()
        refreshDirtyState()
    }

    func settingsSpacer() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    func formRow(_ item: RuntimeSettingItem) -> NSView {
        let row = NSView()
        row.translatesAutoresizingMaskIntoConstraints = false
        row.setContentHuggingPriority(.required, for: .vertical)
        row.setContentCompressionResistancePriority(.required, for: .vertical)
        row.identifier = NSUserInterfaceItemIdentifier("RuntimeSettingsRow.\(item.key)")

        let inputRow = NSView()
        inputRow.translatesAutoresizingMaskIntoConstraints = false
        inputRow.setContentHuggingPriority(.required, for: .vertical)
        inputRow.setContentCompressionResistancePriority(.required, for: .vertical)
        inputRow.identifier = NSUserInterfaceItemIdentifier("RuntimeSettingsInputRow.\(item.key)")

        let labelView = NSTextField(labelWithString: item.label)
        labelView.alignment = .right
        labelView.toolTip = item.help
        labelView.translatesAutoresizingMaskIntoConstraints = false
        labelView.identifier = NSUserInterfaceItemIdentifier("RuntimeSettingsLabel.\(item.key)")

        let control = settingControl(for: item)
        fields[item.key] = control
        if firstField == nil {
            firstField = control
        }

        let valueSlot = NSView()
        valueSlot.translatesAutoresizingMaskIntoConstraints = false
        valueSlot.identifier = NSUserInterfaceItemIdentifier("RuntimeSettingsValueSlot.\(item.key)")

        let actionSlot = NSView()
        actionSlot.translatesAutoresizingMaskIntoConstraints = false
        actionSlot.identifier = NSUserInterfaceItemIdentifier("RuntimeSettingsActionSlot.\(item.key)")

        let unitView = NSTextField(labelWithString: item.unit ?? "")
        unitView.textColor = .secondaryLabelColor
        unitView.translatesAutoresizingMaskIntoConstraints = false
        unitView.identifier = NSUserInterfaceItemIdentifier("RuntimeSettingsUnit.\(item.key)")

        for view in [labelView, valueSlot, actionSlot, unitView] {
            inputRow.addSubview(view)
        }
        valueSlot.addSubview(control)

        NSLayoutConstraint.activate([
            inputRow.heightAnchor.constraint(greaterThanOrEqualToConstant: 26),

            labelView.leadingAnchor.constraint(equalTo: inputRow.leadingAnchor),
            labelView.widthAnchor.constraint(equalToConstant: labelColumnWidth),
            labelView.centerYAnchor.constraint(equalTo: inputRow.centerYAnchor),

            valueSlot.leadingAnchor.constraint(
                equalTo: labelView.trailingAnchor,
                constant: formColumnSpacing
            ),
            valueSlot.topAnchor.constraint(equalTo: inputRow.topAnchor),
            valueSlot.bottomAnchor.constraint(equalTo: inputRow.bottomAnchor),

            actionSlot.leadingAnchor.constraint(
                equalTo: valueSlot.trailingAnchor,
                constant: formColumnSpacing
            ),
            actionSlot.widthAnchor.constraint(equalToConstant: actionColumnWidth),
            actionSlot.topAnchor.constraint(equalTo: inputRow.topAnchor),
            actionSlot.bottomAnchor.constraint(equalTo: inputRow.bottomAnchor),

            unitView.leadingAnchor.constraint(
                equalTo: actionSlot.trailingAnchor,
                constant: formColumnSpacing
            ),
            unitView.trailingAnchor.constraint(equalTo: inputRow.trailingAnchor),
            unitView.widthAnchor.constraint(equalToConstant: unitColumnWidth),
            unitView.centerYAnchor.constraint(equalTo: inputRow.centerYAnchor),

            control.leadingAnchor.constraint(equalTo: valueSlot.leadingAnchor),
            control.centerYAnchor.constraint(equalTo: valueSlot.centerYAnchor),
            control.trailingAnchor.constraint(lessThanOrEqualTo: valueSlot.trailingAnchor),
        ])
        if item.kind == "bool" || item.kind == "bool_auto" {
            control.setContentHuggingPriority(.required, for: .horizontal)
        } else {
            control.trailingAnchor.constraint(equalTo: valueSlot.trailingAnchor).isActive = true
        }

        if isSensitiveSetting(item) {
            let clearButton = NSButton(
                title: "Clear",
                target: self,
                action: #selector(clearSensitiveSetting(_:))
            )
            clearButton.bezelStyle = .rounded
            clearButton.setAccessibilityLabel("Clear \(accessibilityLabel(for: item))")
            clearButton.translatesAutoresizingMaskIntoConstraints = false
            sensitiveClearButtons[item.key] = clearButton
            actionSlot.addSubview(clearButton)
            NSLayoutConstraint.activate([
                clearButton.leadingAnchor.constraint(equalTo: actionSlot.leadingAnchor),
                clearButton.trailingAnchor.constraint(equalTo: actionSlot.trailingAnchor),
                clearButton.centerYAnchor.constraint(equalTo: actionSlot.centerYAnchor),
            ])
            updateSensitiveClearButton(for: item)
        }

        let tipView = NSTextField(wrappingLabelWithString: defaultTipText(for: item))
        tipView.textColor = .tertiaryLabelColor
        tipView.font = NSFont.systemFont(ofSize: 11)
        tipView.usesSingleLineMode = false
        tipView.lineBreakMode = .byWordWrapping
        tipView.maximumNumberOfLines = 0
        tipView.translatesAutoresizingMaskIntoConstraints = false
        tipView.setContentCompressionResistancePriority(.required, for: .vertical)
        tipView.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        tipView.toolTip = tipView.stringValue

        let helpSlot = NSView()
        helpSlot.translatesAutoresizingMaskIntoConstraints = false
        helpSlot.setContentHuggingPriority(.required, for: .vertical)
        helpSlot.setContentCompressionResistancePriority(.required, for: .vertical)
        helpSlot.identifier = NSUserInterfaceItemIdentifier("RuntimeSettingsHelp.\(item.key)")
        helpSlot.addSubview(tipView)
        NSLayoutConstraint.activate([
            tipView.leadingAnchor.constraint(equalTo: helpSlot.leadingAnchor),
            tipView.trailingAnchor.constraint(equalTo: helpSlot.trailingAnchor),
            tipView.topAnchor.constraint(equalTo: helpSlot.topAnchor),
            tipView.bottomAnchor.constraint(equalTo: helpSlot.bottomAnchor),
        ])

        row.addSubview(inputRow)
        row.addSubview(helpSlot)
        NSLayoutConstraint.activate([
            inputRow.leadingAnchor.constraint(equalTo: row.leadingAnchor),
            inputRow.trailingAnchor.constraint(equalTo: row.trailingAnchor),
            inputRow.topAnchor.constraint(equalTo: row.topAnchor),

            helpSlot.leadingAnchor.constraint(equalTo: valueSlot.leadingAnchor),
            helpSlot.trailingAnchor.constraint(equalTo: row.trailingAnchor),
            helpSlot.topAnchor.constraint(equalTo: inputRow.bottomAnchor, constant: 4),
            helpSlot.bottomAnchor.constraint(equalTo: row.bottomAnchor),
        ])
        return row
    }
}
