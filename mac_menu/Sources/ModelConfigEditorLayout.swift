import Cocoa

private final class ModelEditorDocumentView: NSView {
    override var isFlipped: Bool { true }
}

final class TrailingSeparatorlessTableHeaderCell: NSTableHeaderCell {
    override func draw(withFrame cellFrame: NSRect, in controlView: NSView) {
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: cellFrame).addClip()
        var extendedFrame = cellFrame
        extendedFrame.size.width += 2
        super.draw(withFrame: extendedFrame, in: controlView)
        NSGraphicsContext.restoreGraphicsState()
    }
}

extension ModelConfigEditorController {
    func buildWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1052, height: 600),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "LiteLLM Providers & Models"
        // 1052 is the compact three-pane width: 668 workspace + 12 gap
        // + 340 inspector + 32 outer inset.
        window.minSize = NSSize(width: 1052, height: 560)
        window.animationBehavior = .none
        window.level = .normal
        window.collectionBehavior = [.fullScreenPrimary]
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
        modeStack.addArrangedSubview(importSourcePopupButton)
        modeStack.addArrangedSubview(fixedSpacer())

        let editorWorkspaceStack = NSStackView()
        editorWorkspaceStack.orientation = .horizontal
        editorWorkspaceStack.alignment = .height
        editorWorkspaceStack.distribution = .fill
        editorWorkspaceStack.spacing = 12
        editorWorkspaceStack.translatesAutoresizingMaskIntoConstraints = false
        contentGuide.addSubview(editorWorkspaceStack)

        let modeWorkspaceColumn = NSStackView()
        modeWorkspaceColumn.orientation = .vertical
        modeWorkspaceColumn.alignment = .width
        modeWorkspaceColumn.distribution = .fill
        modeWorkspaceColumn.spacing = 14
        modeWorkspaceColumn.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        modeWorkspaceColumn.setContentCompressionResistancePriority(.required, for: .horizontal)
        editorWorkspaceStack.addArrangedSubview(modeWorkspaceColumn)
        modeWorkspaceColumn.addArrangedSubview(modeStack)

        let modeWorkspaceHost = NSView()
        let modeWorkspaceMinimumWidth = modeWorkspaceHost.widthAnchor.constraint(greaterThanOrEqualToConstant: 668)
        modeWorkspaceMinimumWidth.isActive = true
        let modeWorkspacePreferredWidth = modeWorkspaceHost.widthAnchor.constraint(equalToConstant: 680)
        modeWorkspacePreferredWidth.priority = .defaultHigh
        modeWorkspacePreferredWidth.isActive = true
        modeWorkspaceHost.setContentHuggingPriority(.defaultHigh, for: .horizontal)
        modeWorkspaceHost.setContentCompressionResistancePriority(.required, for: .horizontal)
        modeWorkspaceHost.setContentHuggingPriority(.defaultLow, for: .vertical)
        modeWorkspaceHost.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        modeWorkspaceColumn.addArrangedSubview(modeWorkspaceHost)

        let providersWorkspace = NSView()
        providersWorkspace.translatesAutoresizingMaskIntoConstraints = false
        modeWorkspaceHost.addSubview(providersWorkspace)

        let routesWorkspace = NSView()
        routesWorkspace.translatesAutoresizingMaskIntoConstraints = false
        routesWorkspace.isHidden = true
        modeWorkspaceHost.addSubview(routesWorkspace)

        let providersContentStack = NSStackView()
        providersContentStack.orientation = .horizontal
        providersContentStack.alignment = .height
        providersContentStack.distribution = .fill
        providersContentStack.spacing = 12
        providersContentStack.translatesAutoresizingMaskIntoConstraints = false
        providersWorkspace.addSubview(providersContentStack)

        let providerPane = NSView()
        let providerPaneWidthConstraint = providerPane.widthAnchor.constraint(equalToConstant: 196)
        providerPaneWidthConstraint.isActive = true
        providersContentStack.addArrangedSubview(providerPane)

        let providerButtons = NSStackView()
        providerButtons.orientation = .horizontal
        providerButtons.spacing = 8
        let addProviderButton = textButton(
            title: "+",
            toolTip: "Add provider",
            accessibilityLabel: "Add provider"
        )
        addProviderButton.target = self
        addProviderButton.action = #selector(addProvider)
        providerButtons.addArrangedSubview(addProviderButton)
        providerButtons.addArrangedSubview(deleteProviderButton)
        let providerStack = cascadeColumn(title: "Providers", actions: providerButtons)
        configureProviderTable()
        let providerScrollView = scrollView(for: providerTableView)
        providerScrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 260).isActive = true
        providerStack.stack.addArrangedSubview(providerScrollView)
        providerStack.view.translatesAutoresizingMaskIntoConstraints = false
        providerPane.addSubview(providerStack.view)
        NSLayoutConstraint.activate([
            providerStack.view.leadingAnchor.constraint(equalTo: providerPane.leadingAnchor),
            providerStack.view.trailingAnchor.constraint(equalTo: providerPane.trailingAnchor),
            providerStack.view.topAnchor.constraint(equalTo: providerPane.topAnchor),
            providerStack.view.bottomAnchor.constraint(equalTo: providerPane.bottomAnchor),
        ])

        let modelsRoutesPane = NSView()
        let modelsRoutesPaneMinimumWidth = modelsRoutesPane.widthAnchor.constraint(greaterThanOrEqualToConstant: 460)
        modelsRoutesPaneMinimumWidth.isActive = true
        let preferredModelWidth = modelsRoutesPane.widthAnchor.constraint(equalToConstant: 472)
        preferredModelWidth.priority = .defaultHigh
        preferredModelWidth.isActive = true
        modelsRoutesPane.setContentHuggingPriority(.defaultLow, for: .horizontal)
        providersContentStack.addArrangedSubview(modelsRoutesPane)
        let modelButtons = NSStackView()
        modelButtons.orientation = .horizontal
        modelButtons.spacing = 8
        for button in [addModelButton, duplicateModelButton, deleteModelButton] {
            modelButtons.addArrangedSubview(button)
        }
        let modelStack = cascadeColumn(title: "Models", actions: modelButtons)
        configureModelTable()
        let modelScrollView = scrollView(for: modelTableView)
        modelScrollView.hasHorizontalScroller = false
        modelScrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 300).isActive = true
        modelStack.stack.addArrangedSubview(modelScrollView)
        let modelFetchRow = NSStackView()
        modelFetchRow.orientation = .horizontal
        modelFetchRow.spacing = 8
        modelFetchRow.addArrangedSubview(modelCandidateApiKeyPopupButton)
        modelFetchRow.addArrangedSubview(fetchModelsButton)
        modelFetchRow.addArrangedSubview(spacer())
        modelStack.stack.addArrangedSubview(modelFetchRow)
        modelStack.view.translatesAutoresizingMaskIntoConstraints = false
        modelsRoutesPane.addSubview(modelStack.view)
        NSLayoutConstraint.activate([
            modelStack.view.leadingAnchor.constraint(equalTo: modelsRoutesPane.leadingAnchor),
            modelStack.view.trailingAnchor.constraint(equalTo: modelsRoutesPane.trailingAnchor),
            modelStack.view.topAnchor.constraint(equalTo: modelsRoutesPane.topAnchor),
            modelStack.view.bottomAnchor.constraint(equalTo: modelsRoutesPane.bottomAnchor),
        ])
        modelStack.view.setContentHuggingPriority(.defaultLow, for: .vertical)
        modelScrollView.setContentHuggingPriority(.defaultLow, for: .vertical)
        self.modelTableScrollView = modelScrollView

        let routeHeader = NSStackView()
        routeHeader.orientation = .horizontal
        routeHeader.alignment = .centerY
        routeHeader.spacing = 8
        let routeTitle = NSTextField(labelWithString: "Routes")
        routeTitle.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        routeHeader.addArrangedSubview(routeTitle)
        routeHeader.addArrangedSubview(spacer())
        routeHeader.addArrangedSubview(routeMoveUpButton)
        routeHeader.addArrangedSubview(routeMoveDownButton)

        let routeStack = NSStackView()
        routeStack.orientation = .vertical
        routeStack.alignment = .width
        routeStack.spacing = 8
        routeStack.translatesAutoresizingMaskIntoConstraints = false
        routeHeader.heightAnchor.constraint(equalToConstant: 28).isActive = true
        routeStack.addArrangedSubview(routeHeader)
        configureRouteTable()
        let routeScrollView = scrollView(for: routeTableView)
        routeScrollView.hasHorizontalScroller = false
        routeScrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 300).isActive = true
        routeStack.addArrangedSubview(routeScrollView)
        routeStack.widthAnchor.constraint(greaterThanOrEqualToConstant: 560).isActive = true
        routeStack.setContentHuggingPriority(.defaultLow, for: .horizontal)
        routeStack.setContentCompressionResistancePriority(.required, for: .horizontal)
        routesWorkspace.addSubview(routeStack)
        self.routeTableScrollView = routeScrollView

        let detailScrollView = NSScrollView()
        detailScrollView.borderType = .noBorder
        detailScrollView.drawsBackground = false
        detailScrollView.hasVerticalScroller = true
        detailScrollView.hasHorizontalScroller = false
        detailScrollView.autohidesScrollers = true
        detailScrollView.setContentHuggingPriority(.defaultLow, for: .vertical)
        detailScrollView.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        let formStackMinimumWidth = detailScrollView.widthAnchor.constraint(greaterThanOrEqualToConstant: 340)
        formStackMinimumWidth.isActive = true
        detailScrollView.setContentHuggingPriority(.defaultLow, for: .horizontal)
        detailScrollView.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let detailDocumentView = ModelEditorDocumentView()
        detailDocumentView.translatesAutoresizingMaskIntoConstraints = false
        detailScrollView.documentView = detailDocumentView

        let detailContentStack = NSStackView()
        detailContentStack.orientation = .vertical
        detailContentStack.alignment = .width
        detailContentStack.spacing = 0
        detailContentStack.translatesAutoresizingMaskIntoConstraints = false
        detailDocumentView.addSubview(detailContentStack)

        let providerSection = sectionStack(
            title: "Provider",
            header: providerEditorHeaderView()
        )
        let providerSectionStack = providerSection.stack
        providerSectionStack.addArrangedSubview(providerEnabledRow())
        providerSectionStack.addArrangedSubview(formRow("Base URL", providerApiBaseField))
        providerSectionStack.addArrangedSubview(formRow("Provider name", providerNameField))
        configureProviderKeyTable()
        providerSectionStack.addArrangedSubview(providerKeysEditor())
        providerSection.view.isHidden = true
        detailContentStack.addArrangedSubview(providerSection.view)
        providerDetailView = providerSection.view

        let modelSection = sectionStack(
            title: "Selected Model Deployment",
            header: modelBreadcrumbView()
        )
        let modelSectionStack = modelSection.stack
        modelSectionStack.spacing = 6
        modelSectionStack.addArrangedSubview(modelEnabledRow())
        modelSectionStack.addArrangedSubview(modelBillingSummaryPanel())
        modelSectionStack.addArrangedSubview(compactModelFormRow("Public model", modelNameField, preferredWidth: 212, minWidth: 150))
        modelSectionStack.addArrangedSubview(compactModelFormRow("Provider", modelProviderPopupButton, preferredWidth: 150, minWidth: 112))
        modelSectionStack.addArrangedSubview(compactModelFormRow("API key", modelApiKeyPopupButton, preferredWidth: 150, minWidth: 112))
        modelSectionStack.addArrangedSubview(compactModelFormRow("Upstream", upstreamModelField, preferredWidth: 212, minWidth: 150))
        modelSectionStack.addArrangedSubview(compactModelFormRow("Order", orderField, preferredWidth: 64, minWidth: 48))
        modelSectionStack.addArrangedSubview(upstreamApiModeRow())
        modelSection.view.isHidden = true
        detailContentStack.addArrangedSubview(modelSection.view)
        modelDetailView = modelSection.view

        NSLayoutConstraint.activate([
            detailDocumentView.widthAnchor.constraint(equalTo: detailScrollView.contentView.widthAnchor),
            detailContentStack.leadingAnchor.constraint(equalTo: detailDocumentView.leadingAnchor),
            detailContentStack.trailingAnchor.constraint(equalTo: detailDocumentView.trailingAnchor),
            detailContentStack.topAnchor.constraint(equalTo: detailDocumentView.topAnchor),
            detailContentStack.bottomAnchor.constraint(equalTo: detailDocumentView.bottomAnchor),
        ])
        self.editorWorkspaceStack = editorWorkspaceStack
        self.modeWorkspaceColumn = modeWorkspaceColumn
        self.modeWorkspaceHost = modeWorkspaceHost
        self.providerPane = providerPane
        self.providerPaneWidthConstraint = providerPaneWidthConstraint
        self.providersWorkspace = providersWorkspace
        self.routesWorkspace = routesWorkspace
        self.providersContentStack = providersContentStack
        self.modelsRoutesPane = modelsRoutesPane
        self.modelsView = modelStack.view
        self.detailScrollView = detailScrollView
        self.detailDocumentView = detailDocumentView
        self.detailPaneMinimumWidthConstraint = formStackMinimumWidth
        editorWorkspaceStack.addArrangedSubview(detailScrollView)

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
        self.editorFooterView = bottomStack

        NSLayoutConstraint.activate([
            modeStack.heightAnchor.constraint(equalToConstant: 28),

            editorWorkspaceStack.leadingAnchor.constraint(equalTo: contentGuide.leadingAnchor),
            editorWorkspaceStack.trailingAnchor.constraint(equalTo: contentGuide.trailingAnchor),
            editorWorkspaceStack.topAnchor.constraint(equalTo: contentGuide.topAnchor),
            editorWorkspaceStack.bottomAnchor.constraint(equalTo: bottomStack.topAnchor, constant: -8),

            providersWorkspace.leadingAnchor.constraint(equalTo: modeWorkspaceHost.leadingAnchor),
            providersWorkspace.trailingAnchor.constraint(equalTo: modeWorkspaceHost.trailingAnchor),
            providersWorkspace.topAnchor.constraint(equalTo: modeWorkspaceHost.topAnchor),
            providersWorkspace.bottomAnchor.constraint(equalTo: modeWorkspaceHost.bottomAnchor),

            routesWorkspace.leadingAnchor.constraint(equalTo: modeWorkspaceHost.leadingAnchor),
            routesWorkspace.trailingAnchor.constraint(equalTo: modeWorkspaceHost.trailingAnchor),
            routesWorkspace.topAnchor.constraint(equalTo: modeWorkspaceHost.topAnchor),
            routesWorkspace.bottomAnchor.constraint(equalTo: modeWorkspaceHost.bottomAnchor),

            providersContentStack.leadingAnchor.constraint(equalTo: providersWorkspace.leadingAnchor),
            providersContentStack.trailingAnchor.constraint(equalTo: providersWorkspace.trailingAnchor),
            providersContentStack.topAnchor.constraint(equalTo: providersWorkspace.topAnchor),
            providersContentStack.bottomAnchor.constraint(equalTo: providersWorkspace.bottomAnchor),

            routeStack.leadingAnchor.constraint(equalTo: routesWorkspace.leadingAnchor),
            routeStack.trailingAnchor.constraint(equalTo: routesWorkspace.trailingAnchor),
            routeStack.topAnchor.constraint(equalTo: routesWorkspace.topAnchor),
            routeStack.bottomAnchor.constraint(equalTo: routesWorkspace.bottomAnchor),

            bottomStack.leadingAnchor.constraint(equalTo: contentGuide.leadingAnchor),
            bottomStack.trailingAnchor.constraint(equalTo: contentGuide.trailingAnchor),
            bottomStack.bottomAnchor.constraint(equalTo: contentGuide.bottomAnchor),
            bottomStack.heightAnchor.constraint(equalToConstant: 32),
        ])
    }

    func configureProviderTable() {
        configureListTable(providerTableView)
        providerTableView.delegate = self
        providerTableView.dataSource = self
        providerTableView.target = self
        providerTableView.action = #selector(providerTableClicked(_:))
        providerTableView.columnAutoresizingStyle = .firstColumnOnlyAutoresizingStyle

        let nameColumn = NSTableColumn(identifier: providerNameColumnIdentifier)
        nameColumn.title = "Provider"
        nameColumn.width = 150
        nameColumn.minWidth = 110
        providerTableView.addTableColumn(nameColumn)

        let countColumn = NSTableColumn(identifier: providerCountColumnIdentifier)
        countColumn.title = "Models"
        countColumn.width = 48
        countColumn.minWidth = 44
        countColumn.maxWidth = 56
        providerTableView.addTableColumn(countColumn)
        suppressTrailingHeaderSeparator(in: providerTableView)
    }

    func configureModelTable() {
        configureListTable(modelTableView)
        modelTableView.delegate = self
        modelTableView.dataSource = self
        modelTableView.target = self
        modelTableView.action = #selector(modelTableClicked(_:))
        // Keep deployment columns meaningful while allowing the trailing key/order
        // column to absorb the available width without creating horizontal scroll.
        modelTableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle

        let nameColumn = NSTableColumn(identifier: modelNameColumnIdentifier)
        nameColumn.title = "Model"
        nameColumn.width = 118
        nameColumn.minWidth = 85
        modelTableView.addTableColumn(nameColumn)

        let upstreamColumn = NSTableColumn(identifier: modelUpstreamColumnIdentifier)
        upstreamColumn.title = "Upstream"
        upstreamColumn.width = 132
        upstreamColumn.minWidth = 108
        upstreamColumn.maxWidth = 160
        modelTableView.addTableColumn(upstreamColumn)

        let billingColumn = NSTableColumn(identifier: modelBillingColumnIdentifier)
        billingColumn.title = "Balance"
        billingColumn.width = 112
        billingColumn.minWidth = 112
        billingColumn.maxWidth = 112
        modelTableView.addTableColumn(billingColumn)

        let apiKeyOrderColumn = NSTableColumn(identifier: modelApiKeyOrderColumnIdentifier)
        apiKeyOrderColumn.title = "API key / Order"
        apiKeyOrderColumn.width = 104
        apiKeyOrderColumn.minWidth = 96
        apiKeyOrderColumn.maxWidth = 124
        apiKeyOrderColumn.resizingMask = .autoresizingMask
        modelTableView.addTableColumn(apiKeyOrderColumn)
        suppressTrailingHeaderSeparator(in: modelTableView)
    }

    func configureRouteTable() {
        configureListTable(routeTableView)
        routeTableView.delegate = self
        routeTableView.dataSource = self
        routeTableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        routeTableView.usesAlternatingRowBackgroundColors = true

        let modelColumn = NSTableColumn(identifier: routeModelColumnIdentifier)
        modelColumn.title = "Model"
        modelColumn.width = 170
        modelColumn.minWidth = 140
        modelColumn.maxWidth = 300
        routeTableView.addTableColumn(modelColumn)

        let orderColumn = NSTableColumn(identifier: routeOrderColumnIdentifier)
        orderColumn.title = "Order"
        orderColumn.width = 56
        orderColumn.minWidth = 52
        orderColumn.maxWidth = 88
        routeTableView.addTableColumn(orderColumn)

        let providerKeyColumn = NSTableColumn(identifier: routeProviderKeyColumnIdentifier)
        providerKeyColumn.title = "Provider / Key"
        providerKeyColumn.width = 130
        providerKeyColumn.minWidth = 112
        providerKeyColumn.maxWidth = 240
        routeTableView.addTableColumn(providerKeyColumn)

        let upstreamColumn = NSTableColumn(identifier: routeUpstreamColumnIdentifier)
        upstreamColumn.title = "Upstream"
        upstreamColumn.width = 164
        upstreamColumn.minWidth = 140
        upstreamColumn.resizingMask = .autoresizingMask
        routeTableView.addTableColumn(upstreamColumn)
        suppressTrailingHeaderSeparator(in: routeTableView)
    }

    func configureProviderKeyTable() {
        configureListTable(providerKeyTableView)
        providerKeyTableView.delegate = self
        providerKeyTableView.dataSource = self
        providerKeyTableView.target = self
        providerKeyTableView.action = #selector(providerKeyTableClicked(_:))
        providerKeyTableView.headerView = nil
        providerKeyTableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle

        let nameColumn = NSTableColumn(identifier: providerKeyNameColumnIdentifier)
        nameColumn.title = "Key"
        nameColumn.width = 118
        nameColumn.minWidth = 72
        nameColumn.resizingMask = .autoresizingMask
        providerKeyTableView.addTableColumn(nameColumn)
    }

    func configureListTable(_ tableView: NSTableView) {
        tableView.usesAlternatingRowBackgroundColors = false
        tableView.allowsMultipleSelection = false
        tableView.allowsColumnReordering = false
        tableView.rowSizeStyle = .medium
        tableView.intercellSpacing = NSSize(width: 0, height: 0)
        tableView.selectionHighlightStyle = .regular
        tableView.focusRingType = .none
    }

    func suppressTrailingHeaderSeparator(in tableView: NSTableView) {
        guard let column = tableView.tableColumns.last else { return }
        let cell = TrailingSeparatorlessTableHeaderCell(textCell: column.title)
        cell.alignment = column.headerCell.alignment
        column.headerCell = cell
    }

    func scrollView(for tableView: NSTableView, height: CGFloat) -> NSScrollView {
        let scrollView = scrollView(for: tableView)
        scrollView.heightAnchor.constraint(equalToConstant: height).isActive = true
        return scrollView
    }

    func scrollView(for tableView: NSTableView) -> NSScrollView {
        let scrollView = NSScrollView()
        scrollView.borderType = .bezelBorder
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.documentView = tableView
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

    func cascadeColumn(
        title: String,
        actions: NSView? = nil,
        preferredWidth: CGFloat? = nil,
        minWidth: CGFloat? = nil
    ) -> (view: NSStackView, stack: NSStackView) {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 8
        if let minWidth {
            stack.widthAnchor.constraint(greaterThanOrEqualToConstant: minWidth).isActive = true
        }
        if let preferredWidth {
            let constraint = stack.widthAnchor.constraint(equalToConstant: preferredWidth)
            constraint.priority = .defaultHigh
            constraint.isActive = true
        }

        let header = NSStackView()
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 8
        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        header.addArrangedSubview(titleLabel)
        header.addArrangedSubview(spacer())
        if let actions {
            header.addArrangedSubview(actions)
        }
        header.heightAnchor.constraint(equalToConstant: 28).isActive = true
        stack.addArrangedSubview(header)
        return (stack, stack)
    }

    func sectionStack(
        title: String,
        titleWeight: NSFont.Weight = .semibold,
        header: NSView? = nil
    ) -> (view: NSView, stack: NSStackView) {
        let container = NSView()

        let titleView: NSView
        if let header {
            titleView = header
        } else {
            let titleLabel = NSTextField(labelWithString: title)
            titleLabel.alignment = .left
            titleLabel.font = NSFont.systemFont(ofSize: 13, weight: titleWeight)
            titleLabel.textColor = .secondaryLabelColor
            titleView = titleLabel
        }

        let separator = NSBox()
        separator.boxType = .separator

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .width
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 4, left: 14, bottom: 0, right: 6)
        for view in [titleView, separator, stack] {
            view.translatesAutoresizingMaskIntoConstraints = false
            container.addSubview(view)
        }
        NSLayoutConstraint.activate([
            titleView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            titleView.trailingAnchor.constraint(lessThanOrEqualTo: container.trailingAnchor),
            titleView.topAnchor.constraint(equalTo: container.topAnchor, constant: 4),

            separator.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            separator.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            separator.topAnchor.constraint(equalTo: titleView.bottomAnchor, constant: 8),

            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            stack.topAnchor.constraint(equalTo: separator.bottomAnchor, constant: 4),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -8),
        ])
        return (container, stack)
    }

    func makeTextField(
        preferredWidth: CGFloat = 430,
        minWidth: CGFloat = 180
    ) -> NSTextField {
        let field = makeFlexibleTextField()
        field.widthAnchor.constraint(greaterThanOrEqualToConstant: minWidth).isActive = true
        field.setContentHuggingPriority(.defaultLow, for: .horizontal)
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return field
    }

    func textButton(
        title: String,
        toolTip: String? = nil,
        accessibilityLabel: String? = nil
    ) -> NSButton {
        let button = NSButton(title: title, target: nil, action: nil)
        button.bezelStyle = .rounded
        button.toolTip = toolTip
        button.setAccessibilityLabel(accessibilityLabel ?? toolTip ?? title)
        return button
    }

    func makeFlexibleTextField() -> NSTextField {
        let field = NSTextField()
        field.delegate = self
        field.target = self
        field.action = #selector(textFieldAction(_:))
        field.usesSingleLineMode = true
        field.lineBreakMode = .byTruncatingMiddle
        return field
    }

    func makeAPIKeyField(
        preferredWidth: CGFloat = 430,
        minWidth: CGFloat = 180
    ) -> NSTextField {
        let field = makeTextField(preferredWidth: preferredWidth, minWidth: minWidth)
        field.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        field.toolTip = "Provider API key value"
        field.lineBreakMode = .byTruncatingTail
        return field
    }

    func formRow(_ label: String, _ control: NSView) -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 8

        let labelView = NSTextField(labelWithString: label)
        labelView.alignment = .left
        labelView.widthAnchor.constraint(equalToConstant: 96).isActive = true
        row.addArrangedSubview(labelView)
        row.addArrangedSubview(control)
        control.setContentHuggingPriority(.defaultLow, for: .horizontal)
        control.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        row.distribution = .fill
        return row
    }

    func providerKeysEditor() -> NSStackView {
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .width
        content.spacing = 10
        content.edgeInsets = NSEdgeInsets(top: 6, left: 0, bottom: 0, right: 0)

        let title = NSTextField(labelWithString: "API keys")
        title.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        title.textColor = .secondaryLabelColor
        title.alignment = .left
        let titleRow = NSStackView()
        titleRow.orientation = .horizontal
        titleRow.alignment = .centerY
        titleRow.addArrangedSubview(title)
        titleRow.addArrangedSubview(spacer())
        content.addArrangedSubview(titleRow)

        let keyRow = NSStackView()
        keyRow.orientation = .horizontal
        keyRow.alignment = .top
        keyRow.spacing = 12
        content.addArrangedSubview(keyRow)

        let listScroll = scrollView(for: providerKeyTableView, height: 112)
        listScroll.hasHorizontalScroller = false
        listScroll.widthAnchor.constraint(greaterThanOrEqualToConstant: 110).isActive = true
        let preferredListWidth = listScroll.widthAnchor.constraint(equalToConstant: 130)
        preferredListWidth.priority = .defaultHigh
        preferredListWidth.isActive = true
        listScroll.setContentHuggingPriority(.required, for: .horizontal)
        listScroll.setContentCompressionResistancePriority(.required, for: .horizontal)
        keyRow.addArrangedSubview(listScroll)

        let keyFields = NSStackView()
        keyFields.orientation = .vertical
        keyFields.alignment = .width
        keyFields.spacing = 8
        keyFields.setContentHuggingPriority(.defaultLow, for: .horizontal)
        keyFields.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        keyFields.addArrangedSubview(compactFormRow("Label", providerKeyNameField))
        keyFields.addArrangedSubview(compactFormRow("API key", providerApiKeyField))
        keyRow.addArrangedSubview(keyFields)
        keyFields.trailingAnchor.constraint(equalTo: keyRow.trailingAnchor).isActive = true

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

    func providerEnabledRow() -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8

        row.addArrangedSubview(providerEnabledCheckbox)
        row.addArrangedSubview(spacer())
        row.heightAnchor.constraint(greaterThanOrEqualToConstant: 24).isActive = true
        return row
    }

    func compactFormRow(_ label: String, _ control: NSView) -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = 8

        let labelView = NSTextField(labelWithString: label)
        labelView.alignment = .left
        labelView.widthAnchor.constraint(equalToConstant: 48).isActive = true
        row.addArrangedSubview(labelView)
        row.addArrangedSubview(control)
        control.setContentHuggingPriority(.defaultLow, for: .horizontal)
        control.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        row.distribution = .fill
        return row
    }

    func modelEnabledRow() -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        row.addArrangedSubview(enabledCheckbox)
        row.addArrangedSubview(probeModelAvailabilityButton)
        row.addArrangedSubview(modelProbeStatusLabel)
        row.addArrangedSubview(spacer())
        row.heightAnchor.constraint(greaterThanOrEqualToConstant: 24).isActive = true
        return row
    }

    func modelBreadcrumbView() -> NSStackView {
        let breadcrumb = NSStackView()
        breadcrumb.orientation = .horizontal
        breadcrumb.alignment = .centerY
        breadcrumb.spacing = 5
        let separator = NSTextField(labelWithString: ">")
        separator.textColor = .tertiaryLabelColor
        separator.font = NSFont.systemFont(ofSize: 13, weight: .regular)
        modelBreadcrumbModelLabel.textColor = .secondaryLabelColor
        breadcrumb.addArrangedSubview(modelBreadcrumbProviderButton)
        breadcrumb.addArrangedSubview(separator)
        breadcrumb.addArrangedSubview(modelBreadcrumbModelLabel)
        return breadcrumb
    }

    func providerEditorHeaderView() -> NSStackView {
        let header = NSStackView()
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 8
        header.addArrangedSubview(providerEditorTitleLabel)
        header.addArrangedSubview(spacer())
        header.addArrangedSubview(providerReturnToModelButton)
        return header
    }

    func modelBillingSummaryPanel() -> NSStackView {
        let panel = NSStackView()
        panel.orientation = .vertical
        panel.alignment = .width
        panel.spacing = 5
        panel.edgeInsets = NSEdgeInsets(top: 4, left: 0, bottom: 6, right: 0)
        panel.addArrangedSubview(modelDetailGridRow("Balance", modelBillingStatusLabel, controlInset: 10))
        panel.addArrangedSubview(modelDetailGridRow("Usage", modelUsageStatusLabel, controlInset: 10))
        panel.addArrangedSubview(modelDetailGridRow("Multiplier", modelMultiplierStatusLabel, controlInset: 10))
        return panel
    }

    func compactModelFormRow(
        _ title: String,
        _ control: NSView,
        preferredWidth: CGFloat,
        minWidth: CGFloat
    ) -> NSView {
        control.widthAnchor.constraint(greaterThanOrEqualToConstant: minWidth).isActive = true
        let preferredControlWidth = control.widthAnchor.constraint(equalToConstant: preferredWidth)
        preferredControlWidth.priority = .defaultHigh
        preferredControlWidth.isActive = true
        return modelDetailGridRow(title, control)
    }

    func modelDetailGridRow(
        _ title: String,
        _ control: NSView,
        controlInset: CGFloat = 8,
        labelWidth: CGFloat? = nil
    ) -> NSView {
        let row = NSView()
        let label = NSTextField(labelWithString: title)
        label.alignment = .left
        label.lineBreakMode = .byClipping
        label.setContentCompressionResistancePriority(.required, for: .horizontal)
        label.translatesAutoresizingMaskIntoConstraints = false
        control.translatesAutoresizingMaskIntoConstraints = false
        row.addSubview(label)
        row.addSubview(control)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: row.leadingAnchor),
            label.widthAnchor.constraint(equalToConstant: labelWidth ?? modelFormLabelWidth),
            label.centerYAnchor.constraint(equalTo: row.centerYAnchor),
            control.leadingAnchor.constraint(equalTo: label.trailingAnchor, constant: controlInset),
            control.trailingAnchor.constraint(lessThanOrEqualTo: row.trailingAnchor, constant: -4),
            control.topAnchor.constraint(greaterThanOrEqualTo: row.topAnchor),
            control.bottomAnchor.constraint(lessThanOrEqualTo: row.bottomAnchor),
            control.centerYAnchor.constraint(equalTo: row.centerYAnchor),
            row.heightAnchor.constraint(greaterThanOrEqualTo: control.heightAnchor),
            row.heightAnchor.constraint(greaterThanOrEqualToConstant: 24),
        ])
        return row
    }

    func upstreamApiModeRow() -> NSView {
        configureUpstreamApiModeRowsIfNeeded()
        upstreamApiModeStackView.toolTip = "Fallback order used from LiteLLM to the upstream endpoint. This does not change the client-facing API."
        return modelDetailGridRow("API order", upstreamApiModeStackView)
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
        applyButton.isEnabled = pending && !externalImportInFlight && !runtimeApplyInFlight
    }

    func captureConfigurationBaseline(
        providers: [EditableProvider]? = nil,
        document: ConfigEditorDocument? = nil,
        preservesNilDocument: Bool = true
    ) {
        configurationBaselineProviders = providers ?? self.providers
        configurationBaselineDocument = preservesNilDocument ? (document ?? sourceDocument) : document
        refreshPendingChanges()
    }

    func refreshPendingChanges(updateStatus: Bool = true) {
        let changed = providers != configurationBaselineProviders
            || sourceDocument != configurationBaselineDocument
        setPendingChanges(changed, updateStatus: updateStatus)
    }

    func markPendingChanges(updateStatus: Bool = true) {
        guard !isRenderingSelection else { return }
        refreshPendingChanges(updateStatus: updateStatus)
    }

    func markPendingChangesIfNeeded(_ changed: Bool, updateStatus: Bool = true) {
        markPendingChanges(updateStatus: updateStatus)
    }

    func setEditorError(_ title: String, message: String) {
        let detail = message.trimmingCharacters(in: .whitespacesAndNewlines)
        let inline = detail.isEmpty ? title : "\(title): \(detail)"
        setEditorStatus(
            elidedDisplayText(inline, limit: inlineStatusLimit),
            tooltip: detail.isEmpty ? title : "\(title)\n\(detail)"
        )
        showAlert(title: title, message: detail)
    }
}
