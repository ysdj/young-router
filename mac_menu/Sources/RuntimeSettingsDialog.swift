import Cocoa

final class RuntimeSettingsTextBorderView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        NSColor.textBackgroundColor.setFill()
        bounds.fill()
        NSColor.separatorColor.setStroke()
        let border = bounds.insetBy(dx: 0.5, dy: 0.5)
        NSBezierPath(rect: border).stroke()
    }
}

final class RuntimeSettingsTextView: NSTextView {
    override func scrollWheel(with event: NSEvent) {
        if let outerScrollView = nearestOuterScrollView() {
            outerScrollView.scrollWheel(with: event)
            return
        }
        super.scrollWheel(with: event)
    }

    private func nearestOuterScrollView() -> NSScrollView? {
        var view = superview
        while let current = view {
            if let scrollView = current as? NSScrollView {
                return scrollView
            }
            view = current.superview
        }
        return nil
    }
}

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
    }
}

struct RuntimeSettingsPayload: Codable {
    var path: String?
    var settings: [RuntimeSettingItem]
}

struct RuntimeSettingsSavePayload: Codable {
    var values: [String: String]
}

final class RuntimeSettingsDialogController: NSObject, NSWindowDelegate, NSTextFieldDelegate {
    var didStopModal = false
    var result: [String: String]?
    let settings: [RuntimeSettingItem]
    var fields: [String: NSView] = [:]
    var firstField: NSView?
    var applyButton: NSButton!
    var closeButton: NSButton!
    var resetButton: NSButton!
    var window: NSPanel!
    let labelColumnWidth: CGFloat = 155
    let unitColumnWidth: CGFloat = 55
    let formContentWidth: CGFloat = 810

    init(settings: [RuntimeSettingItem]) {
        self.settings = settings
        super.init()
        buildWindow()
    }

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

    @objc func applyAction(_ sender: Any?) {
        if let message = validationMessage() {
            showAlert(title: "Runtime settings invalid", message: message)
            return
        }
        result = currentValues()
        stopModal(with: .OK)
    }

    @objc func resetAction(_ sender: Any?) {
        for item in settings {
            setValue(item.defaultValue, for: item)
        }
    }

    @objc func closeAction(_ sender: Any?) {
        stopModal(with: .cancel)
    }

    func currentValues() -> [String: String] {
        var values: [String: String] = [:]
        for item in settings {
            values[item.key] = value(for: item)
        }
        return values
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
                continue
            case "int":
                guard let number = Double(value), number.isFinite, Int(value) != nil else {
                    return "\(item.label) must be an integer."
                }
                if let minimum = item.minimum, number < minimum {
                    return "\(item.label) must be at least \(formatBound(minimum))."
                }
                if let maximum = item.maximum, number > maximum {
                    return "\(item.label) must be at most \(formatBound(maximum))."
                }
            case "float", "mb":
                guard let number = Double(value), number.isFinite else {
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
        return nil
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

    func stopModal(with response: NSApplication.ModalResponse) {
        guard !didStopModal else { return }
        didStopModal = true
        NSApp.stopModal(withCode: response)
        window.orderOut(nil)
    }

    func trimmed(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func controlWidth(for item: RuntimeSettingItem) -> CGFloat {
        switch item.kind {
        case "bool", "bool_auto":
            return 90
        case "enum":
            return 190
        case "string":
            return 500
        default:
            return 140
        }
    }

    func isMultilineSetting(_ item: RuntimeSettingItem) -> Bool {
        item.kind == "string" && item.key.localizedCaseInsensitiveContains("PROMPT")
    }

    func defaultTipText(for item: RuntimeSettingItem) -> String {
        let defaultText = item.defaultValue.isEmpty ? "(empty)" : item.defaultValue
        var lines = ["Default: \(defaultText)"]
        if let help = item.help?.trimmingCharacters(in: .whitespacesAndNewlines), !help.isEmpty {
            lines.append(help)
        }
        return lines.joined(separator: "\n")
    }

    func estimatedWrappedLineCount(_ text: String, charactersPerLine: Int = 92) -> Int {
        text.split(separator: "\n", omittingEmptySubsequences: false).reduce(0) { total, line in
            total + max(1, Int(ceil(Double(line.count) / Double(charactersPerLine))))
        }
    }

    func estimatedRowHeight(for item: RuntimeSettingItem) -> CGFloat {
        let controlHeight: CGFloat = isMultilineSetting(item) ? 98 : 28
        let detailHeight = CGFloat(estimatedWrappedLineCount(defaultTipText(for: item))) * 15 + 8
        return controlHeight + detailHeight + 6
    }

    func configureTextField(_ field: NSTextField, item: RuntimeSettingItem) {
        field.stringValue = item.value
        field.placeholderString = item.defaultValue
        field.usesSingleLineMode = true
        field.lineBreakMode = item.kind == "string" ? .byTruncatingMiddle : .byTruncatingTail
        field.delegate = self
        field.toolTip = item.help
        field.translatesAutoresizingMaskIntoConstraints = false
        field.heightAnchor.constraint(equalToConstant: 24).isActive = true
        field.widthAnchor.constraint(equalToConstant: controlWidth(for: item)).isActive = true
    }

    func configureTextView(_ textView: NSTextView, item: RuntimeSettingItem) -> NSView {
        textView.string = item.value
        textView.font = NSFont.systemFont(ofSize: 13)
        textView.isRichText = false
        textView.importsGraphics = false
        textView.allowsUndo = true
        textView.isVerticallyResizable = false
        textView.isHorizontallyResizable = false
        textView.textContainer?.widthTracksTextView = true
        textView.textContainerInset = NSSize(width: 4, height: 4)
        textView.toolTip = item.help
        textView.drawsBackground = true
        textView.backgroundColor = .textBackgroundColor

        let container = RuntimeSettingsTextBorderView()
        container.wantsLayer = false
        container.translatesAutoresizingMaskIntoConstraints = false
        container.widthAnchor.constraint(equalToConstant: controlWidth(for: item)).isActive = true
        container.heightAnchor.constraint(equalToConstant: 96).isActive = true

        textView.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(textView)
        NSLayoutConstraint.activate([
            textView.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 1),
            textView.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -1),
            textView.topAnchor.constraint(equalTo: container.topAnchor, constant: 1),
            textView.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -1),
        ])
        return container
    }

    func settingControl(for item: RuntimeSettingItem) -> NSView {
        switch item.kind {
        case "bool", "bool_auto":
            let button = NSButton(checkboxWithTitle: "", target: nil, action: nil)
            button.state = boolValue(item.value) ? .on : .off
            button.toolTip = item.help
            button.translatesAutoresizingMaskIntoConstraints = false
            button.widthAnchor.constraint(equalToConstant: controlWidth(for: item)).isActive = true
            return button
        case "enum":
            let popup = NSPopUpButton(frame: .zero, pullsDown: false)
            popup.translatesAutoresizingMaskIntoConstraints = false
            popup.toolTip = item.help
            popup.addItems(withTitles: item.options ?? [])
            if let index = popup.itemTitles.firstIndex(of: item.value) {
                popup.selectItem(at: index)
            } else if popup.numberOfItems > 0 {
                popup.selectItem(at: 0)
            }
            popup.widthAnchor.constraint(equalToConstant: controlWidth(for: item)).isActive = true
            return popup
        default:
            if isMultilineSetting(item) {
                let textView = RuntimeSettingsTextView()
                return configureTextView(textView, item: item)
            }
            let field = NSTextField()
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
            return trimmed(field.stringValue)
        }
        if let container = control as? RuntimeSettingsTextBorderView,
           let textView = container.subviews.compactMap({ $0 as? NSTextView }).first {
            return trimmed(textView.string)
        }
        return item.defaultValue
    }

    func setValue(_ value: String, for item: RuntimeSettingItem) {
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
            field.stringValue = value
        }
        if let container = control as? RuntimeSettingsTextBorderView,
           let textView = container.subviews.compactMap({ $0 as? NSTextView }).first {
            textView.string = value
        }
    }

    func buildWindow() {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 620),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "Runtime Settings"
        panel.minSize = NSSize(width: 900, height: 500)
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        panel.animationBehavior = .none
        window = panel

        let content = NSView()
        panel.contentView = content

        let titleLabel = NSTextField(labelWithString: "Runtime Settings")
        titleLabel.font = NSFont.systemFont(ofSize: 16, weight: .semibold)

        let subtitleLabel = NSTextField(
            wrappingLabelWithString: "These values are saved as runtime defaults and applied after the service restarts."
        )
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.font = NSFont.systemFont(ofSize: 13)

        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.borderType = .bezelBorder
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        let formWidth: CGFloat = formContentWidth + 24
        let categoryCount = Set(settings.map { $0.category }).count
        let formHeight = max(
            CGFloat(240),
            settings.reduce(CGFloat(categoryCount * 34 + 18)) { total, item in
                total + estimatedRowHeight(for: item) + 8
            }
        )
        let formContainer = FlippedDocumentView(frame: NSRect(x: 0, y: 0, width: formWidth, height: formHeight))
        let formStack = NSStackView(frame: NSRect(x: 12, y: 10, width: formContentWidth, height: 1))
        formStack.orientation = .vertical
        formStack.spacing = 8
        formStack.alignment = .leading
        formStack.autoresizingMask = [.width]
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
                label.textColor = .labelColor
                formStack.addArrangedSubview(label)
            }
            formStack.addArrangedSubview(formRow(item))
        }

        formStack.layoutSubtreeIfNeeded()
        let stackHeight = formStack.fittingSize.height
        formStack.setFrameSize(NSSize(width: formContentWidth, height: stackHeight))
        formContainer.setFrameSize(NSSize(width: formWidth, height: max(formHeight, stackHeight + 20)))

        resetButton = NSButton(title: "Reset Defaults", target: self, action: #selector(resetAction(_:)))
        resetButton.bezelStyle = .rounded
        applyButton = NSButton(title: "Apply", target: self, action: #selector(applyAction(_:)))
        applyButton.bezelStyle = .rounded
        applyButton.keyEquivalent = "\r"
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
    }

    func settingsSpacer() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    func formRow(_ item: RuntimeSettingItem) -> NSStackView {
        let row = NSStackView()
        row.orientation = .vertical
        row.alignment = .leading
        row.spacing = 4
        row.translatesAutoresizingMaskIntoConstraints = false
        row.setContentHuggingPriority(.required, for: .vertical)
        row.setContentCompressionResistancePriority(.required, for: .vertical)
        row.widthAnchor.constraint(equalToConstant: formContentWidth).isActive = true

        let inputRow = NSStackView()
        inputRow.orientation = .horizontal
        inputRow.alignment = isMultilineSetting(item) ? .top : .centerY
        inputRow.spacing = 8
        inputRow.translatesAutoresizingMaskIntoConstraints = false
        inputRow.setContentHuggingPriority(.required, for: .vertical)
        inputRow.setContentCompressionResistancePriority(.required, for: .vertical)
        inputRow.heightAnchor.constraint(greaterThanOrEqualToConstant: isMultilineSetting(item) ? 98 : 26).isActive = true

        let labelView = NSTextField(labelWithString: item.label)
        labelView.alignment = .right
        labelView.toolTip = item.help
        labelView.translatesAutoresizingMaskIntoConstraints = false
        labelView.widthAnchor.constraint(equalToConstant: labelColumnWidth).isActive = true

        let control = settingControl(for: item)
        fields[item.key] = control
        if firstField == nil {
            firstField = control
        }

        let unitView = NSTextField(labelWithString: item.unit ?? "")
        unitView.textColor = .secondaryLabelColor
        unitView.translatesAutoresizingMaskIntoConstraints = false
        unitView.widthAnchor.constraint(equalToConstant: unitColumnWidth).isActive = true

        inputRow.addArrangedSubview(labelView)
        inputRow.addArrangedSubview(control)
        inputRow.addArrangedSubview(unitView)

        let tipView = NSTextField(wrappingLabelWithString: defaultTipText(for: item))
        tipView.textColor = .tertiaryLabelColor
        tipView.font = NSFont.systemFont(ofSize: 11)
        tipView.usesSingleLineMode = false
        tipView.lineBreakMode = .byWordWrapping
        tipView.maximumNumberOfLines = 0
        tipView.translatesAutoresizingMaskIntoConstraints = false
        tipView.setContentCompressionResistancePriority(.required, for: .vertical)
        tipView.setContentCompressionResistancePriority(.required, for: .horizontal)
        tipView.toolTip = tipView.stringValue
        tipView.widthAnchor.constraint(equalToConstant: formContentWidth - labelColumnWidth - 8).isActive = true

        let tipRow = NSStackView()
        tipRow.orientation = .horizontal
        tipRow.alignment = .top
        tipRow.spacing = 8
        tipRow.translatesAutoresizingMaskIntoConstraints = false
        tipRow.setContentHuggingPriority(.required, for: .vertical)
        tipRow.setContentCompressionResistancePriority(.required, for: .vertical)

        let tipIndent = NSView()
        tipIndent.translatesAutoresizingMaskIntoConstraints = false
        tipIndent.widthAnchor.constraint(equalToConstant: labelColumnWidth).isActive = true
        tipRow.addArrangedSubview(tipIndent)
        tipRow.addArrangedSubview(tipView)

        row.addArrangedSubview(inputRow)
        row.addArrangedSubview(tipRow)
        return row
    }
}
