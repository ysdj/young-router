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

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        if tableView == routeTableView {
            let rows = routeTableRows()
            if row >= 0, row < rows.count {
                if case .modelGroup = rows[row] {
                    return 30
                }
            }
        }
        return 28
    }

    func selectionShouldChange(in tableView: NSTableView) -> Bool {
        if isRenderingSelection {
            return true
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
            text = tableColumn?.identifier == providerCountColumnIdentifier ? "\(provider.models.count)" : provider.displayName
            enabled = provider.enabled
        } else if tableView == modelTableView {
            guard let providerIndex = selectedProviderIndex,
                  row >= 0,
                  row < providers[providerIndex].models.count else { return nil }
            let model = providers[providerIndex].models[row]
            if tableColumn?.identifier == modelUpstreamColumnIdentifier {
                text = modelUpstreamPart(model.litellmModel)
            } else if tableColumn?.identifier == modelRouteColumnIdentifier {
                text = modelRouteSummary(model)
            } else {
                text = model.displayName
            }
            tooltip = modelRouteTooltip(model)
            enabled = modelEffectivelyEnabled(providerIndex: providerIndex, model: model)
        } else if tableView == routeTableView {
            let rows = routeTableRows()
            guard row >= 0, row < rows.count else { return nil }
            switch rows[row] {
            case .modelGroup(let group):
                if tableColumn?.identifier == routeModelColumnIdentifier {
                    text = group.publicModel
                } else if tableColumn?.identifier == routeProviderKeyColumnIdentifier {
                    text = "\(group.routeCount) \(group.routeCount == 1 ? "route" : "routes")"
                } else if tableColumn?.identifier == routeUpstreamColumnIdentifier {
                    text = "RUN \(group.runningCount) / OFF \(group.offCount)"
                } else if tableColumn?.identifier == routeStatusColumnIdentifier {
                    text = group.runningCount > 0 ? "RUN" : "OFF"
                } else {
                    text = ""
                }
                tooltip = routeGroupTooltip(group)
                enabled = group.runningCount > 0
            case .deployment(let route):
                if tableColumn?.identifier == routeOrderColumnIdentifier {
                    text = route.order.map { "\($0)" } ?? "-"
                } else if tableColumn?.identifier == routeProviderKeyColumnIdentifier {
                    text = "  \(route.providerName) / \(route.keyName)"
                } else if tableColumn?.identifier == routeUpstreamColumnIdentifier {
                    text = route.upstreamModel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "(blank upstream)" : route.upstreamModel
                } else if tableColumn?.identifier == routeStatusColumnIdentifier {
                    text = route.enabled ? "RUN" : "OFF"
                } else {
                    text = ""
                }
                tooltip = routeTooltip(route)
                enabled = route.enabled
            }
        } else {
            guard let providerIndex = selectedProviderIndex,
                  row >= 0,
                  row < providers[providerIndex].apiKeys.count else { return nil }
            let key = providers[providerIndex].apiKeys[row]
            text = key.displayName
            enabled = key.enabled
        }
        let label = NSTextField(labelWithString: text)
        label.lineBreakMode = .byTruncatingMiddle
        label.textColor = enabled ? .labelColor : .secondaryLabelColor
        if tableView == routeTableView, routeGroup(atTableRow: row) != nil {
            label.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        }
        label.alignment = tableColumn?.identifier == providerCountColumnIdentifier
            || tableColumn?.identifier == routeOrderColumnIdentifier
            || tableColumn?.identifier == routeStatusColumnIdentifier ? .right : .left
        label.toolTip = tooltip ?? text
        return verticallyCenteredTableCell(label: label)
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
        let row = routeTableView.selectedRow
        guard let route = routeDeployment(atTableRow: row) else { return nil }
        return modelSelectionIdentity(providerIndex: route.providerIndex, modelIndex: route.modelIndex)
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

    func selectRoute(providerIndex: Int, modelIndex: Int) {
        let rows = routeTableRows()
        guard let rowIndex = rows.firstIndex(where: {
            if case .deployment(let route) = $0 {
                return route.providerIndex == providerIndex && route.modelIndex == modelIndex
            }
            return false
        }) else {
            routeTableView.deselectAll(nil)
            return
        }
        routeTableView.selectRowIndexes(IndexSet(integer: rowIndex), byExtendingSelection: false)
        routeTableView.scrollRowToVisible(rowIndex)
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

    func showProvider(at providerIndex: Int) {
        guard providerIndex >= 0, providerIndex < providers.count else { return }
        commitEditor()
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
        guard let route = selectedRouteRow() else {
            refreshRouteControlsEnabled()
            return
        }
        showModel(providerIndex: route.providerIndex, modelIndex: route.modelIndex)
        refreshRouteControlsEnabled()
    }

    func renderProviderSelection() {
        isRenderingSelection = true
        defer { isRenderingSelection = false }
        detailMode = .provider
        modelEditorTarget = nil
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
            providerKeyTableView.reloadData()
            refreshModelCandidateApiKeyPopup(providerIndex: nil)
            clearModelForm()
            return
        }

        ensureProviderHasKey(providerIndex)
        providerEditorTargetIndex = providerIndex
        let provider = providers[providerIndex]
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
        refreshRuntimeMap()
    }

    func renderProviderKeySelection() {
        detailMode = .provider
        modelEditorTarget = nil
        let hasKey = selectedProviderKeyIndex != nil
        deleteProviderKeyButton.isEnabled = hasKey && (selectedProviderIndex.map { providers[$0].apiKeys.count > 1 } ?? false)
        providerKeyEnabledCheckbox.isEnabled = hasKey
        providerKeyNameField.isEnabled = hasKey
        providerApiKeyField.isEnabled = hasKey

        guard let providerIndex = selectedProviderIndex,
              let keyIndex = selectedProviderKeyIndex else {
            providerKeyEditorTarget = nil
            providerEditorDirty = false
            providerKeyEnabledCheckbox.state = .off
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
        providerKeyEnabledCheckbox.state = providers[providerIndex].apiKeys[keyIndex].enabled ? .on : .off
        providerKeyNameField.stringValue = providers[providerIndex].apiKeys[keyIndex].name
        providerApiKeyField.stringValue = providers[providerIndex].apiKeys[keyIndex].value
        providerEditorDirty = false
        refreshRuntimeMap()
    }

    func renderModelSelection() {
        isRenderingSelection = true
        defer { isRenderingSelection = false }
        detailMode = .model
        providerEditorTargetIndex = nil
        providerEditorTargetID = nil
        providerKeyEditorTarget = nil
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
        enabledCheckbox.state = model.modelEnabled ? .on : .off
        modelNameField.stringValue = model.modelName
        refreshModelApiKeyPopup(providerIndex: providerIndex, selected: model.apiKeyName)
        setAdapterControls(from: model.litellmModel)
        upstreamModelField.stringValue = modelUpstreamPart(model.litellmModel)
        let order = model.order.trimmingCharacters(in: .whitespacesAndNewlines)
        orderField.stringValue = order.isEmpty ? "1" : order
        setUpstreamApiSupportCheckboxes(normalizedSupportedUpstreamApiModes(for: model))
        selectedModelImageGenerationEndpointDisabled = modelIsImageGenerationEndpointModel(model)
        refreshResponsesEndpointSupportControls()
        if viewMode == .routes {
            selectRoute(providerIndex: providerIndex, modelIndex: modelIndex)
        }
        refreshSelectedModelInfoState(providerIndex: providerIndex, modelIndex: modelIndex)
        refreshRouteControlsEnabled()
        refreshRuntimeMap()
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
        adapterPopupButton.selectItem(withTitle: "openai")
        modelApiKeyPopupButton.removeAllItems()
        modelApiKeyPopupButton.isEnabled = false
        customAdapterField.isEnabled = false
        customAdapterField.isHidden = true
        setUpstreamApiSupportCheckboxes([defaultUpstreamApiMode])
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
        [modelNameField, customAdapterField, upstreamModelField, orderField]
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
        providerKeyEnabledCheckbox.isEnabled = enabled && selectedProviderKeyIndex != nil
    }

    func setModelFormEnabled(_ enabled: Bool) {
        enabledCheckbox.isEnabled = enabled
        modelApiKeyPopupButton.isEnabled = enabled
        adapterPopupButton.isEnabled = enabled
        supportsOpenAIChatCheckbox.isEnabled = enabled
        supportsOpenAIResponsesCheckbox.isEnabled = enabled
        supportsAnthropicCheckbox.isEnabled = enabled
        for field in modelFields {
            field.isEnabled = enabled
        }
        customAdapterField.isHidden = !selectedAdapterIsCustom
        customAdapterField.isEnabled = enabled && selectedAdapterIsCustom
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
        let aliases: [String: String] = [
            "openai_chat": "openai/chat",
            "openai_chat_completions": "openai/chat",
            "openai-chat": "openai/chat",
            "chat": "openai/chat",
            "chat_completions": "openai/chat",
            "openai_responses": "openai/responses",
            "openai-responses": "openai/responses",
            "responses": "openai/responses",
            "anthropic_messages": "anthropic",
            "anthropic/messages": "anthropic",
            "claude": "anthropic",
        ]
        let normalized = aliases[text] ?? text
        return upstreamApiModes.contains(normalized) ? normalized : defaultUpstreamApiMode
    }

    func normalizedUpstreamApiMode(for model: EditableModel) -> String {
        effectiveUpstreamApiMode(
            from: normalizedSupportedUpstreamApiModes(for: model),
            fallback: model.upstreamApiMode
        )
    }
}
