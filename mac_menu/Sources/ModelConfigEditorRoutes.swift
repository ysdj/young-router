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
        displayedUpstreamApiModes = normalizedUpstreamApiModes(modes)
            + upstreamApiModes.filter { !modes.contains($0) }
        supportsOpenAIChatCheckbox.state = modes.contains("openai/chat") ? .on : .off
        supportsOpenAIResponsesCheckbox.state = modes.contains("openai/responses") ? .on : .off
        supportsAnthropicCheckbox.state = modes.contains("anthropic") ? .on : .off
        refreshUpstreamApiModeRows()
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
        case "openai/chat": return "OpenAI Chat"
        case "anthropic": return "Anthropic Messages"
        default: return "OpenAI Responses"
        }
    }

    func configureUpstreamApiModeRowsIfNeeded() {
        guard upstreamApiModeRows.isEmpty else { return }
        for mode in upstreamApiModes {
            let row = NSStackView()
            row.orientation = .horizontal
            row.alignment = .centerY
            row.spacing = 6
            let rank = NSTextField(labelWithString: "")
            rank.alignment = .right
            rank.widthAnchor.constraint(equalToConstant: 18).isActive = true
            let checkbox = upstreamApiCheckbox(for: mode)
            checkbox.title = upstreamApiDisplayName(mode)
            checkbox.widthAnchor.constraint(equalToConstant: 148).isActive = true
            let status = NSTextField(labelWithString: "Not probed")
            status.textColor = .secondaryLabelColor
            status.lineBreakMode = .byTruncatingTail
            status.widthAnchor.constraint(equalToConstant: 235).isActive = true
            let up = NSButton(
                image: NSImage(systemSymbolName: "chevron.up", accessibilityDescription: "Move protocol up")!,
                target: self,
                action: #selector(moveUpstreamApiModeUp(_:))
            )
            let down = NSButton(
                image: NSImage(systemSymbolName: "chevron.down", accessibilityDescription: "Move protocol down")!,
                target: self,
                action: #selector(moveUpstreamApiModeDown(_:))
            )
            for (button, tooltip) in [(up, "Move protocol earlier"), (down, "Move protocol later")] {
                button.bezelStyle = .inline
                button.identifier = NSUserInterfaceItemIdentifier(mode)
                button.toolTip = tooltip
                button.widthAnchor.constraint(equalToConstant: 22).isActive = true
                button.heightAnchor.constraint(equalToConstant: 22).isActive = true
                row.addArrangedSubview(button)
            }
            row.insertArrangedSubview(status, at: 0)
            row.insertArrangedSubview(checkbox, at: 0)
            row.insertArrangedSubview(rank, at: 0)
            upstreamApiModeRows[mode] = row
            upstreamApiModeRankLabels[mode] = rank
            upstreamApiModeStatusLabels[mode] = status
        }
    }

    func refreshUpstreamApiModeRows() {
        configureUpstreamApiModeRowsIfNeeded()
        for view in upstreamApiModeStackView.arrangedSubviews {
            upstreamApiModeStackView.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        let summaries = upstreamApiProbeKey == selectedModelProbeKey()
            ? upstreamApiProbeSummaries
            : [:]
        let details = upstreamApiProbeKey == selectedModelProbeKey()
            ? upstreamApiProbeDetails
            : [:]
        for (index, mode) in displayedUpstreamApiModes.enumerated() {
            guard let row = upstreamApiModeRows[mode] else { continue }
            upstreamApiModeRankLabels[mode]?.stringValue = "\(index + 1)"
            upstreamApiModeStatusLabels[mode]?.stringValue = summaries[mode] ?? "Not probed"
            upstreamApiModeStatusLabels[mode]?.toolTip = details[mode]
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
                  self.selectedModelIdentity() == identity,
                  let current = self.modelSelectionIndices(for: identity) else { return }
            self.selectedModelInfoInFlight = false
            if case .success(let capability) = result {
                self.selectedModelImageGenerationEndpointDisabled = capability?.isImageGenerationEndpointModel == true
                if let capability {
                    var model = self.providers[current.provider].models[current.model]
                    if capability.isImageGenerationEndpointModel {
                        model.supportsImageGeneration = false
                        model.supportsImageGenerationPresent = false
                    } else if let supportsImageGenerationFlag = capability.supportsImageGenerationFlag {
                        model.supportsImageGeneration = supportsImageGenerationFlag
                        model.supportsImageGenerationPresent = supportsImageGenerationFlag
                    }
                    self.providers[current.provider].models[current.model] = model
                }
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
                    EditableProviderKey(name: defaultProviderKeyName, value: providers[providerIndex].apiKey, enabled: true)
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

    func providerKeyEnabled(providerIndex: Int, keyName: String) -> Bool {
        let trimmed = keyName.trimmingCharacters(in: .whitespacesAndNewlines)
        return normalizedProviderKeys(providerIndex).first { $0.name == trimmed }?.enabled ?? true
    }

    func modelEffectivelyEnabled(providerIndex: Int, model: EditableModel) -> Bool {
        providers[providerIndex].enabled
            && providerKeyEnabled(providerIndex: providerIndex, keyName: model.apiKeyName)
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
                    enabled: provider.enabled && (key?.enabled ?? false) && model.modelEnabled
                ))
            }
        }
        return rows.sorted(by: routeRowComesBefore)
    }

    func routeTableRows() -> [RouteTableRow] {
        let routes = routeRows()
        var tableRows: [RouteTableRow] = []
        var group: [RouteDeploymentRow] = []

        func appendCurrentGroup() {
            guard let first = group.first else { return }
            let runningCount = group.filter { $0.enabled }.count
            tableRows.append(.modelGroup(RouteModelGroupRow(
                publicModel: first.publicModel,
                routeCount: group.count,
                runningCount: runningCount,
                offCount: group.count - runningCount
            )))
            tableRows.append(contentsOf: group.map { .deployment($0) })
        }

        for route in routes {
            if let first = group.first, first.publicModel != route.publicModel {
                appendCurrentGroup()
                group.removeAll(keepingCapacity: true)
            }
            group.append(route)
        }
        appendCurrentGroup()
        return tableRows
    }

    func routeDeployment(atTableRow row: Int) -> RouteDeploymentRow? {
        let rows = routeTableRows()
        guard row >= 0, row < rows.count else { return nil }
        if case .deployment(let route) = rows[row] {
            return route
        }
        return nil
    }

    func routeGroup(atTableRow row: Int) -> RouteModelGroupRow? {
        let rows = routeTableRows()
        guard row >= 0, row < rows.count else { return nil }
        if case .modelGroup(let group) = rows[row] {
            return group
        }
        return nil
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
            "Order: \(route.order.map { "\($0)" } ?? "(none)")",
            "Provider/key: \(route.providerName) / \(route.keyName)",
            "Upstream: \(route.upstreamModel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "(blank)" : route.upstreamModel)",
        ]
        if !route.enabled {
            lines.append("Status: OFF (\(routeOffReason(route)))")
        } else {
            lines.append("Status: RUN")
        }
        return lines.joined(separator: "\n")
    }

    func routeGroupTooltip(_ group: RouteModelGroupRow) -> String {
        [
            "Public model: \(group.publicModel)",
            "Routes: \(group.routeCount)",
            "Running: \(group.runningCount)",
            "Off: \(group.offCount)",
        ].joined(separator: "\n")
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
        if let key, !key.enabled { reasons.append("key disabled") }
        if !model.modelEnabled { reasons.append("model disabled") }
        return reasons.isEmpty ? "unknown" : reasons.joined(separator: ", ")
    }

    func reloadRouteTable(preserving identity: ModelSelectionIdentity? = nil) {
        let target = identity ?? selectedRouteIdentity ?? modelEditorTarget
        let wasRenderingSelection = isRenderingSelection
        isRenderingSelection = true
        routeTableView.reloadData()
        if let target, let current = modelSelectionIndices(for: target) {
            selectRoute(providerIndex: current.provider, modelIndex: current.model)
        } else {
            routeTableView.deselectAll(nil)
        }
        isRenderingSelection = wasRenderingSelection
        refreshRouteControlsEnabled()
    }

    func reloadRouteTable(preserving indices: (provider: Int, model: Int)) {
        reloadRouteTable(preserving: modelSelectionIdentity(providerIndex: indices.provider, modelIndex: indices.model))
    }

    func routeGroup(for publicModel: String) -> [RouteDeploymentRow] {
        routeRows().filter { $0.publicModel == publicModel }
    }

    func firstDeploymentTableRowIndex(inGroup publicModel: String? = nil) -> Int? {
        routeTableRows().firstIndex {
            if case .deployment(let route) = $0 {
                return publicModel == nil || route.publicModel == publicModel
            }
            return false
        }
    }

    func refreshRouteControlsEnabled() {
        guard viewMode == .routes, let selected = selectedRouteRow() else {
            routeMoveUpButton.isEnabled = false
            routeMoveDownButton.isEnabled = false
            routeNormalizeButton.isEnabled = false
            return
        }
        let group = routeGroup(for: selected.publicModel)
        guard let index = group.firstIndex(where: { $0.providerIndex == selected.providerIndex && $0.modelIndex == selected.modelIndex }) else {
            routeMoveUpButton.isEnabled = false
            routeMoveDownButton.isEnabled = false
            routeNormalizeButton.isEnabled = false
            return
        }
        routeMoveUpButton.isEnabled = index > 0
        routeMoveDownButton.isEnabled = index < group.count - 1
        routeNormalizeButton.isEnabled = !group.isEmpty
    }

    func applyEditorViewMode() {
        refreshViewModeButtons()
        providerCascadeView?.isHidden = viewMode != .providers
        routesListView?.isHidden = viewMode != .routes
        if viewMode == .routes {
            reloadRouteTable()
            if selectedRouteRow() == nil, routeTableView.numberOfRows > 0 {
                isRenderingSelection = true
                if let firstRouteIndex = firstDeploymentTableRowIndex() {
                    routeTableView.selectRowIndexes(IndexSet(integer: firstRouteIndex), byExtendingSelection: false)
                    routeTableView.scrollRowToVisible(firstRouteIndex)
                } else {
                    routeTableView.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
                    routeTableView.scrollRowToVisible(0)
                }
                isRenderingSelection = false
                renderRouteSelection()
            }
        } else {
            refreshRouteControlsEnabled()
        }
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
        reloadRouteTable(preserving: identity)
        if let current = modelSelectionIndices(for: identity) {
            isRenderingSelection = true
            selectModel(providerIndex: current.provider, modelIndex: current.model)
            isRenderingSelection = false
            renderModelSelection()
        }
        refreshRuntimeMap()
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

    func modelRouteSummary(_ model: EditableModel) -> String {
        let key = model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines)
        let order = model.order.trimmingCharacters(in: .whitespacesAndNewlines)
        let keyText = key.isEmpty ? "(no key)" : key
        return order.isEmpty ? keyText : "\(keyText) / o\(order)"
    }

    func modelRouteTooltip(_ model: EditableModel) -> String {
        let split = splitLiteLLMModel(model.litellmModel)
        let adapter = split.0.trimmingCharacters(in: .whitespacesAndNewlines)
        let upstream = split.1.trimmingCharacters(in: .whitespacesAndNewlines)
        let order = model.order.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines)
        return [
            "Public model: \(model.displayName)",
            "Upstream: \(upstream.isEmpty ? "(blank)" : upstream)",
            "Adapter: \(adapter.isEmpty ? "(none)" : adapter)",
            "Key: \(key.isEmpty ? "(no key)" : key)",
            "Order: \(order.isEmpty ? "(none)" : order)",
        ].joined(separator: "\n")
    }

    func refreshRuntimeMap() {
        let deployments = runtimeDeployments()
        let grouped = Dictionary(grouping: deployments) { $0.publicModel }
        let preferredModelNames = preferredRuntimeModelNames()
        let modelNames = grouped.keys
            .filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            .sorted { left, right in
                let leftPreferred = preferredModelNames.firstIndex(of: left)
                let rightPreferred = preferredModelNames.firstIndex(of: right)
                if leftPreferred != rightPreferred {
                    return (leftPreferred ?? Int.max) < (rightPreferred ?? Int.max)
                }
                return left < right
            }
        var rows: [RuntimeMapRow] = [
            .summary(RuntimeMapSummaryRow(
                modelCount: modelNames.count,
                runningCount: deployments.filter { $0.enabled }.count,
                offCount: deployments.filter { !$0.enabled }.count
            )),
        ]
        for modelName in modelNames {
            let group = (grouped[modelName] ?? []).sorted(by: runtimeDeploymentComesBefore)
            rows.append(.model(RuntimeMapModelRow(
                publicModel: modelName,
                runningCount: group.filter { $0.enabled }.count,
                offCount: group.filter { !$0.enabled }.count
            )))
            let byOrder = Dictionary(grouping: group) { $0.order }
            let orders = byOrder.keys.sorted { orderSortValue($0) < orderSortValue($1) }
            for (index, order) in orders.enumerated() {
                let orderDeployments = (byOrder[order] ?? []).sorted(by: runtimeDeploymentComesBefore)
                rows.append(.order(RuntimeMapOrderRow(
                    order: order,
                    previousOrder: index > 0 ? orders[index - 1] : nil,
                    isFirst: index == 0,
                    runningCount: orderDeployments.filter { $0.enabled }.count,
                    offCount: orderDeployments.filter { !$0.enabled }.count
                )))
                rows.append(contentsOf: orderDeployments.map { .deployment($0) })
            }
        }
        if modelNames.isEmpty {
            rows.append(.empty)
        }

        runtimeMapRows = rows
        runtimeMapTableView.reloadData()
        scrollRuntimeMapToTop()
    }

    func preferredRuntimeModelNames() -> [String] {
        if let providerIndex = selectedProviderIndex {
            if let modelIndex = selectedModelIndex,
               modelIndex >= 0,
               modelIndex < providers[providerIndex].models.count {
                let selectedName = providers[providerIndex].models[modelIndex].modelName.trimmingCharacters(in: .whitespacesAndNewlines)
                if !selectedName.isEmpty {
                    return [selectedName]
                }
            }

            var names: [String] = []
            for model in providers[providerIndex].models {
                let name = model.modelName.trimmingCharacters(in: .whitespacesAndNewlines)
                if !name.isEmpty && !names.contains(name) {
                    names.append(name)
                }
            }
            return names
        }
        return []
    }

    func scrollRuntimeMapToTop() {
        DispatchQueue.main.async { [weak self] in
            guard let self, let scrollView = self.runtimeMapScrollView else { return }
            scrollView.contentView.scroll(to: .zero)
            scrollView.reflectScrolledClipView(scrollView.contentView)
        }
    }

    func scrollTableToTop(_ tableView: NSTableView) {
        guard tableView.numberOfRows > 0 else { return }
        DispatchQueue.main.async { [weak tableView] in
            guard let tableView, tableView.numberOfRows > 0 else { return }
            tableView.scrollRowToVisible(0)
        }
    }

    func runtimeMapCell(at row: Int) -> NSView? {
        guard runtimeMapRows.indices.contains(row) else { return nil }
        switch runtimeMapRows[row] {
        case .summary(let summary):
            return runtimeMapSummaryCell(summary)
        case .model(let model):
            return runtimeMapModelCell(model)
        case .order(let order):
            return runtimeMapOrderCell(order)
        case .deployment(let deployment):
            return runtimeMapDeploymentCell(deployment)
        case .empty:
            return runtimeMapEmptyCell()
        }
    }

    func runtimeMapSummaryCell(_ summary: RuntimeMapSummaryRow) -> NSView {
        let content = NSStackView()
        content.orientation = .horizontal
        content.alignment = .centerY
        content.spacing = 7
        let modelLabel = runtimeMapLabel(
            "\(summary.modelCount) \(summary.modelCount == 1 ? "model" : "models")",
            font: NSFont.systemFont(ofSize: 11, weight: .semibold)
        )
        content.addArrangedSubview(modelLabel)
        content.addArrangedSubview(runtimeMapStatusToken(
            text: "\(summary.runningCount) RUN",
            color: .systemGreen
        ))
        content.addArrangedSubview(runtimeMapStatusToken(
            text: "\(summary.offCount) OFF",
            color: .tertiaryLabelColor
        ))
        content.addArrangedSubview(spacer())
        content.addArrangedSubview(runtimeMapFallbackFlowView())
        content.toolTip = "Fallback order: try the selected deployment's protocols in order, then another RUN deployment at the same route order, then the next route order. Cooldown is isolated by deployment and protocol."
        return runtimeMapCellContainer(content, verticalInset: 5)
    }

    func runtimeMapModelCell(_ model: RuntimeMapModelRow) -> NSView {
        let content = NSStackView()
        content.orientation = .horizontal
        content.alignment = .centerY
        content.spacing = 7
        if let icon = runtimeMapSymbolView(
            name: "rectangle.stack",
            description: "Model group",
            color: .secondaryLabelColor
        ) {
            content.addArrangedSubview(icon)
        }
        let name = runtimeMapLabel(
            model.publicModel,
            font: NSFont.systemFont(ofSize: 12, weight: .semibold),
            lineBreakMode: .byTruncatingMiddle
        )
        name.toolTip = model.publicModel
        content.addArrangedSubview(name)
        content.addArrangedSubview(spacer())
        content.addArrangedSubview(runtimeMapStatusToken(
            text: "\(model.runningCount) RUN",
            color: model.runningCount > 0 ? .systemGreen : .tertiaryLabelColor
        ))
        if model.offCount > 0 {
            content.addArrangedSubview(runtimeMapStatusToken(
                text: "\(model.offCount) OFF",
                color: .tertiaryLabelColor
            ))
        }
        content.toolTip = "\(model.publicModel): \(model.runningCount) active and \(model.offCount) disabled deployments."
        return runtimeMapCellContainer(content, horizontalInset: 7, verticalInset: 4)
    }

    func runtimeMapOrderCell(_ order: RuntimeMapOrderRow) -> NSView {
        let content = NSStackView()
        content.orientation = .horizontal
        content.alignment = .centerY
        content.spacing = 6
        let symbolName = order.isFirst ? "arrow.right" : "arrow.turn.down.right"
        if let icon = runtimeMapSymbolView(
            name: symbolName,
            description: order.isFirst ? "Start order" : "Next order",
            color: .controlAccentColor
        ) {
            content.addArrangedSubview(icon)
        }
        let orderLabel = runtimeMapLabel(
            runtimeMapOrderLabel(order.order),
            font: NSFont.systemFont(ofSize: 10.5, weight: .semibold),
            color: .controlAccentColor
        )
        content.addArrangedSubview(orderLabel)
        let transition: String
        if order.isFirst {
            transition = "start"
        } else {
            transition = "after \(runtimeMapOrderLabel(order.previousOrder)) exhausted"
        }
        var details = [transition, "\(order.runningCount) RUN"]
        if order.offCount > 0 {
            details.append("\(order.offCount) OFF")
        }
        let detailLabel = runtimeMapLabel(
            details.joined(separator: "  |  "),
            font: NSFont.systemFont(ofSize: 10),
            color: .secondaryLabelColor
        )
        content.addArrangedSubview(detailLabel)
        content.addArrangedSubview(spacer())
        content.toolTip = "Each RUN deployment exhausts its configured protocol chain before routing tries another RUN deployment at this order. After all peers at this order are exhausted, routing advances to the next order."
        return runtimeMapCellContainer(content, horizontalInset: 16, verticalInset: 3)
    }

    func runtimeMapDeploymentCell(_ deployment: RuntimeDeployment) -> NSView {
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 1

        let top = NSStackView()
        top.orientation = .horizontal
        top.alignment = .centerY
        top.spacing = 6
        let status = runtimeMapStatusToken(
            text: deployment.enabled ? "RUN" : "OFF",
            color: deployment.enabled ? .systemGreen : .tertiaryLabelColor
        )
        status.widthAnchor.constraint(equalToConstant: 40).isActive = true
        top.addArrangedSubview(status)

        let provider = runtimeMapLabel(
            "\(deployment.providerName) / \(deployment.keyName)",
            font: NSFont.systemFont(ofSize: 11, weight: .medium),
            color: deployment.enabled ? .labelColor : .secondaryLabelColor,
            lineBreakMode: .byTruncatingMiddle
        )
        provider.widthAnchor.constraint(lessThanOrEqualToConstant: 170).isActive = true
        provider.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        top.addArrangedSubview(provider)
        if let arrow = runtimeMapSymbolView(
            name: "arrow.right",
            description: "Routes to",
            color: .tertiaryLabelColor,
            size: 10
        ) {
            top.addArrangedSubview(arrow)
        }
        let upstream = deployment.upstreamModel.trimmingCharacters(in: .whitespacesAndNewlines)
        let host = apiBaseHost(deployment.apiBase)
        let endpoint = "\(upstream.isEmpty ? "(no upstream model)" : upstream) @ \(host.isEmpty ? "(no host)" : host)"
        let endpointLabel = runtimeMapLabel(
            endpoint,
            font: NSFont.systemFont(ofSize: 10.5),
            color: deployment.enabled ? .secondaryLabelColor : .tertiaryLabelColor,
            lineBreakMode: .byTruncatingMiddle
        )
        endpointLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        endpointLabel.toolTip = endpoint
        top.addArrangedSubview(endpointLabel)
        content.addArrangedSubview(top)
        top.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true

        let bottom = NSStackView()
        bottom.orientation = .horizontal
        bottom.alignment = .centerY
        bottom.spacing = 6
        let indent = NSView()
        indent.widthAnchor.constraint(equalToConstant: 46).isActive = true
        bottom.addArrangedSubview(indent)
        bottom.addArrangedSubview(runtimeMapProtocolChain(deployment))
        bottom.addArrangedSubview(spacer())
        if deployment.supportsImageGeneration && !deployment.isImageGenerationEndpoint,
           let icon = runtimeMapSymbolView(
               name: "photo",
               description: "Responses image-generation tool",
               color: deployment.enabled ? .systemPurple : .tertiaryLabelColor
           ) {
            icon.toolTip = "Supports the Responses image-generation tool."
            bottom.addArrangedSubview(icon)
        }
        if needsBrowserCompatibleHeaders(apiBase: deployment.apiBase),
           let icon = runtimeMapSymbolView(
               name: "globe",
               description: "Browser-compatible headers",
               color: deployment.enabled ? .systemBlue : .tertiaryLabelColor
           ) {
            icon.toolTip = "Adds browser-compatible headers for this upstream host."
            bottom.addArrangedSubview(icon)
        }
        content.addArrangedSubview(bottom)
        bottom.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        content.toolTip = runtimeDeploymentTooltip(deployment)
        return runtimeMapCellContainer(content, horizontalInset: 8, verticalInset: 3)
    }

    func runtimeMapEmptyCell() -> NSView {
        let content = NSStackView()
        content.orientation = .horizontal
        content.alignment = .centerY
        content.spacing = 7
        if let icon = runtimeMapSymbolView(
            name: "tray",
            description: "No deployments",
            color: .tertiaryLabelColor,
            size: 14
        ) {
            content.addArrangedSubview(icon)
        }
        content.addArrangedSubview(runtimeMapLabel(
            "No configured model deployments",
            font: NSFont.systemFont(ofSize: 11),
            color: .secondaryLabelColor
        ))
        content.addArrangedSubview(spacer())
        return runtimeMapCellContainer(content, horizontalInset: 16, verticalInset: 8)
    }

    func runtimeMapCellContainer(
        _ content: NSView,
        horizontalInset: CGFloat = 8,
        verticalInset: CGFloat
    ) -> NSTableCellView {
        let cell = NSTableCellView()
        content.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: horizontalInset),
            content.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -horizontalInset),
            content.topAnchor.constraint(equalTo: cell.topAnchor, constant: verticalInset),
            content.bottomAnchor.constraint(equalTo: cell.bottomAnchor, constant: -verticalInset),
        ])
        return cell
    }

    func runtimeMapLabel(
        _ text: String,
        font: NSFont,
        color: NSColor = .labelColor,
        lineBreakMode: NSLineBreakMode = .byTruncatingTail
    ) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = font
        label.textColor = color
        label.usesSingleLineMode = true
        label.lineBreakMode = lineBreakMode
        return label
    }

    func runtimeMapStatusToken(text: String, color: NSColor) -> NSStackView {
        let token = NSStackView()
        token.orientation = .horizontal
        token.alignment = .centerY
        token.spacing = 4
        let dot = NSView()
        dot.wantsLayer = true
        dot.layer?.backgroundColor = color.cgColor
        dot.layer?.cornerRadius = 3
        dot.widthAnchor.constraint(equalToConstant: 6).isActive = true
        dot.heightAnchor.constraint(equalToConstant: 6).isActive = true
        token.addArrangedSubview(dot)
        token.addArrangedSubview(runtimeMapLabel(
            text,
            font: NSFont.systemFont(ofSize: 9.5, weight: .semibold),
            color: color
        ))
        return token
    }

    func runtimeMapFallbackFlowView() -> NSStackView {
        let flow = NSStackView()
        flow.orientation = .horizontal
        flow.alignment = .centerY
        flow.spacing = 4
        flow.addArrangedSubview(runtimeMapLabel(
            "Fallback",
            font: NSFont.systemFont(ofSize: 9.5, weight: .semibold),
            color: .secondaryLabelColor
        ))
        for (index, text) in ["protocol", "peer", "order"].enumerated() {
            if index > 0, let arrow = runtimeMapSymbolView(
                name: "chevron.right",
                description: "then",
                color: .tertiaryLabelColor,
                size: 8
            ) {
                flow.addArrangedSubview(arrow)
            }
            flow.addArrangedSubview(runtimeMapLabel(
                text,
                font: NSFont.systemFont(ofSize: 9.5, weight: index == 0 ? .semibold : .regular),
                color: index == 0 ? .controlAccentColor : .secondaryLabelColor
            ))
        }
        return flow
    }

    func runtimeMapProtocolChain(_ deployment: RuntimeDeployment) -> NSStackView {
        let chain = NSStackView()
        chain.orientation = .horizontal
        chain.alignment = .centerY
        chain.spacing = 4
        if deployment.isImageGenerationEndpoint {
            if let icon = runtimeMapSymbolView(
                name: "photo",
                description: "Images API",
                color: deployment.enabled ? .controlAccentColor : .tertiaryLabelColor,
                size: 10
            ) {
                chain.addArrangedSubview(icon)
            }
            chain.addArrangedSubview(runtimeMapLabel(
                "Images API",
                font: NSFont.systemFont(ofSize: 10, weight: .semibold),
                color: deployment.enabled ? .controlAccentColor : .tertiaryLabelColor
            ))
            chain.toolTip = "Standalone image-generation endpoint."
            return chain
        }

        for (index, mode) in deployment.supportedUpstreamApiModes.enumerated() {
            if index > 0, let arrow = runtimeMapSymbolView(
                name: "chevron.right",
                description: "fallback to",
                color: .tertiaryLabelColor,
                size: 8
            ) {
                chain.addArrangedSubview(arrow)
            }
            let activeColor: NSColor = index == 0 ? .controlAccentColor : .secondaryLabelColor
            let label = runtimeMapLabel(
                runtimeMapProtocolName(mode),
                font: NSFont.systemFont(ofSize: 10, weight: index == 0 ? .semibold : .regular),
                color: deployment.enabled ? activeColor : .tertiaryLabelColor
            )
            label.toolTip = runtimeMapProtocolTooltip(mode)
            chain.addArrangedSubview(label)
        }
        chain.toolTip = "Protocol fallback order. Cooldown is isolated for each deployment and protocol."
        return chain
    }

    func runtimeMapSymbolView(
        name: String,
        description: String,
        color: NSColor,
        size: CGFloat = 11
    ) -> NSImageView? {
        guard let image = NSImage(systemSymbolName: name, accessibilityDescription: description) else {
            return nil
        }
        let view = NSImageView(image: image)
        view.contentTintColor = color
        view.imageScaling = .scaleProportionallyDown
        view.widthAnchor.constraint(equalToConstant: size).isActive = true
        view.heightAnchor.constraint(equalToConstant: size).isActive = true
        return view
    }

    func runtimeMapOrderLabel(_ order: Int?) -> String {
        order.map { "Order \($0)" } ?? "Order -"
    }

    func runtimeMapProtocolName(_ mode: String) -> String {
        switch mode {
        case "anthropic": return "Anthropic"
        case "openai/chat": return "Chat"
        default: return "Responses"
        }
    }

    func runtimeMapProtocolTooltip(_ mode: String) -> String {
        switch mode {
        case "anthropic": return "anthropic via /v1/messages"
        case "openai/chat": return "openai/chat via /v1/chat/completions"
        default: return "openai/responses via /v1/responses"
        }
    }

    func runtimeDeploymentTooltip(_ deployment: RuntimeDeployment) -> String {
        let status = deployment.enabled ? "RUN" : "OFF: \(runtimeDeploymentOffReason(deployment))"
        let upstream = deployment.upstreamModel.trimmingCharacters(in: .whitespacesAndNewlines)
        let host = apiBaseHost(deployment.apiBase)
        let protocols = deployment.isImageGenerationEndpoint
            ? "Images API"
            : deployment.supportedUpstreamApiModes.joined(separator: " -> ")
        var details = [
            status,
            "Provider/key: \(deployment.providerName) / \(deployment.keyName)",
            "Upstream: \(upstream.isEmpty ? "(none)" : upstream)",
            "Host: \(host.isEmpty ? "(none)" : host)",
            "Protocols: \(protocols)",
        ]
        if deployment.supportsImageGeneration {
            details.append("Supports the Responses image-generation tool.")
        }
        if needsBrowserCompatibleHeaders(apiBase: deployment.apiBase) {
            details.append("Browser-compatible headers are added for this host.")
        }
        return details.joined(separator: "\n")
    }

    func runtimeDeployments() -> [RuntimeDeployment] {
        var deployments: [RuntimeDeployment] = []

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
                let apiBase = model.apiBase.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    ? provider.apiBase
                    : model.apiBase
                deployments.append(RuntimeDeployment(
                    id: "\(providerIndex):\(modelIndex):\(keyName)",
                    publicModel: model.modelName.trimmingCharacters(in: .whitespacesAndNewlines),
                    providerName: provider.displayName,
                    keyName: keyName.isEmpty ? "(no-key)" : keyName,
                    upstreamModel: modelUpstreamPart(model.litellmModel),
                    apiBase: apiBase.trimmingCharacters(in: .whitespacesAndNewlines),
                    order: parseOrder(model.order),
                    providerEnabled: provider.enabled,
                    keyEnabled: key?.enabled ?? false,
                    modelEnabled: model.modelEnabled,
                    missingKey: key == nil,
                    supportsImageGeneration: model.supportsImageGeneration,
                    isImageGenerationEndpoint: modelIsImageGenerationEndpointModel(model),
                    supportedUpstreamApiModes: normalizedSupportedUpstreamApiModes(for: model)
                ))
            }
        }

        return deployments
    }

    func parseOrder(_ value: String) -> Int? {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? 1 : Int(text)
    }

    func orderSortValue(_ order: Int?) -> Int {
        order ?? Int.max
    }

    func runtimeDeploymentComesBefore(_ left: RuntimeDeployment, _ right: RuntimeDeployment) -> Bool {
        let leftOrder = orderSortValue(left.order)
        let rightOrder = orderSortValue(right.order)
        if leftOrder != rightOrder {
            return leftOrder < rightOrder
        }
        if left.providerName != right.providerName {
            return left.providerName < right.providerName
        }
        if left.keyName != right.keyName {
            return left.keyName < right.keyName
        }
        return left.upstreamModel < right.upstreamModel
    }

    func runtimeDeploymentOffReason(_ deployment: RuntimeDeployment) -> String {
        var reasons: [String] = []
        if !deployment.providerEnabled { reasons.append("provider") }
        if deployment.missingKey { reasons.append("missing key") }
        if !deployment.keyEnabled && !deployment.missingKey { reasons.append("key") }
        if !deployment.modelEnabled { reasons.append("model") }
        return reasons.isEmpty ? "unknown" : reasons.joined(separator: "+")
    }
}
