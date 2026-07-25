import Cocoa

extension ModelConfigEditorController {
    func numberOfRows(in tableView: NSTableView) -> Int {
        if tableView == providerTableView {
            return providers.count
        }
        if tableView == routeTableView {
            return routeTableRows().count
        }
        if tableView == providerKeyTableView {
            guard let providerIndex = selectedProviderIndex else { return 0 }
            return providers[providerIndex].apiKeys.count
        }
        guard let providerIndex = selectedProviderIndex else { return 0 }
        return providers[providerIndex].models.count
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat { 28 }

    func selectionShouldChange(in tableView: NSTableView) -> Bool {
        if isRenderingSelection {
            return true
        }
        if tableView == routeTableView {
            let clickedRow = tableView.clickedRow
            pendingRouteSelectionIdentity = clickedRow >= 0
                ? routeSelectionIdentity(atTableRow: clickedRow)
                : nil
        } else {
            pendingRouteSelectionIdentity = nil
        }
        commitEditor()
        return true
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !isRenderingSelection else { return }
        guard let tableView = notification.object as? NSTableView else { return }
        if tableView == providerTableView {
            renderProviderSelection()
        } else if tableView == providerKeyTableView {
            renderProviderKeySelection()
        } else if tableView == routeTableView {
            renderRouteSelection()
        } else {
            renderModelSelection()
        }
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let text: String
        var tooltip: String?
        var enabled = true
        if tableView == providerTableView {
            guard row >= 0, row < providers.count else { return nil }
            let provider = providers[row]
            if tableColumn?.identifier == providerCountColumnIdentifier {
                text = "\(provider.models.count)"
            } else {
                text = provider.displayName
            }
            tooltip = provider.displayName
            enabled = provider.enabled
        } else if tableView == modelTableView {
            guard let providerIndex = selectedProviderIndex,
                  row >= 0,
                  row < providers[providerIndex].models.count else { return nil }
            let model = providers[providerIndex].models[row]
            if tableColumn?.identifier == modelBillingColumnIdentifier {
                text = modelBillingSummary(provider: providers[providerIndex], model: model)
            } else if tableColumn?.identifier == modelUpstreamColumnIdentifier {
                let upstream = modelUpstreamPart(model.litellmModel)
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                text = upstream.isEmpty ? "N/A" : upstream
            } else if tableColumn?.identifier == modelApiKeyOrderColumnIdentifier {
                let apiKeyName = model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines)
                let order = model.order.trimmingCharacters(in: .whitespacesAndNewlines)
                text = "\(apiKeyName.isEmpty ? "N/A" : apiKeyName) / \(order.isEmpty ? "1" : order)"
            } else {
                text = model.displayName
            }
            switch tableColumn?.identifier {
            case modelBillingColumnIdentifier:
                tooltip = modelBillingTooltip(provider: providers[providerIndex], model: model)
            case modelApiKeyOrderColumnIdentifier:
                tooltip = "API key and route order: \(text)"
            default:
                tooltip = modelDeploymentTooltip(model)
            }
            enabled = modelEffectivelyEnabled(providerIndex: providerIndex, model: model)
        } else if tableView == routeTableView {
            let rows = routeTableRows()
            guard row >= 0, row < rows.count else { return nil }
            let route = rows[row]
            if tableColumn?.identifier == routeModelColumnIdentifier {
                text = routeStartsModelGroup(atTableRow: row) ? route.publicModel : ""
            } else if tableColumn?.identifier == routeOrderColumnIdentifier {
                text = route.order.map(orderDisplayText) ?? "-"
            } else if tableColumn?.identifier == routeProviderKeyColumnIdentifier {
                text = "\(route.providerName) / \(route.keyName)"
            } else if tableColumn?.identifier == routeUpstreamColumnIdentifier {
                let upstream = route.upstreamModel.trimmingCharacters(in: .whitespacesAndNewlines)
                text = upstream.isEmpty ? "N/A" : upstream
            } else {
                text = ""
            }
            tooltip = routeProbeTooltip(route)
            enabled = route.enabled
        } else {
            guard let providerIndex = selectedProviderIndex,
                  row >= 0,
                  row < providers[providerIndex].apiKeys.count else { return nil }
            text = providers[providerIndex].apiKeys[row].displayName
        }
        let label = NSTextField(labelWithString: text)
        if tableView == modelTableView,
           tableColumn?.identifier == modelBillingColumnIdentifier {
            label.lineBreakMode = .byClipping
            label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        } else {
            label.lineBreakMode = .byTruncatingMiddle
        }
        if tableView == routeTableView,
           tableColumn?.identifier == routeModelColumnIdentifier,
           routeStartsModelGroup(atTableRow: row) {
            label.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        } else if tableView == modelTableView,
                  tableColumn?.identifier == modelBillingColumnIdentifier,
                  let providerIndex = selectedProviderIndex {
            label.textColor = modelEffectivelyEnabled(providerIndex: providerIndex, model: providers[providerIndex].models[row])
                ? .labelColor
                : .secondaryLabelColor
            label.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        } else {
            label.textColor = enabled ? .labelColor : .secondaryLabelColor
        }
        label.alignment = tableColumn?.identifier == providerCountColumnIdentifier
            || tableColumn?.identifier == routeOrderColumnIdentifier ? .right : .left
        label.toolTip = tooltip ?? text
        return verticallyCenteredTableCell(label: label)
    }

    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool {
        true
    }

    func tableView(_ tableView: NSTableView, isGroupRow row: Int) -> Bool {
        false
    }

    func verticallyCenteredTableCell(label: NSTextField) -> NSTableCellView {
        let cell = NSTableCellView()
        cell.textField = label
        label.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 8),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -8),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    var selectedProviderIndex: Int? {
        let row = providerTableView.selectedRow
        return row >= 0 && row < providers.count ? row : nil
    }

    var selectedModelIndex: Int? {
        guard let providerIndex = selectedProviderIndex else { return nil }
        let row = modelTableView.selectedRow
        return row >= 0 && row < providers[providerIndex].models.count ? row : nil
    }

    var selectedProviderKeyIndex: Int? {
        guard let providerIndex = selectedProviderIndex else { return nil }
        let row = providerKeyTableView.selectedRow
        return row >= 0 && row < providers[providerIndex].apiKeys.count ? row : nil
    }

    var selectedRouteIdentity: ModelSelectionIdentity? {
        routeSelectionIdentity(atTableRow: routeTableView.selectedRow)
    }

    func selectedRouteRow() -> RouteDeploymentRow? {
        let row = routeTableView.selectedRow
        return routeDeployment(atTableRow: row)
    }

    func modelSelectionIdentity(providerIndex: Int, modelIndex: Int) -> ModelSelectionIdentity? {
        guard providerIndex >= 0,
              providerIndex < providers.count,
              modelIndex >= 0,
              modelIndex < providers[providerIndex].models.count else {
            return nil
        }
        return ModelSelectionIdentity(
            provider: providerIndex,
            providerID: providers[providerIndex].editorID,
            model: modelIndex,
            modelID: providers[providerIndex].models[modelIndex].editorID
        )
    }

    func modelSelectionIndices(for identity: ModelSelectionIdentity) -> (provider: Int, model: Int)? {
        if identity.provider >= 0,
           identity.provider < providers.count,
           providers[identity.provider].editorID == identity.providerID,
           identity.model >= 0,
           identity.model < providers[identity.provider].models.count,
           providers[identity.provider].models[identity.model].editorID == identity.modelID {
            return (identity.provider, identity.model)
        }

        guard let providerIndex = providers.firstIndex(where: { $0.editorID == identity.providerID }) else {
            return nil
        }
        guard let modelIndex = providers[providerIndex].models.firstIndex(where: { $0.editorID == identity.modelID }) else {
            return nil
        }
        return (providerIndex, modelIndex)
    }

    func selectedModelIdentity() -> ModelSelectionIdentity? {
        guard let providerIndex = selectedProviderIndex,
              let modelIndex = selectedModelIndex else {
            return nil
        }
        return modelSelectionIdentity(providerIndex: providerIndex, modelIndex: modelIndex)
    }

    func selectProvider(at providerIndex: Int) {
        if providerIndex >= 0 && providerIndex < providers.count {
            providerTableView.selectRowIndexes(IndexSet(integer: providerIndex), byExtendingSelection: false)
            providerTableView.scrollRowToVisible(providerIndex)
        }
    }

    func selectModel(providerIndex: Int, modelIndex: Int) {
        selectProvider(at: providerIndex)
        if modelIndex >= 0 && modelIndex < providers[providerIndex].models.count {
            modelTableView.selectRowIndexes(IndexSet(integer: modelIndex), byExtendingSelection: false)
            modelTableView.scrollRowToVisible(modelIndex)
        }
    }

    func routeSelectionIdentity(atTableRow row: Int) -> ModelSelectionIdentity? {
        guard let route = routeDeployment(atTableRow: row) else { return nil }
        return modelSelectionIdentity(providerIndex: route.providerIndex, modelIndex: route.modelIndex)
    }

    func routeTableRowIndex(for identity: ModelSelectionIdentity) -> Int? {
        guard let current = modelSelectionIndices(for: identity) else { return nil }
        return routeTableRows().firstIndex {
            $0.providerIndex == current.provider && $0.modelIndex == current.model
        }
    }

    func selectRoute(
        providerIndex: Int,
        modelIndex: Int,
        scrollIntoView: Bool = false
    ) {
        guard let identity = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: modelIndex) else {
            routeTableView.deselectAll(nil)
            return
        }
        selectRoute(identity, scrollIntoView: scrollIntoView)
    }

    func selectRoute(_ identity: ModelSelectionIdentity, scrollIntoView: Bool = false) {
        guard let rowIndex = routeTableRowIndex(for: identity) else {
            routeTableView.deselectAll(nil)
            return
        }
        routeTableView.selectRowIndexes(IndexSet(integer: rowIndex), byExtendingSelection: false)
        if scrollIntoView {
            routeTableView.scrollRowToVisible(rowIndex)
        }
    }

    func selectProviderKey(at keyIndex: Int) {
        guard let providerIndex = selectedProviderIndex else { return }
        if keyIndex >= 0 && keyIndex < providers[providerIndex].apiKeys.count {
            providerKeyTableView.selectRowIndexes(IndexSet(integer: keyIndex), byExtendingSelection: false)
            providerKeyTableView.scrollRowToVisible(keyIndex)
        }
    }

    func reloadSelectionTablesPreserving(
        providerIndex requestedProviderIndex: Int?,
        modelIndex requestedModelIndex: Int?,
        providerKeyIndex requestedProviderKeyIndex: Int?
    ) {
        let providerIndex = requestedProviderIndex.flatMap {
            $0 >= 0 && $0 < providers.count ? $0 : nil
        }
        let modelIndex = providerIndex.flatMap { providerIndex in
            requestedModelIndex.flatMap {
                $0 >= 0 && $0 < providers[providerIndex].models.count ? $0 : nil
            }
        }
        let providerKeyIndex = providerIndex.flatMap { providerIndex in
            requestedProviderKeyIndex.flatMap {
                $0 >= 0 && $0 < providers[providerIndex].apiKeys.count ? $0 : nil
            }
        }
        let routeIdentity = providerIndex.flatMap { providerIndex in
            modelIndex.flatMap { modelSelectionIdentity(providerIndex: providerIndex, modelIndex: $0) }
        }

        let wasRenderingSelection = isRenderingSelection
        isRenderingSelection = true
        defer { isRenderingSelection = wasRenderingSelection }

        providerTableView.reloadData()
        if let providerIndex {
            providerTableView.selectRowIndexes(IndexSet(integer: providerIndex), byExtendingSelection: false)
            providerTableView.scrollRowToVisible(providerIndex)
        } else {
            providerTableView.deselectAll(nil)
        }

        providerKeyTableView.reloadData()
        if let providerKeyIndex {
            providerKeyTableView.selectRowIndexes(IndexSet(integer: providerKeyIndex), byExtendingSelection: false)
            providerKeyTableView.scrollRowToVisible(providerKeyIndex)
        } else {
            providerKeyTableView.deselectAll(nil)
        }

        modelTableView.reloadData()
        if let modelIndex {
            modelTableView.selectRowIndexes(IndexSet(integer: modelIndex), byExtendingSelection: false)
            modelTableView.scrollRowToVisible(modelIndex)
        } else {
            modelTableView.deselectAll(nil)
            scrollTableToTop(modelTableView)
        }

        reloadRouteTable(preserving: routeIdentity)
    }

    func showProvider(at providerIndex: Int, preservingModelSource: Bool = false) {
        guard providerIndex >= 0, providerIndex < providers.count else { return }
        commitEditor()
        if !preservingModelSource {
            providerEditorSourceModel = nil
        }
        isRenderingSelection = true
        selectProvider(at: providerIndex)
        isRenderingSelection = false
        renderProviderSelection()
    }

    func showModel(providerIndex: Int, modelIndex: Int) {
        guard providerIndex >= 0,
              providerIndex < providers.count,
              modelIndex >= 0,
              modelIndex < providers[providerIndex].models.count else { return }
        commitEditor()
        providerEditorSourceModel = nil
        isRenderingSelection = true
        selectProvider(at: providerIndex)
        modelTableView.reloadData()
        modelTableView.selectRowIndexes(IndexSet(integer: modelIndex), byExtendingSelection: false)
        modelTableView.scrollRowToVisible(modelIndex)
        isRenderingSelection = false
        renderModelSelection()
    }

    func showProviderKey(at keyIndex: Int) {
        guard let providerIndex = selectedProviderIndex,
              keyIndex >= 0,
              keyIndex < providers[providerIndex].apiKeys.count else { return }
        commitEditor()
        isRenderingSelection = true
        selectProviderKey(at: keyIndex)
        isRenderingSelection = false
        renderProviderKeySelection()
    }

    func renderRouteSelection() {
        let target = pendingRouteSelectionIdentity ?? selectedRouteIdentity
        defer { pendingRouteSelectionIdentity = nil }
        guard let target,
              let current = modelSelectionIndices(for: target) else {
            refreshRouteControlsEnabled()
            return
        }
        isRenderingSelection = true
        selectRoute(target)
        isRenderingSelection = false
        showModel(providerIndex: current.provider, modelIndex: current.model)
        refreshRouteControlsEnabled()
    }

    func renderProviderSelection() {
        isRenderingSelection = true
        defer { isRenderingSelection = false }
        detailMode = .provider
        modelEditorTarget = nil
        providerNameAutofillProviderID = nil
        let hasProvider = selectedProviderIndex != nil
        deleteProviderButton.isEnabled = hasProvider
        addModelButton.isEnabled = hasProvider
        setProviderFormEnabled(hasProvider)
        providerDetailView?.isHidden = !hasProvider
        modelDetailView?.isHidden = true

        guard let providerIndex = selectedProviderIndex else {
            providerEditorTargetIndex = nil
            providerEditorTargetID = nil
            providerKeyEditorTarget = nil
            providerEditorDirty = false
            providerEnabledCheckbox.state = .off
            providerNameField.stringValue = ""
            providerApiBaseField.stringValue = ""
            providerKeyNameField.stringValue = ""
            providerApiKeyField.stringValue = ""
            providerEditorSourceModel = nil
            renderProviderEditorHeader()
            providerKeyTableView.reloadData()
            refreshModelCandidateApiKeyPopup(providerIndex: nil)
            clearModelForm()
            return
        }

        ensureProviderHasKey(providerIndex)
        providerEditorTargetIndex = providerIndex
        let provider = providers[providerIndex]
        if let source = providerEditorSourceModel,
           (source.provider != providerIndex || modelSelectionIndices(for: source) == nil) {
            providerEditorSourceModel = nil
        }
        providerEditorTargetID = provider.editorID
        providerEnabledCheckbox.state = provider.enabled ? .on : .off
        providerNameField.stringValue = provider.name
        providerApiBaseField.stringValue = provider.apiBase
        providerKeyTableView.reloadData()
        if providers[providerIndex].apiKeys.isEmpty {
            renderProviderKeySelection()
        } else if selectedProviderKeyIndex == nil {
            selectProviderKey(at: 0)
            renderProviderKeySelection()
        } else {
            renderProviderKeySelection()
        }
        refreshModelCandidateApiKeyPopup(providerIndex: providerIndex)

        modelTableView.reloadData()
        scrollTableToTop(modelTableView)
        modelTableView.deselectAll(nil)
        reloadRouteTable()
        clearModelForm()
        providerEditorDirty = false
        renderProviderEditorHeader()
    }

    func renderProviderKeySelection() {
        detailMode = .provider
        modelEditorTarget = nil
        let hasKey = selectedProviderKeyIndex != nil
        deleteProviderKeyButton.isEnabled = hasKey && (selectedProviderIndex.map { providers[$0].apiKeys.count > 1 } ?? false)
        providerKeyNameField.isEnabled = hasKey
        providerApiKeyField.isEnabled = hasKey

        guard let providerIndex = selectedProviderIndex,
              let keyIndex = selectedProviderKeyIndex else {
            providerKeyEditorTarget = nil
            providerEditorDirty = false
            providerKeyNameField.stringValue = ""
            providerApiKeyField.stringValue = ""
            return
        }

        providerEditorTargetIndex = providerIndex
        providerEditorTargetID = providers[providerIndex].editorID
        providerKeyEditorTarget = (
            providerIndex,
            providers[providerIndex].editorID,
            keyIndex,
            providers[providerIndex].apiKeys[keyIndex].editorID
        )
        providerKeyNameField.stringValue = providers[providerIndex].apiKeys[keyIndex].name
        providerApiKeyField.stringValue = providers[providerIndex].apiKeys[keyIndex].value
        providerEditorDirty = false
    }

    func renderModelSelection() {
        isRenderingSelection = true
        defer { isRenderingSelection = false }
        detailMode = .model
        providerEditorTargetIndex = nil
        providerEditorTargetID = nil
        providerKeyEditorTarget = nil
        providerNameAutofillProviderID = nil
        providerEditorSourceModel = nil
        providerEditorDirty = false
        let hasModel = selectedModelIndex != nil
        selectedModelImageGenerationEndpointDisabled = false
        selectedModelInfoInFlight = false
        duplicateModelButton.isEnabled = hasModel
        deleteModelButton.isEnabled = hasModel
        setModelFormEnabled(hasModel)
        enabledCheckbox.isEnabled = hasModel
        providerDetailView?.isHidden = true
        modelDetailView?.isHidden = !hasModel

        guard let providerIndex = selectedProviderIndex,
              let modelIndex = selectedModelIndex else {
            modelEditorTarget = nil
            clearModelForm()
            return
        }

        modelEditorTarget = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: modelIndex)
        let model = providers[providerIndex].models[modelIndex]
        renderModelBreadcrumb(providerIndex: providerIndex, modelIndex: modelIndex)
        refreshModelBillingDetail(provider: providers[providerIndex], model: model)
        enabledCheckbox.state = model.modelEnabled ? .on : .off
        modelNameField.stringValue = model.modelName
        refreshModelProviderPopup(providerIndex: providerIndex)
        refreshModelApiKeyPopup(providerIndex: providerIndex, selected: model.apiKeyName)
        upstreamModelField.stringValue = modelUpstreamPart(model.litellmModel)
        let order = model.order.trimmingCharacters(in: .whitespacesAndNewlines)
        orderField.stringValue = order.isEmpty ? "1" : order
        loadUpstreamApiModeOrder(for: model)
        selectedModelImageGenerationEndpointDisabled = modelIsImageGenerationEndpointModel(model)
        refreshResponsesEndpointSupportControls()
        if viewMode == .routes {
            selectRoute(providerIndex: providerIndex, modelIndex: modelIndex)
        }
        refreshSelectedModelInfoState(providerIndex: providerIndex, modelIndex: modelIndex)
        refreshRouteControlsEnabled()
    }

    func clearModelForm() {
        selectedModelImageGenerationEndpointDisabled = false
        selectedModelInfoInFlight = false
        selectedModelInfoRequestGeneration += 1
        modelEditorTarget = nil
        duplicateModelButton.isEnabled = false
        deleteModelButton.isEnabled = false
        setModelFormEnabled(false)
        enabledCheckbox.state = .off
        for field in modelFields {
            field.stringValue = ""
        }
        modelBreadcrumbProviderButton.isEnabled = false
        modelBreadcrumbProviderButton.setNavigationTitle("")
        modelBreadcrumbModelLabel.stringValue = ""
        modelProviderPopupButton.removeAllItems()
        modelProviderPopupButton.isEnabled = false
        modelApiKeyPopupButton.removeAllItems()
        modelApiKeyPopupButton.isEnabled = false
        modelBillingStatusLabel.stringValue = ""
        modelBillingStatusLabel.toolTip = nil
        modelUsageStatusLabel.stringValue = ""
        modelUsageStatusLabel.toolTip = nil
        modelMultiplierStatusLabel.stringValue = ""
        modelMultiplierStatusLabel.toolTip = nil
        loadUpstreamApiModeOrder([defaultUpstreamApiMode])
        refreshResponsesEndpointSupportControls()
        refreshRouteControlsEnabled()
        refreshModelCandidateControlsEnabled()
        refreshModelAvailabilityProbeControlsEnabled()
        refreshResponsesEndpointProbeControlsEnabled()
    }

    var providerFields: [NSTextField] {
        [providerNameField, providerApiBaseField, providerKeyNameField, providerApiKeyField]
    }

    var modelFields: [NSTextField] {
        [modelNameField, upstreamModelField, orderField]
    }

    func renderModelBreadcrumb(providerIndex: Int, modelIndex: Int) {
        guard providers.indices.contains(providerIndex),
              providers[providerIndex].models.indices.contains(modelIndex) else {
            modelBreadcrumbProviderButton.setNavigationTitle("")
            modelBreadcrumbProviderButton.isEnabled = false
            modelBreadcrumbModelLabel.stringValue = ""
            return
        }
        let provider = providers[providerIndex]
        let model = provider.models[modelIndex]
        let providerName = provider.displayName
        modelBreadcrumbProviderButton.setNavigationTitle(providerName)
        modelBreadcrumbProviderButton.isEnabled = true
        modelBreadcrumbProviderButton.toolTip = "Edit provider \(providerName)"
        modelBreadcrumbProviderButton.setAccessibilityLabel("Edit provider \(providerName)")
        modelBreadcrumbModelLabel.stringValue = routePublicModelName(model)
    }

    func renderProviderEditorHeader() {
        guard let providerIndex = selectedProviderIndex,
              providers.indices.contains(providerIndex) else {
            providerEditorTitleLabel.stringValue = "Provider"
            providerReturnToModelButton.isHidden = true
            providerReturnToModelButton.isEnabled = false
            providerReturnToModelButton.setNavigationTitle("")
            return
        }

        providerEditorTitleLabel.stringValue = "Provider: \(providers[providerIndex].displayName)"
        guard let source = providerEditorSourceModel,
              source.provider == providerIndex,
              let current = modelSelectionIndices(for: source) else {
            providerReturnToModelButton.isHidden = true
            providerReturnToModelButton.isEnabled = false
            providerReturnToModelButton.setNavigationTitle("")
            return
        }

        let modelName = routePublicModelName(providers[current.provider].models[current.model])
        providerReturnToModelButton.isHidden = false
        providerReturnToModelButton.isEnabled = true
        providerReturnToModelButton.setNavigationTitle("Back to model \(modelName)")
        providerReturnToModelButton.toolTip = "Back to model \(modelName)"
        providerReturnToModelButton.setAccessibilityLabel("Back to model \(modelName)")
    }

    func isProviderField(_ field: NSTextField) -> Bool {
        providerFields.contains { $0 === field }
    }

    func markProviderEditorDirty(for sender: Any?) {
        if let field = sender as? NSTextField, isProviderField(field) {
            providerEditorDirty = true
        }
    }

    func setProviderFormEnabled(_ enabled: Bool) {
        providerEnabledCheckbox.isEnabled = enabled
        for field in providerFields {
            field.isEnabled = enabled
        }
    }

    func setModelFormEnabled(_ enabled: Bool) {
        enabledCheckbox.isEnabled = enabled
        modelProviderPopupButton.isEnabled = enabled && providers.count > 1
        modelApiKeyPopupButton.isEnabled = enabled
        supportsOpenAIChatCheckbox.isEnabled = enabled
        supportsOpenAIResponsesCheckbox.isEnabled = enabled
        supportsAnthropicCheckbox.isEnabled = enabled
        for field in modelFields {
            field.isEnabled = enabled
        }
        refreshModelCandidateControlsEnabled()
        refreshModelAvailabilityProbeControlsEnabled()
        refreshResponsesEndpointSupportControls()
    }

    func modelEffectiveAPIBase(providerIndex: Int, model: EditableModel) -> String {
        let modelBaseURL = model.apiBase.trimmingCharacters(in: .whitespacesAndNewlines)
        if !modelBaseURL.isEmpty {
            return modelBaseURL
        }
        guard providerIndex >= 0, providerIndex < providers.count else {
            return ""
        }
        return providers[providerIndex].apiBase.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func modelIsImageGenerationEndpointModel(_ model: EditableModel) -> Bool {
        guard case .string(let mode)? = model.modelInfoExtra["mode"] else {
            return false
        }
        return mode.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "image_generation"
    }

    func normalizedUpstreamApiMode(_ value: String) -> String {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return upstreamApiModes.contains(text) ? text : defaultUpstreamApiMode
    }

    func normalizedUpstreamApiMode(for model: EditableModel) -> String {
        effectiveUpstreamApiMode(
            from: normalizedSupportedUpstreamApiModes(for: model),
            fallback: model.upstreamApiMode
        )
    }
}
