import Cocoa

extension ModelConfigEditorController {
    func buildWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1220, height: 700),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Edit LiteLLM Providers & Models"
        window.minSize = NSSize(width: 1220, height: 700)
        window.animationBehavior = .none
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.isReleasedWhenClosed = false
        window.delegate = self
        self.window = window

        let contentView = NSView()
        window.contentView = contentView

        let contentGuide = NSView()
        contentGuide.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(contentGuide)

        NSLayoutConstraint.activate([
            contentGuide.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 16),
            contentGuide.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -16),
            contentGuide.topAnchor.constraint(equalTo: contentView.topAnchor, constant: 16),
            contentGuide.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -16),
        ])

        let modeStack = NSStackView()
        modeStack.orientation = .horizontal
        modeStack.spacing = 8
        modeStack.alignment = .centerY
        modeStack.translatesAutoresizingMaskIntoConstraints = false
        modeStack.addArrangedSubview(viewModeControl)
        modeStack.addArrangedSubview(fixedSpacer())
        contentGuide.addSubview(modeStack)

        let listHeight: CGFloat = 500
        let listPaneWidth: CGFloat = 600

        let mainStack = NSStackView()
        mainStack.orientation = .horizontal
        mainStack.alignment = .top
        mainStack.spacing = 16
        mainStack.translatesAutoresizingMaskIntoConstraints = false
        contentGuide.addSubview(mainStack)

        let listPane = NSView()
        listPane.widthAnchor.constraint(equalToConstant: listPaneWidth).isActive = true
        listPane.heightAnchor.constraint(greaterThanOrEqualToConstant: listHeight + 86).isActive = true
        mainStack.addArrangedSubview(listPane)

        let cascadeStack = NSStackView()
        cascadeStack.orientation = .horizontal
        cascadeStack.alignment = .top
        cascadeStack.spacing = 14
        cascadeStack.translatesAutoresizingMaskIntoConstraints = false
        listPane.addSubview(cascadeStack)
        providerCascadeView = cascadeStack
        NSLayoutConstraint.activate([
            cascadeStack.leadingAnchor.constraint(equalTo: listPane.leadingAnchor),
            cascadeStack.trailingAnchor.constraint(lessThanOrEqualTo: listPane.trailingAnchor),
            cascadeStack.topAnchor.constraint(equalTo: listPane.topAnchor),
            cascadeStack.bottomAnchor.constraint(lessThanOrEqualTo: listPane.bottomAnchor),
        ])

        let providerStack = cascadeColumn(title: "Providers", width: 220)
        configureProviderTable()
        providerStack.stack.addArrangedSubview(scrollView(for: providerTableView, height: listHeight))
        let providerButtons = NSStackView()
        providerButtons.orientation = .horizontal
        providerButtons.spacing = 8
        let addProviderButton = NSButton(title: "Add", target: self, action: #selector(addProvider))
        for button in [addProviderButton, deleteProviderButton] {
            button.bezelStyle = .rounded
            providerButtons.addArrangedSubview(button)
        }
        providerButtons.addArrangedSubview(spacer())
        providerStack.stack.addArrangedSubview(providerButtons)
        cascadeStack.addArrangedSubview(providerStack.view)

        let modelStack = cascadeColumn(title: "Models", width: 340)
        configureModelTable()
        modelStack.stack.addArrangedSubview(scrollView(for: modelTableView, height: listHeight))
        let modelButtons = NSStackView()
        modelButtons.orientation = .horizontal
        modelButtons.spacing = 8
        for button in [addModelButton, duplicateModelButton, deleteModelButton] {
            button.bezelStyle = .rounded
            modelButtons.addArrangedSubview(button)
        }
        modelButtons.addArrangedSubview(spacer())
        modelStack.stack.addArrangedSubview(modelButtons)
        let modelFetchRow = NSStackView()
        modelFetchRow.orientation = .horizontal
        modelFetchRow.spacing = 8
        modelFetchRow.addArrangedSubview(modelCandidateApiKeyPopupButton)
        modelFetchRow.addArrangedSubview(fetchModelsButton)
        modelFetchRow.addArrangedSubview(spacer())
        modelStack.stack.addArrangedSubview(modelFetchRow)
        cascadeStack.addArrangedSubview(modelStack.view)

        let routeStack = cascadeColumn(title: "Routes", width: listPaneWidth)
        configureRouteTable()
        routeStack.stack.addArrangedSubview(scrollView(for: routeTableView, height: listHeight))
        let routeButtons = NSStackView()
        routeButtons.orientation = .horizontal
        routeButtons.spacing = 8
        for button in [routeMoveUpButton, routeMoveDownButton, routeNormalizeButton] {
            button.bezelStyle = .rounded
            routeButtons.addArrangedSubview(button)
        }
        routeButtons.addArrangedSubview(spacer())
        routeStack.stack.addArrangedSubview(routeButtons)
        routeStack.view.isHidden = true
        routeStack.view.translatesAutoresizingMaskIntoConstraints = false
        routesListView = routeStack.view
        listPane.addSubview(routeStack.view)
        NSLayoutConstraint.activate([
            routeStack.view.leadingAnchor.constraint(equalTo: listPane.leadingAnchor),
            routeStack.view.trailingAnchor.constraint(equalTo: listPane.trailingAnchor),
            routeStack.view.topAnchor.constraint(equalTo: listPane.topAnchor),
            routeStack.view.bottomAnchor.constraint(lessThanOrEqualTo: listPane.bottomAnchor),
        ])

        let formStack = NSStackView()
        formStack.orientation = .vertical
        formStack.spacing = 12
        formStack.alignment = .leading
        formStack.widthAnchor.constraint(equalToConstant: 560).isActive = true
        mainStack.addArrangedSubview(formStack)

        let detailPane = NSView()
        detailPane.widthAnchor.constraint(equalToConstant: 560).isActive = true
        detailPane.heightAnchor.constraint(equalToConstant: 312).isActive = true
        formStack.addArrangedSubview(detailPane)

        let providerSection = sectionStack(title: "Selected Provider")
        let providerSectionStack = providerSection.stack
        providerSectionStack.addArrangedSubview(providerEnabledCheckbox)
        providerSectionStack.addArrangedSubview(formRow("Provider name", providerNameField))
        providerSectionStack.addArrangedSubview(formRow("Base URL", providerApiBaseField))
        configureProviderKeyTable()
        providerSectionStack.addArrangedSubview(providerKeysEditor())
        providerSection.view.translatesAutoresizingMaskIntoConstraints = false
        detailPane.addSubview(providerSection.view)
        providerDetailView = providerSection.view

        let modelSection = sectionStack(title: "Selected Model Deployment")
        let modelSectionStack = modelSection.stack
        modelSectionStack.addArrangedSubview(modelEnabledRow())
        modelSectionStack.addArrangedSubview(formRow("Public model", modelNameField))
        modelSectionStack.addArrangedSubview(formRow("Provider API key", modelApiKeyPopupButton))
        modelSectionStack.addArrangedSubview(formRow("LiteLLM adapter", adapterControlRow()))
        modelSectionStack.addArrangedSubview(formRow("Upstream model", upstreamModelField))
        modelSectionStack.addArrangedSubview(formRow("Route order", orderField))
        modelSectionStack.addArrangedSubview(upstreamApiModeRow())
        modelSection.view.translatesAutoresizingMaskIntoConstraints = false
        detailPane.addSubview(modelSection.view)
        modelDetailView = modelSection.view

        NSLayoutConstraint.activate([
            providerSection.view.leadingAnchor.constraint(equalTo: detailPane.leadingAnchor),
            providerSection.view.trailingAnchor.constraint(equalTo: detailPane.trailingAnchor),
            providerSection.view.topAnchor.constraint(equalTo: detailPane.topAnchor),
            providerSection.view.bottomAnchor.constraint(lessThanOrEqualTo: detailPane.bottomAnchor),

            modelSection.view.leadingAnchor.constraint(equalTo: detailPane.leadingAnchor),
            modelSection.view.trailingAnchor.constraint(equalTo: detailPane.trailingAnchor),
            modelSection.view.topAnchor.constraint(equalTo: detailPane.topAnchor),
            modelSection.view.bottomAnchor.constraint(lessThanOrEqualTo: detailPane.bottomAnchor),
        ])

        let runtimeSection = sectionStack(title: "Runtime Map")
        configureRuntimeMapTable()
        let runtimeScroll = NSScrollView()
        runtimeScroll.borderType = .bezelBorder
        runtimeScroll.hasVerticalScroller = true
        runtimeScroll.hasHorizontalScroller = false
        runtimeScroll.autohidesScrollers = false
        runtimeScroll.usesPredominantAxisScrolling = true
        runtimeScroll.verticalScrollElasticity = .none
        runtimeScroll.documentView = runtimeMapTableView
        runtimeScroll.widthAnchor.constraint(equalToConstant: 560).isActive = true
        runtimeScroll.heightAnchor.constraint(equalToConstant: 220).isActive = true
        runtimeSection.stack.addArrangedSubview(runtimeScroll)
        formStack.addArrangedSubview(runtimeSection.view)
        runtimeMapScrollView = runtimeScroll

        let cancelButton = NSButton(title: "Close", target: self, action: #selector(cancel))
        cancelButton.keyEquivalent = "\u{1b}"
        let buttonRow = NSStackView()
        buttonRow.orientation = .horizontal
        buttonRow.alignment = .centerY
        buttonRow.spacing = 8
        buttonRow.setContentHuggingPriority(.required, for: .horizontal)
        buttonRow.setContentCompressionResistancePriority(.required, for: .horizontal)
        buttonRow.addArrangedSubview(cancelButton)
        buttonRow.addArrangedSubview(applyButton)

        let bottomStack = NSStackView()
        bottomStack.orientation = .horizontal
        bottomStack.alignment = .centerY
        bottomStack.spacing = 8
        bottomStack.translatesAutoresizingMaskIntoConstraints = false
        bottomStack.addArrangedSubview(applyStatusLabel)
        bottomStack.addArrangedSubview(spacer())
        bottomStack.addArrangedSubview(buttonRow)
        contentGuide.addSubview(bottomStack)

        NSLayoutConstraint.activate([
            modeStack.leadingAnchor.constraint(equalTo: contentGuide.leadingAnchor),
            modeStack.trailingAnchor.constraint(equalTo: contentGuide.trailingAnchor),
            modeStack.topAnchor.constraint(equalTo: contentGuide.topAnchor),
            modeStack.heightAnchor.constraint(equalToConstant: 28),

            mainStack.leadingAnchor.constraint(equalTo: contentGuide.leadingAnchor),
            mainStack.trailingAnchor.constraint(lessThanOrEqualTo: contentGuide.trailingAnchor),
            mainStack.topAnchor.constraint(equalTo: modeStack.bottomAnchor, constant: 14),
            mainStack.bottomAnchor.constraint(equalTo: bottomStack.topAnchor, constant: -8),

            bottomStack.leadingAnchor.constraint(equalTo: contentGuide.leadingAnchor),
            bottomStack.trailingAnchor.constraint(equalTo: contentGuide.trailingAnchor),
            bottomStack.bottomAnchor.constraint(equalTo: contentGuide.bottomAnchor),
            bottomStack.heightAnchor.constraint(greaterThanOrEqualToConstant: 30),
        ])
    }

    func configureProviderTable() {
        configureListTable(providerTableView)
        providerTableView.delegate = self
        providerTableView.dataSource = self
        providerTableView.target = self
        providerTableView.action = #selector(providerTableClicked(_:))

        let nameColumn = NSTableColumn(identifier: providerNameColumnIdentifier)
        nameColumn.title = "Provider"
        nameColumn.width = 145
        providerTableView.addTableColumn(nameColumn)

        let countColumn = NSTableColumn(identifier: providerCountColumnIdentifier)
        countColumn.title = "#"
        countColumn.width = 50
        providerTableView.addTableColumn(countColumn)
    }

    func configureModelTable() {
        configureListTable(modelTableView)
        modelTableView.delegate = self
        modelTableView.dataSource = self
        modelTableView.target = self
        modelTableView.action = #selector(modelTableClicked(_:))

        let nameColumn = NSTableColumn(identifier: modelNameColumnIdentifier)
        nameColumn.title = "Model"
        nameColumn.width = 125
        modelTableView.addTableColumn(nameColumn)

        let upstreamColumn = NSTableColumn(identifier: modelUpstreamColumnIdentifier)
        upstreamColumn.title = "Upstream"
        upstreamColumn.width = 115
        modelTableView.addTableColumn(upstreamColumn)

        let routeColumn = NSTableColumn(identifier: modelRouteColumnIdentifier)
        routeColumn.title = "Key / Order"
        routeColumn.width = 95
        modelTableView.addTableColumn(routeColumn)
    }

    func configureRouteTable() {
        configureListTable(routeTableView)
        routeTableView.delegate = self
        routeTableView.dataSource = self
        routeTableView.target = self
        routeTableView.action = #selector(routeTableClicked(_:))
        routeTableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle

        let modelColumn = NSTableColumn(identifier: routeModelColumnIdentifier)
        modelColumn.title = "Model"
        modelColumn.width = 145
        routeTableView.addTableColumn(modelColumn)

        let orderColumn = NSTableColumn(identifier: routeOrderColumnIdentifier)
        orderColumn.title = "Order"
        orderColumn.width = 55
        routeTableView.addTableColumn(orderColumn)

        let providerKeyColumn = NSTableColumn(identifier: routeProviderKeyColumnIdentifier)
        providerKeyColumn.title = "Provider / Key"
        providerKeyColumn.width = 165
        routeTableView.addTableColumn(providerKeyColumn)

        let upstreamColumn = NSTableColumn(identifier: routeUpstreamColumnIdentifier)
        upstreamColumn.title = "Upstream"
        upstreamColumn.width = 155
        routeTableView.addTableColumn(upstreamColumn)

        let statusColumn = NSTableColumn(identifier: routeStatusColumnIdentifier)
        statusColumn.title = "Status"
        statusColumn.width = 55
        routeTableView.addTableColumn(statusColumn)
    }

    func configureProviderKeyTable() {
        configureListTable(providerKeyTableView)
        providerKeyTableView.delegate = self
        providerKeyTableView.dataSource = self
        providerKeyTableView.target = self
        providerKeyTableView.action = #selector(providerKeyTableClicked(_:))
        providerKeyTableView.headerView = nil

        let nameColumn = NSTableColumn(identifier: providerKeyNameColumnIdentifier)
        nameColumn.title = "Key"
        nameColumn.width = 180
        providerKeyTableView.addTableColumn(nameColumn)
    }

    func configureRuntimeMapTable() {
        runtimeMapTableView.delegate = self
        runtimeMapTableView.dataSource = self
        runtimeMapTableView.headerView = nil
        runtimeMapTableView.usesAlternatingRowBackgroundColors = false
        runtimeMapTableView.intercellSpacing = .zero
        runtimeMapTableView.selectionHighlightStyle = .none
        runtimeMapTableView.focusRingType = .none
        runtimeMapTableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        runtimeMapTableView.allowsColumnReordering = false
        runtimeMapTableView.allowsColumnResizing = false
        runtimeMapTableView.floatsGroupRows = false

        let column = NSTableColumn(identifier: runtimeMapColumnIdentifier)
        column.title = "Runtime route"
        column.resizingMask = .autoresizingMask
        column.width = 540
        runtimeMapTableView.addTableColumn(column)
    }

    func configureListTable(_ tableView: NSTableView) {
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.allowsMultipleSelection = false
        tableView.rowSizeStyle = .medium
        tableView.intercellSpacing = NSSize(width: 0, height: 0)
        tableView.selectionHighlightStyle = .regular
        tableView.focusRingType = .none
    }

    func scrollView(for tableView: NSTableView, height: CGFloat) -> NSScrollView {
        let scrollView = NSScrollView()
        scrollView.borderType = .bezelBorder
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.autohidesScrollers = true
        scrollView.documentView = tableView
        scrollView.heightAnchor.constraint(equalToConstant: height).isActive = true
        return scrollView
    }

    func spacer() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    func fixedSpacer() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return view
    }

    func cascadeColumn(title: String, width: CGFloat) -> (view: NSStackView, stack: NSStackView) {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 8
        stack.widthAnchor.constraint(equalToConstant: width).isActive = true

        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        stack.addArrangedSubview(titleLabel)
        return (stack, stack)
    }

    func sectionStack(title: String, width: CGFloat = 560) -> (view: NSStackView, stack: NSStackView) {
        let container = NSStackView()
        container.orientation = .vertical
        container.alignment = .leading
        container.spacing = 8
        container.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 8, right: 0)
        container.widthAnchor.constraint(equalToConstant: width).isActive = true

        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        titleLabel.textColor = .secondaryLabelColor
        container.addArrangedSubview(titleLabel)

        let separator = NSBox()
        separator.boxType = .separator
        separator.widthAnchor.constraint(equalToConstant: width).isActive = true
        container.addArrangedSubview(separator)

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 4, left: 14, bottom: 0, right: 0)
        container.addArrangedSubview(stack)
        return (container, stack)
    }

    func makeTextField(width: CGFloat = 430) -> NSTextField {
        let field = NSTextField()
        field.delegate = self
        field.target = self
        field.action = #selector(textFieldAction(_:))
        field.usesSingleLineMode = true
        field.lineBreakMode = .byTruncatingMiddle
        field.widthAnchor.constraint(equalToConstant: width).isActive = true
        return field
    }

    func makeTokenField(width: CGFloat = 430) -> NSTextField {
        let field = makeTextField(width: width)
        field.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        field.toolTip = "Visible provider api_key token"
        field.lineBreakMode = .byTruncatingTail
        return field
    }

    func formRow(_ label: String, _ control: NSView) -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 8

        let labelView = NSTextField(labelWithString: label)
        labelView.alignment = .right
        labelView.widthAnchor.constraint(equalToConstant: 110).isActive = true
        row.addArrangedSubview(labelView)
        row.addArrangedSubview(control)
        return row
    }

    func providerKeysEditor() -> NSStackView {
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 10
        content.edgeInsets = NSEdgeInsets(top: 6, left: 0, bottom: 0, right: 0)

        let title = NSTextField(labelWithString: "API keys")
        title.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        title.textColor = .secondaryLabelColor
        content.addArrangedSubview(title)

        let keyRow = NSStackView()
        keyRow.orientation = .horizontal
        keyRow.alignment = .top
        keyRow.spacing = 12
        content.addArrangedSubview(keyRow)

        let listScroll = scrollView(for: providerKeyTableView, height: 112)
        listScroll.widthAnchor.constraint(equalToConstant: 160).isActive = true
        keyRow.addArrangedSubview(listScroll)

        let keyFields = NSStackView()
        keyFields.orientation = .vertical
        keyFields.alignment = .leading
        keyFields.spacing = 8
        keyFields.addArrangedSubview(providerKeyEnabledCheckbox)
        keyFields.addArrangedSubview(compactFormRow("Label", providerKeyNameField))
        keyFields.addArrangedSubview(compactFormRow("Token", providerApiKeyField))
        keyRow.addArrangedSubview(keyFields)

        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.spacing = 8
        for button in [addProviderKeyButton, deleteProviderKeyButton] {
            button.bezelStyle = .rounded
            buttons.addArrangedSubview(button)
        }
        buttons.addArrangedSubview(spacer())
        content.addArrangedSubview(buttons)

        return content
    }

    func compactFormRow(_ label: String, _ control: NSView) -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 8

        let labelView = NSTextField(labelWithString: label)
        labelView.alignment = .right
        labelView.widthAnchor.constraint(equalToConstant: 48).isActive = true
        row.addArrangedSubview(labelView)
        row.addArrangedSubview(control)
        return row
    }

    func adapterControlRow() -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 8
        row.addArrangedSubview(adapterPopupButton)
        row.addArrangedSubview(customAdapterField)
        return row
    }

    func modelEnabledRow() -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        row.addArrangedSubview(enabledCheckbox)
        row.addArrangedSubview(probeModelAvailabilityButton)
        row.addArrangedSubview(spacer())
        return row
    }

    func upstreamApiModeRow() -> NSStackView {
        configureUpstreamApiModeRowsIfNeeded()
        return upstreamApiModeStackView
    }

    func setEditorStatus(_ message: String, color: NSColor = .secondaryLabelColor, tooltip: String? = nil) {
        let inline = elidedDisplayText(message, limit: inlineStatusLimit)
        applyStatusLabel.stringValue = inline
        applyStatusLabel.textColor = color
        let detail = tooltip?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        applyStatusLabel.toolTip = detail.isEmpty || detail == message
            ? message
            : "\(message)\n\n\(detail)"
    }

    func setPendingChanges(_ pending: Bool, updateStatus: Bool = true) {
        hasPendingChanges = pending
        applyButton.isEnabled = pending
    }

    func markPendingChanges(updateStatus: Bool = true) {
        guard !isRenderingSelection else { return }
        setPendingChanges(true, updateStatus: updateStatus)
    }

    func markPendingChangesIfNeeded(_ changed: Bool, updateStatus: Bool = true) {
        guard changed else { return }
        markPendingChanges(updateStatus: updateStatus)
    }

    func setEditorError(_ title: String, message: String) {
        let detail = message.trimmingCharacters(in: .whitespacesAndNewlines)
        let inline = detail.isEmpty ? title : "\(title): \(detail)"
        setEditorStatus(
            elidedDisplayText(inline, limit: inlineStatusLimit),
            color: .systemRed,
            tooltip: detail.isEmpty ? title : "\(title)\n\(detail)"
        )
        showAlert(title: title, message: detail)
    }
}
