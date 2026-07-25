import Cocoa

enum ConfigurationPackageDialogResult {
    case importPackage
    case export(sections: [String])
}

final class ConfigurationPackageDialogController: NSObject, NSWindowDelegate {
    private let panelWidth: CGFloat = 420
    private let importPanelHeight: CGFloat = 132
    private let exportPanelHeight: CGFloat = 208
    private var result: ConfigurationPackageDialogResult?
    private var didStopModal = false
    private let modeControl = NSSegmentedControl(
        labels: ["Import", "Export"],
        trackingMode: .selectOne,
        target: nil,
        action: nil
    )
    private let runtimeCheckbox = NSButton(checkboxWithTitle: "Runtime Settings", target: nil, action: nil)
    private let providersCheckbox = NSButton(checkboxWithTitle: "Providers & Models", target: nil, action: nil)
    private let sectionsStack = NSStackView()
    private let actionButton = NSButton(title: "Choose File…", target: nil, action: nil)
    private var window: NSPanel!

    override init() {
        super.init()
        buildWindow()
    }

    func runModal() -> ConfigurationPackageDialogResult? {
        beginSettingsWindowPresentation(window)
        window.center()
        window.makeKeyAndOrderFront(nil)
        let response = NSApp.runModal(for: window)
        window.orderOut(nil)
        endSettingsWindowPresentation(window)
        return response == .OK ? result : nil
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        stopModal(with: .cancel)
        return false
    }

    @objc private func modeChanged(_ sender: NSSegmentedControl) {
        refreshMode()
    }

    @objc private func sectionChanged(_ sender: NSButton) {
        refreshMode()
    }

    @objc private func continueAction(_ sender: NSButton) {
        if modeControl.selectedSegment == 0 {
            result = .importPackage
        } else {
            var sections: [String] = []
            if runtimeCheckbox.state == .on { sections.append("runtime_settings") }
            if providersCheckbox.state == .on { sections.append("providers_models") }
            guard !sections.isEmpty else { return }
            result = .export(sections: sections)
        }
        stopModal(with: .OK)
    }

    @objc private func cancelAction(_ sender: NSButton) {
        stopModal(with: .cancel)
    }

    private func stopModal(with response: NSApplication.ModalResponse) {
        guard !didStopModal else { return }
        didStopModal = true
        NSApp.stopModal(withCode: response)
    }

    private func refreshMode() {
        let exporting = modeControl.selectedSegment == 1
        sectionsStack.isHidden = !exporting
        actionButton.title = exporting ? "Export…" : "Choose File…"
        actionButton.isEnabled = !exporting
            || runtimeCheckbox.state == .on
            || providersCheckbox.state == .on
        window.setContentSize(NSSize(
            width: panelWidth,
            height: exporting ? exportPanelHeight : importPanelHeight
        ))
    }

    private func buildWindow() {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: panelWidth, height: importPanelHeight),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        panel.title = "Import / Export Config"
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        panel.animationBehavior = .none
        window = panel

        let content = NSView()
        panel.contentView = content

        modeControl.target = self
        modeControl.action = #selector(modeChanged(_:))
        modeControl.selectedSegment = 0
        modeControl.segmentStyle = .rounded
        modeControl.setWidth(120, forSegment: 0)
        modeControl.setWidth(120, forSegment: 1)

        runtimeCheckbox.state = .on
        providersCheckbox.state = .on
        runtimeCheckbox.target = self
        providersCheckbox.target = self
        runtimeCheckbox.action = #selector(sectionChanged(_:))
        providersCheckbox.action = #selector(sectionChanged(_:))

        let sectionTitle = NSTextField(labelWithString: "Sections")
        sectionTitle.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        sectionTitle.textColor = .secondaryLabelColor
        sectionsStack.orientation = .vertical
        sectionsStack.alignment = .leading
        sectionsStack.spacing = 7
        sectionsStack.addArrangedSubview(sectionTitle)
        sectionsStack.addArrangedSubview(runtimeCheckbox)
        sectionsStack.addArrangedSubview(providersCheckbox)

        let cancelButton = NSButton(title: "Cancel", target: self, action: #selector(cancelAction(_:)))
        cancelButton.bezelStyle = .rounded
        cancelButton.keyEquivalent = "\u{1b}"
        actionButton.target = self
        actionButton.action = #selector(continueAction(_:))
        actionButton.bezelStyle = .rounded
        actionButton.keyEquivalent = "\r"

        let buttons = NSStackView(views: [cancelButton, actionButton])
        buttons.orientation = .horizontal
        buttons.spacing = 8

        for view in [modeControl, sectionsStack, buttons] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        NSLayoutConstraint.activate([
            modeControl.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            modeControl.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            modeControl.widthAnchor.constraint(equalToConstant: 240),

            sectionsStack.leadingAnchor.constraint(equalTo: modeControl.leadingAnchor),
            sectionsStack.topAnchor.constraint(equalTo: modeControl.bottomAnchor, constant: 16),
            sectionsStack.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -20),

            buttons.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            buttons.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
            cancelButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 88),
            actionButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 108),
        ])
        refreshMode()
    }
}
