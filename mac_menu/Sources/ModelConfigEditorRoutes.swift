import Cocoa

extension ModelConfigEditorController {
    func normalizedUpstreamApiModes(_ values: [String]) -> [String] {
        var modes: [String] = []
        for item in values {
            if item.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                continue
            }
            let mode = normalizedUpstreamApiMode(item)
            if !modes.contains(mode) {
                modes.append(mode)
            }
        }
        return modes
    }

    func normalizedSupportedUpstreamApiModes(for model: EditableModel) -> [String] {
        var modes = normalizedUpstreamApiModes(model.supportedUpstreamApiModes)
        if modes.isEmpty {
            modes = [defaultUpstreamApiMode]
        }
        return modes
    }

    func effectiveUpstreamApiMode(from modes: [String], fallback: String = "") -> String {
        let normalizedModes = normalizedUpstreamApiModes(modes)
        if let first = normalizedModes.first {
            return first
        }
        return normalizedUpstreamApiMode(fallback)
    }

    func setUpstreamApiSupportCheckboxes(_ modes: [String]) {
        supportsOpenAIChatCheckbox.state = modes.contains("openai/chat") ? .on : .off
        supportsOpenAIResponsesCheckbox.state = modes.contains("openai/responses") ? .on : .off
        supportsAnthropicCheckbox.state = modes.contains("anthropic") ? .on : .off
        refreshUpstreamApiModeRows()
    }

    func loadUpstreamApiModeOrder(_ modes: [String]) {
        let normalized = normalizedUpstreamApiModes(modes)
        displayedUpstreamApiModes = normalized
            + upstreamApiModes.filter { !normalized.contains($0) }
        setUpstreamApiSupportCheckboxes(normalized)
    }

    func loadUpstreamApiModeOrder(for model: EditableModel) {
        let enabled = normalizedSupportedUpstreamApiModes(for: model)
        let persisted: [String]
        if case .array(let rawOrder)? = model.modelInfoExtra[upstreamApiModeOrderMetadataKey] {
            persisted = rawOrder.compactMap { value in
                guard case .string(let mode) = value else { return nil }
                return mode
            }
        } else {
            persisted = []
        }
        let normalizedOrder = normalizedUpstreamApiModes(persisted)
        displayedUpstreamApiModes = normalizedOrder
            + enabled.filter { !normalizedOrder.contains($0) }
            + upstreamApiModes.filter { !normalizedOrder.contains($0) && !enabled.contains($0) }
        setUpstreamApiSupportCheckboxes(enabled)
    }

    func persistDisplayedUpstreamApiModeOrder(providerIndex: Int, modelIndex: Int) {
        providers[providerIndex].models[modelIndex].modelInfoExtra[upstreamApiModeOrderMetadataKey] = .array(
            displayedUpstreamApiModes.map { .string($0) }
        )
    }

    func selectedSupportedUpstreamApiModes() -> [String] {
        let selected = Set([
            supportsOpenAIChatCheckbox.state == .on ? "openai/chat" : nil,
            supportsOpenAIResponsesCheckbox.state == .on ? "openai/responses" : nil,
            supportsAnthropicCheckbox.state == .on ? "anthropic" : nil,
        ].compactMap { $0 })
        return displayedUpstreamApiModes.filter { selected.contains($0) }
    }

    func refreshResponsesEndpointSupportControls() {
        let hasModel = selectedModelIndex != nil
        let protocolsEnabled = hasModel && !selectedModelImageGenerationEndpointDisabled
        supportsOpenAIChatCheckbox.isEnabled = protocolsEnabled
        supportsOpenAIResponsesCheckbox.isEnabled = protocolsEnabled
        supportsAnthropicCheckbox.isEnabled = protocolsEnabled
        upstreamApiModeStackView.isHidden = hasModel && selectedModelImageGenerationEndpointDisabled
        refreshUpstreamApiModeRows()
        refreshResponsesEndpointProbeControlsEnabled()
    }

    func upstreamApiCheckbox(for mode: String) -> NSButton {
        switch mode {
        case "openai/chat": return supportsOpenAIChatCheckbox
        case "anthropic": return supportsAnthropicCheckbox
        default: return supportsOpenAIResponsesCheckbox
        }
    }

    func upstreamApiDisplayName(_ mode: String) -> String {
        switch mode {
        case "openai/chat": return "Chat"
        case "anthropic": return "Anthropic"
        default: return "Responses"
        }
    }

    func configureUpstreamApiModeRowsIfNeeded() {
        guard upstreamApiModeRows.isEmpty else { return }
        for mode in upstreamApiModes {
            let row = NSStackView()
            row.orientation = .horizontal
            row.alignment = .centerY
            row.spacing = 3
            row.heightAnchor.constraint(equalToConstant: 24).isActive = true
            let rank = NSTextField(labelWithString: "")
            rank.alignment = .right
            rank.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)
            rank.textColor = .secondaryLabelColor
            rank.widthAnchor.constraint(equalToConstant: 20).isActive = true
            let checkbox = upstreamApiCheckbox(for: mode)
            checkbox.title = upstreamApiDisplayName(mode)
            checkbox.widthAnchor.constraint(equalToConstant: 112).isActive = true
            let up = NSButton(title: "↑", target: self, action: #selector(moveUpstreamApiModeUp(_:)))
            let down = NSButton(title: "↓", target: self, action: #selector(moveUpstreamApiModeDown(_:)))
            for (button, tooltip, accessibilityLabel) in [
                (up, "Move protocol up", "Move protocol up"),
                (down, "Move protocol down", "Move protocol down"),
            ] {
                button.bezelStyle = .rounded
                button.identifier = NSUserInterfaceItemIdentifier(mode)
                button.toolTip = tooltip
                button.setAccessibilityLabel(accessibilityLabel)
                row.addArrangedSubview(button)
            }
            row.insertArrangedSubview(checkbox, at: 0)
            row.insertArrangedSubview(rank, at: 0)
            upstreamApiModeRows[mode] = row
            row.toolTip = "Upstream API priority. Check to enable; move to change the LiteLLM-to-provider fallback order."
            upstreamApiModeRankLabels[mode] = rank
            upstreamApiModeMoveUpButtons[mode] = up
            upstreamApiModeMoveDownButtons[mode] = down
        }
    }

    func refreshUpstreamApiModeRows() {
        configureUpstreamApiModeRowsIfNeeded()
        for view in upstreamApiModeStackView.arrangedSubviews {
            upstreamApiModeStackView.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        for (index, mode) in displayedUpstreamApiModes.enumerated() {
            guard let row = upstreamApiModeRows[mode] else { continue }
            upstreamApiModeRankLabels[mode]?.stringValue = "\(index + 1)"
            let canReorder = selectedModelIndex != nil && !selectedModelImageGenerationEndpointDisabled
            upstreamApiModeMoveUpButtons[mode]?.isEnabled = canReorder && index > 0
            upstreamApiModeMoveDownButtons[mode]?.isEnabled = canReorder && index < displayedUpstreamApiModes.count - 1
            upstreamApiModeStackView.addArrangedSubview(row)
        }
    }

    func moveSelectedUpstreamApiMode(_ mode: String, delta: Int) {
        guard let providerIndex = selectedProviderIndex, let modelIndex = selectedModelIndex else { return }
        guard let index = displayedUpstreamApiModes.firstIndex(of: mode) else { return }
        let destination = index + delta
        guard displayedUpstreamApiModes.indices.contains(destination) else { return }
        displayedUpstreamApiModes.swapAt(index, destination)
        let modes = selectedSupportedUpstreamApiModes()
        guard let primary = modes.first else { return }
        providers[providerIndex].models[modelIndex].supportedUpstreamApiModes = modes
        providers[providerIndex].models[modelIndex].upstreamApiMode = primary
        persistDisplayedUpstreamApiModeOrder(providerIndex: providerIndex, modelIndex: modelIndex)
        refreshUpstreamApiModeRows()
        commitEditor()
        markPendingChanges()
    }

    func refreshSelectedModelInfoState(providerIndex: Int, modelIndex: Int) {
        guard let identity = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: modelIndex) else { return }
        selectedModelInfoRequestGeneration += 1
        let generation = selectedModelInfoRequestGeneration
        selectedModelInfoInFlight = true
        let lookup = LiteLLMModelInfoLookup(
            publicModel: routePublicModelName(providers[providerIndex].models[modelIndex]),
            litellmModel: providers[providerIndex].models[modelIndex].litellmModel.trimmingCharacters(in: .whitespacesAndNewlines),
            upstreamModel: modelUpstreamPart(providers[providerIndex].models[modelIndex].litellmModel).trimmingCharacters(in: .whitespacesAndNewlines),
            apiBase: modelEffectiveAPIBase(providerIndex: providerIndex, model: providers[providerIndex].models[modelIndex]),
            deploymentToken: providers[providerIndex].models[modelIndex].deploymentToken.trimmingCharacters(in: .whitespacesAndNewlines)
        )

        fetchLiteLLMModelInfoCapability(lookup: lookup) { [weak self] result in
            guard let self,
                  self.selectedModelInfoRequestGeneration == generation,
                  self.selectedModelIdentity() == identity else { return }
            self.selectedModelInfoInFlight = false
            if case .success(let capability) = result {
                self.selectedModelImageGenerationEndpointDisabled = capability?.isImageGenerationEndpointModel == true
            }
            self.refreshResponsesEndpointSupportControls()
        }
    }

    func ensureProviderHasKey(_ providerIndex: Int) {
        if providers[providerIndex].apiKeys.isEmpty {
            if providers[providerIndex].apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                providers[providerIndex].apiKeys = [EditableProviderKey.blank()]
            } else {
                providers[providerIndex].apiKeys = [
                    EditableProviderKey(name: defaultProviderKeyName, value: providers[providerIndex].apiKey)
                ]
            }
        }
    }

    func normalizedProviderKeys(_ providerIndex: Int) -> [EditableProviderKey] {
        ensureProviderHasKey(providerIndex)
        return providers[providerIndex].apiKeys.filter {
            !$0.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                || !$0.value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    func modelEffectivelyEnabled(providerIndex: Int, model: EditableModel) -> Bool {
        providers[providerIndex].enabled
            && model.modelEnabled
    }

    func routePublicModelName(_ model: EditableModel) -> String {
        let publicModel = model.modelName.trimmingCharacters(in: .whitespacesAndNewlines)
        return publicModel.isEmpty ? model.displayName : publicModel
    }

    func routeRows() -> [RouteDeploymentRow] {
        var rows: [RouteDeploymentRow] = []
        for providerIndex in providers.indices {
            let provider = providers[providerIndex]
            let keys = normalizedProviderKeys(providerIndex)
            for modelIndex in provider.models.indices {
                let model = provider.models[modelIndex]
                if model.isBlank {
                    continue
                }
                let keyName = model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines)
                let key = keys.first { $0.name == keyName }
                rows.append(RouteDeploymentRow(
                    providerIndex: providerIndex,
                    modelIndex: modelIndex,
                    publicModel: routePublicModelName(model),
                    providerName: provider.displayName,
                    keyName: keyName.isEmpty ? "(no-key)" : keyName,
                    upstreamModel: modelUpstreamPart(model.litellmModel),
                    order: parseOrder(model.order),
                    enabled: provider.enabled && key != nil && model.modelEnabled
                ))
            }
        }
        return rows.sorted(by: routeRowComesBefore)
    }

    func routeTableRows() -> [RouteDeploymentRow] {
        routeRows()
    }

    func routeDeployment(atTableRow row: Int) -> RouteDeploymentRow? {
        let rows = routeTableRows()
        guard rows.indices.contains(row) else { return nil }
        return rows[row]
    }

    func routeStartsModelGroup(atTableRow row: Int) -> Bool {
        let rows = routeTableRows()
        guard rows.indices.contains(row) else { return false }
        return row == 0 || rows[row - 1].publicModel != rows[row].publicModel
    }

    func routeRowComesBefore(_ left: RouteDeploymentRow, _ right: RouteDeploymentRow) -> Bool {
        if left.publicModel != right.publicModel {
            return left.publicModel.localizedCaseInsensitiveCompare(right.publicModel) == .orderedAscending
        }
        let leftOrder = orderSortValue(left.order)
        let rightOrder = orderSortValue(right.order)
        if leftOrder != rightOrder {
            return leftOrder < rightOrder
        }
        if left.providerName != right.providerName {
            return left.providerName.localizedCaseInsensitiveCompare(right.providerName) == .orderedAscending
        }
        if left.keyName != right.keyName {
            return left.keyName.localizedCaseInsensitiveCompare(right.keyName) == .orderedAscending
        }
        if left.upstreamModel != right.upstreamModel {
            return left.upstreamModel.localizedCaseInsensitiveCompare(right.upstreamModel) == .orderedAscending
        }
        if left.providerIndex != right.providerIndex {
            return left.providerIndex < right.providerIndex
        }
        return left.modelIndex < right.modelIndex
    }

    func routeTooltip(_ route: RouteDeploymentRow) -> String {
        var lines = [
            "Public model: \(route.publicModel)",
            "Order: \(route.order.map(orderDisplayText) ?? "(none)")",
            "Provider/key: \(route.providerName) / \(route.keyName)",
            "Upstream: \(route.upstreamModel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "(blank)" : route.upstreamModel)",
        ]
        if !route.enabled {
            lines.append("Status: Disabled (\(routeOffReason(route)))")
        } else {
            lines.append("Status: Enabled")
        }
        return lines.joined(separator: "\n")
    }

    func routeProbeTooltip(_ route: RouteDeploymentRow) -> String {
        if let presentation = modelProbePresentation(
            providerIndex: route.providerIndex,
            modelIndex: route.modelIndex
        ) {
            return "\(routeTooltip(route))\n\nProbe: \(presentation.summary)\n\(presentation.detail)"
        }
        return routeTooltip(route)
    }

    func routeOffReason(_ route: RouteDeploymentRow) -> String {
        guard route.providerIndex >= 0,
              route.providerIndex < providers.count,
              route.modelIndex >= 0,
              route.modelIndex < providers[route.providerIndex].models.count else {
            return "unknown"
        }
        let provider = providers[route.providerIndex]
        let model = provider.models[route.modelIndex]
        let keys = normalizedProviderKeys(route.providerIndex)
        let keyName = model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = keys.first { $0.name == keyName }
        var reasons: [String] = []
        if !provider.enabled { reasons.append("provider disabled") }
        if key == nil { reasons.append("missing key") }
        if !model.modelEnabled { reasons.append("model disabled") }
        return reasons.isEmpty ? "unknown" : reasons.joined(separator: ", ")
    }

    func routeTableViewportOrigin() -> NSPoint? {
        routeTableScrollView?.contentView.bounds.origin
    }

    func restoreRouteTableViewport(_ origin: NSPoint?) {
        guard let origin, let routeTableScrollView else { return }
        routeTableScrollView.contentView.scroll(to: origin)
        routeTableScrollView.reflectScrolledClipView(routeTableScrollView.contentView)
    }

    func reloadRouteTable(
        preserving identity: ModelSelectionIdentity? = nil,
        scrollSelectionIntoView: Bool = false
    ) {
        let target = pendingRouteSelectionIdentity ?? identity ?? selectedRouteIdentity ?? modelEditorTarget
        let viewportOrigin = routeTableViewportOrigin()
        let wasRenderingSelection = isRenderingSelection
        isRenderingSelection = true
        defer { isRenderingSelection = wasRenderingSelection }
        routeTableView.reloadData()
        routeTableView.layoutSubtreeIfNeeded()
        if let target {
            selectRoute(target, scrollIntoView: scrollSelectionIntoView)
        } else {
            routeTableView.deselectAll(nil)
        }
        if !scrollSelectionIntoView {
            restoreRouteTableViewport(viewportOrigin)
        }
        // The target has now been resolved against the current rows. Do not
        // let a click-time identity leak into a later unrelated refresh.
        pendingRouteSelectionIdentity = nil
        refreshRouteControlsEnabled()
    }

    func reloadRouteTable(preserving indices: (provider: Int, model: Int)) {
        reloadRouteTable(preserving: modelSelectionIdentity(providerIndex: indices.provider, modelIndex: indices.model))
    }

    func routeGroup(for publicModel: String) -> [RouteDeploymentRow] {
        routeRows().filter { $0.publicModel == publicModel }
    }

    func refreshRouteControlsEnabled() {
        guard viewMode == .routes, let selected = selectedRouteRow() else {
            routeMoveUpButton.isEnabled = false
            routeMoveDownButton.isEnabled = false
            return
        }
        let group = routeGroup(for: selected.publicModel)
        guard let index = group.firstIndex(where: { $0.providerIndex == selected.providerIndex && $0.modelIndex == selected.modelIndex }) else {
            routeMoveUpButton.isEnabled = false
            routeMoveDownButton.isEnabled = false
            return
        }
        routeMoveUpButton.isEnabled = index > 0
        routeMoveDownButton.isEnabled = index < group.count - 1
    }

    func applyEditorViewMode() {
        refreshViewModeButtons()
        let routesMode = viewMode == .routes
        providersWorkspace?.isHidden = routesMode
        routesWorkspace?.isHidden = !routesMode
        if !routesMode, let detailScrollView {
            detailScrollView.contentView.scroll(to: .zero)
            detailScrollView.reflectScrolledClipView(detailScrollView.contentView)
        }
        if routesMode {
            reloadRouteTable(
                preserving: selectedRouteIdentity ?? modelEditorTarget,
                scrollSelectionIntoView: true
            )
            if selectedRouteRow() == nil, !routeTableRows().isEmpty {
                let firstRouteRow = 0
                isRenderingSelection = true
                routeTableView.selectRowIndexes(IndexSet(integer: firstRouteRow), byExtendingSelection: false)
                routeTableView.scrollRowToVisible(firstRouteRow)
                isRenderingSelection = false
            }
            if let route = selectedRouteRow() {
                showModel(providerIndex: route.providerIndex, modelIndex: route.modelIndex)
            } else {
                clearModelForm()
            }
        } else {
            refreshRouteControlsEnabled()
        }
    }

    func refreshModelProviderPopup(providerIndex: Int?) {
        let selectedProviderID = providerIndex.flatMap { index in
            providers.indices.contains(index) ? providers[index].editorID : nil
        }
        modelProviderPopupButton.removeAllItems()
        for provider in providers {
            modelProviderPopupButton.addItem(withTitle: provider.displayName)
            modelProviderPopupButton.lastItem?.representedObject = provider.editorID
        }
        if let selectedProviderID,
           let item = modelProviderPopupButton.itemArray.first(where: { ($0.representedObject as? UUID) == selectedProviderID }) {
            modelProviderPopupButton.select(item)
        }
        modelProviderPopupButton.isEnabled = providerIndex != nil && providers.count > 1
    }

    func rewriteRouteGroupOrder(_ orderedRows: [RouteDeploymentRow], preserving identity: ModelSelectionIdentity, status: String) {
        var changed = false
        for (offset, route) in orderedRows.enumerated() {
            let providerIndex = route.providerIndex
            let modelIndex = route.modelIndex
            guard providerIndex >= 0,
                  providerIndex < providers.count,
                  modelIndex >= 0,
                  modelIndex < providers[providerIndex].models.count else {
                continue
            }
            let newOrder = "\(offset + 1)"
            if providers[providerIndex].models[modelIndex].order != newOrder {
                providers[providerIndex].models[modelIndex].order = newOrder
                changed = true
            }
        }
        markPendingChangesIfNeeded(changed)
        providerTableView.reloadData()
        modelTableView.reloadData()
        reloadRouteTable(preserving: identity, scrollSelectionIntoView: true)
        if let current = modelSelectionIndices(for: identity) {
            isRenderingSelection = true
            selectModel(providerIndex: current.provider, modelIndex: current.model)
            isRenderingSelection = false
            renderModelSelection()
        }
        if changed {
            setEditorStatus(status)
        }
    }

    func moveSelectedRoute(by delta: Int) {
        let selectedIdentity = selectedRouteIdentity ?? modelEditorTarget
        commitEditor()
        guard let selectedIdentity,
              let current = modelSelectionIndices(for: selectedIdentity) else {
            refreshRouteControlsEnabled()
            return
        }
        let publicModel = routePublicModelName(providers[current.provider].models[current.model])
        var group = routeGroup(for: publicModel)
        guard let currentIndex = group.firstIndex(where: { $0.providerIndex == current.provider && $0.modelIndex == current.model }) else {
            refreshRouteControlsEnabled()
            return
        }
        let targetIndex = currentIndex + delta
        guard targetIndex >= 0, targetIndex < group.count else {
            refreshRouteControlsEnabled()
            return
        }
        group.swapAt(currentIndex, targetIndex)
        let direction = delta < 0 ? "up" : "down"
        rewriteRouteGroupOrder(group, preserving: selectedIdentity, status: "Moved \(publicModel) route \(direction).")
    }

    func modelDeploymentTooltip(_ model: EditableModel) -> String {
        let upstream = modelUpstreamPart(model.litellmModel).trimmingCharacters(in: .whitespacesAndNewlines)
        let order = model.order.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines)
        return [
            "Public model: \(model.displayName)",
            "Upstream: \(upstream.isEmpty ? "(blank)" : upstream)",
            "Key: \(key.isEmpty ? "(no key)" : key)",
            "Order: \(order.isEmpty ? "(none)" : order)",
        ].joined(separator: "\n")
    }

    func scrollTableToTop(_ tableView: NSTableView) {
        guard tableView.numberOfRows > 0 else { return }
        DispatchQueue.main.async { [weak tableView] in
            guard let tableView, tableView.numberOfRows > 0 else { return }
            tableView.scrollRowToVisible(0)
        }
    }

    func parseOrder(_ value: String) -> Decimal? {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return 1 }
        let pattern = #"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"#
        guard text.range(of: pattern, options: .regularExpression) != nil else { return nil }
        return Decimal(string: text, locale: Locale(identifier: "en_US_POSIX"))
    }

    func orderSortValue(_ order: Decimal?) -> Decimal {
        order ?? Decimal.greatestFiniteMagnitude
    }

    func orderDisplayText(_ order: Decimal) -> String {
        NSDecimalNumber(decimal: order).stringValue
    }

}
