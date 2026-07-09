import Cocoa

extension ModelConfigEditorController {
    func uniqueProviderKeyName(providerIndex: Int, preferred: String) -> String {
        let base = preferred.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? defaultProviderKeyName : preferred.trimmingCharacters(in: .whitespacesAndNewlines)
        let used = Set(providers[providerIndex].apiKeys.map { $0.name.trimmingCharacters(in: .whitespacesAndNewlines) })
        if !used.contains(base) {
            return base
        }
        var suffix = 2
        while used.contains("\(base)-\(suffix)") {
            suffix += 1
        }
        return "\(base)-\(suffix)"
    }

    func commitEditor() {
        switch detailMode {
        case .provider:
            commitProviderEditor()
        case .model:
            commitModelEditor()
        case .none:
            break
        }
    }

    @objc func editorViewModeChanged(_ sender: NSSegmentedControl) {
        commitEditor()
        viewMode = sender.selectedSegment == 1 ? .routes : .providers
        applyEditorViewMode()
    }

    @objc func providerTableClicked(_ sender: NSTableView) {
        let row = sender.clickedRow >= 0 ? sender.clickedRow : sender.selectedRow
        guard row >= 0, row < providers.count else { return }
        showProvider(at: row)
    }

    @objc func modelTableClicked(_ sender: NSTableView) {
        guard let providerIndex = selectedProviderIndex else { return }
        let row = sender.clickedRow >= 0 ? sender.clickedRow : sender.selectedRow
        guard row >= 0, row < providers[providerIndex].models.count else { return }
        showModel(providerIndex: providerIndex, modelIndex: row)
    }

    @objc func providerKeyTableClicked(_ sender: NSTableView) {
        let row = sender.clickedRow >= 0 ? sender.clickedRow : sender.selectedRow
        guard row >= 0 else { return }
        showProviderKey(at: row)
    }

    @objc func routeTableClicked(_ sender: NSTableView) {
        let rows = routeTableRows()
        let row = sender.clickedRow >= 0 ? sender.clickedRow : sender.selectedRow
        guard row >= 0, row < rows.count else { return }
        switch rows[row] {
        case .deployment(let route):
            showModel(providerIndex: route.providerIndex, modelIndex: route.modelIndex)
            refreshRouteControlsEnabled()
        case .modelGroup(let group):
            guard let firstRouteIndex = firstDeploymentTableRowIndex(inGroup: group.publicModel),
                  let firstRoute = routeDeployment(atTableRow: firstRouteIndex) else {
                refreshRouteControlsEnabled()
                return
            }
            routeTableView.selectRowIndexes(IndexSet(integer: firstRouteIndex), byExtendingSelection: false)
            routeTableView.scrollRowToVisible(firstRouteIndex)
            showModel(providerIndex: firstRoute.providerIndex, modelIndex: firstRoute.modelIndex)
            refreshRouteControlsEnabled()
        }
    }

    @objc func moveRouteUp() {
        moveSelectedRoute(by: -1)
    }

    @objc func moveRouteDown() {
        moveSelectedRoute(by: 1)
    }

    @objc func normalizeRouteOrder() {
        let selectedIdentity = selectedRouteIdentity ?? modelEditorTarget
        commitEditor()
        guard let selectedIdentity,
              let current = modelSelectionIndices(for: selectedIdentity) else {
            refreshRouteControlsEnabled()
            return
        }
        let publicModel = routePublicModelName(providers[current.provider].models[current.model])
        let group = routeGroup(for: publicModel)
        guard !group.isEmpty else {
            refreshRouteControlsEnabled()
            return
        }
        rewriteRouteGroupOrder(group, preserving: selectedIdentity, status: "Normalized \(publicModel) route order.")
    }

    func commitProviderEditor() {
        guard providerEditorDirty else { return }
        guard let providerIndex = providerEditorTargetIndex,
              let providerID = providerEditorTargetID,
              providerIndex >= 0,
              providerIndex < providers.count,
              providers[providerIndex].editorID == providerID else { return }
        let originalProvider = providers[providerIndex]
        let currentCandidateKeyName = selectedModelCandidateKeyName()
        var candidateKeyNameAfterCommit = currentCandidateKeyName
        providers[providerIndex].name = providerNameField.stringValue
        providers[providerIndex].enabled = providerEnabledCheckbox.state == .on
        providers[providerIndex].apiBase = providerApiBaseField.stringValue
        if let target = providerKeyEditorTarget,
           target.provider == providerIndex,
           target.providerID == providerID,
           target.key >= 0,
           target.key < providers[providerIndex].apiKeys.count,
           providers[providerIndex].apiKeys[target.key].editorID == target.keyID {
            let keyIndex = target.key
            let oldName = providers[providerIndex].apiKeys[keyIndex].name
            let newName = providerKeyNameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            providers[providerIndex].apiKeys[keyIndex].enabled = providerKeyEnabledCheckbox.state == .on
            providers[providerIndex].apiKeys[keyIndex].name = newName
            providers[providerIndex].apiKeys[keyIndex].value = providerApiKeyField.stringValue
            if oldName != newName {
                for modelIndex in providers[providerIndex].models.indices where providers[providerIndex].models[modelIndex].apiKeyName == oldName {
                    providers[providerIndex].models[modelIndex].apiKeyName = newName
                }
                if currentCandidateKeyName == oldName {
                    candidateKeyNameAfterCommit = newName
                }
            }
        }
        providers[providerIndex].apiKey = normalizedProviderKeys(providerIndex).first?.value ?? ""
        let changed = providers[providerIndex] != originalProvider
        providerEditorDirty = false
        markPendingChangesIfNeeded(changed)
        providerTableView.reloadData(forRowIndexes: IndexSet(integer: providerIndex), columnIndexes: IndexSet(integersIn: 0..<providerTableView.numberOfColumns))
        providerKeyTableView.reloadData()
        modelTableView.reloadData()
        reloadRouteTable(preserving: modelEditorTarget)
        scrollTableToTop(providerKeyTableView)
        if let keyIndex = providerKeyEditorTarget?.key,
           keyIndex >= 0,
           keyIndex < providers[providerIndex].apiKeys.count,
           providerKeyEditorTarget?.providerID == providerID,
           providerKeyEditorTarget?.keyID == providers[providerIndex].apiKeys[keyIndex].editorID {
            providerKeyTableView.selectRowIndexes(IndexSet(integer: keyIndex), byExtendingSelection: false)
        }
        refreshModelCandidateApiKeyPopup(providerIndex: providerIndex, selected: candidateKeyNameAfterCommit)
        refreshRuntimeMap()
    }

    func commitModelEditor() {
        guard let target = modelEditorTarget else { return }
        guard let current = modelSelectionIndices(for: target) else {
            modelEditorTarget = nil
            return
        }
        let providerIndex = current.provider
        let modelIndex = current.model
        let originalModel = providers[providerIndex].models[modelIndex]
        providers[providerIndex].models[modelIndex].modelEnabled = enabledCheckbox.state == .on
        providers[providerIndex].models[modelIndex].enabled = modelEffectivelyEnabled(providerIndex: providerIndex, model: providers[providerIndex].models[modelIndex])
        providers[providerIndex].models[modelIndex].modelName = modelNameField.stringValue
        providers[providerIndex].models[modelIndex].apiKeyName = modelApiKeyPopupButton.titleOfSelectedItem ?? ""
        if let key = normalizedProviderKeys(providerIndex).first(where: { $0.name == providers[providerIndex].models[modelIndex].apiKeyName }) {
            providers[providerIndex].models[modelIndex].apiKey = key.value
        }
        providers[providerIndex].models[modelIndex].litellmModel = composedLiteLLMModel()
        let order = orderField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        providers[providerIndex].models[modelIndex].order = order.isEmpty ? "1" : order
        orderField.stringValue = providers[providerIndex].models[modelIndex].order
        providers[providerIndex].models[modelIndex].sslVerify = ""
        providers[providerIndex].models[modelIndex].sslVerifyPresent = false
        let supportedApiModes = selectedSupportedUpstreamApiModes()
        let originalSupportedApiModes = normalizedSupportedUpstreamApiModes(for: originalModel)
        let apiModesChanged = Set(normalizedUpstreamApiModes(supportedApiModes)) != Set(originalSupportedApiModes)
        if apiModesChanged {
            providers[providerIndex].models[modelIndex].upstreamApiMode = effectiveUpstreamApiMode(
                from: supportedApiModes,
                fallback: originalModel.upstreamApiMode
            )
            providers[providerIndex].models[modelIndex].upstreamApiModePresent = true
            providers[providerIndex].models[modelIndex].supportedUpstreamApiModes = supportedApiModes
            providers[providerIndex].models[modelIndex].supportedUpstreamApiModesPresent = true
            providers[providerIndex].models[modelIndex].supportsResponsesEndpoint = supportedApiModes.contains("openai/responses")
            providers[providerIndex].models[modelIndex].supportsResponsesEndpointPresent = false
        } else {
            providers[providerIndex].models[modelIndex].upstreamApiMode = originalModel.upstreamApiMode
            providers[providerIndex].models[modelIndex].upstreamApiModePresent = originalModel.upstreamApiModePresent
            providers[providerIndex].models[modelIndex].supportedUpstreamApiModes = originalModel.supportedUpstreamApiModes
            providers[providerIndex].models[modelIndex].supportedUpstreamApiModesPresent = originalModel.supportedUpstreamApiModesPresent
            providers[providerIndex].models[modelIndex].supportsResponsesEndpoint = originalModel.supportsResponsesEndpoint
            providers[providerIndex].models[modelIndex].supportsResponsesEndpointPresent = originalModel.supportsResponsesEndpointPresent
        }
        markPendingChangesIfNeeded(providers[providerIndex].models[modelIndex] != originalModel)
        if selectedProviderIndex == providerIndex {
            modelTableView.reloadData(forRowIndexes: IndexSet(integer: modelIndex), columnIndexes: IndexSet(integersIn: 0..<modelTableView.numberOfColumns))
        }
        modelEditorTarget = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: modelIndex)
        reloadRouteTable(preserving: modelEditorTarget)
        refreshRuntimeMap()
    }

    var selectedAdapterIsCustom: Bool {
        adapterPopupButton.titleOfSelectedItem == customAdapterTitle
    }

    func splitLiteLLMModel(_ value: String) -> (String, String) {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let slashIndex = trimmed.firstIndex(of: "/") else {
            return ("", trimmed)
        }
        let adapter = String(trimmed[..<slashIndex])
        let upstreamStart = trimmed.index(after: slashIndex)
        return (adapter, String(trimmed[upstreamStart...]))
    }

    func modelUpstreamPart(_ value: String) -> String {
        splitLiteLLMModel(value).1
    }

    func setAdapterControls(from value: String) {
        let split = splitLiteLLMModel(value)
        if adapterOptions.contains(split.0) {
            adapterPopupButton.selectItem(withTitle: split.0)
            customAdapterField.stringValue = ""
            customAdapterField.isHidden = true
            customAdapterField.isEnabled = false
        } else {
            adapterPopupButton.selectItem(withTitle: customAdapterTitle)
            customAdapterField.stringValue = split.0
            customAdapterField.isHidden = false
            customAdapterField.isEnabled = enabledCheckbox.isEnabled
        }
    }

    func composedLiteLLMModel() -> String {
        let upstream = upstreamModelField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let adapter = (selectedAdapterIsCustom ? customAdapterField.stringValue : (adapterPopupButton.titleOfSelectedItem ?? ""))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if adapter.isEmpty {
            return upstream
        }
        if upstream.isEmpty {
            return "\(adapter)/"
        }
        return "\(adapter)/\(upstream)"
    }

    func adapterName(forUpstreamApiMode mode: String) -> String {
        normalizedUpstreamApiMode(mode) == "anthropic" ? "anthropic" : "openai"
    }

    func litellmModel(_ value: String, settingAdapterFor mode: String) -> String {
        let upstream = modelUpstreamPart(value).trimmingCharacters(in: .whitespacesAndNewlines)
        let adapter = adapterName(forUpstreamApiMode: mode)
        if upstream.isEmpty {
            return "\(adapter)/"
        }
        return "\(adapter)/\(upstream)"
    }

    func applyAdapterControls(forUpstreamApiMode mode: String) {
        let adapter = adapterName(forUpstreamApiMode: mode)
        adapterPopupButton.selectItem(withTitle: adapter)
        customAdapterField.stringValue = ""
        customAdapterField.isHidden = true
        customAdapterField.isEnabled = false
    }

    func validatedProvidersForSave() throws -> [EditableProvider] {
        commitEditor()
        var effectiveProviders = providers.filter { !$0.isBlank }
        var seenProviders: Set<String> = []
        var modelNumber = 0

        for providerIndex in effectiveProviders.indices {
            effectiveProviders[providerIndex].models = effectiveProviders[providerIndex].models.filter { !$0.isBlank }
            let providerName = effectiveProviders[providerIndex].name.trimmingCharacters(in: .whitespacesAndNewlines)
            if providerName.isEmpty {
                throw ConfigEditorError(message: "Every provider needs a name.")
            }
            if seenProviders.contains(providerName) {
                throw ConfigEditorError(message: "Duplicate provider name: \(providerName)")
            }
            seenProviders.insert(providerName)

            effectiveProviders[providerIndex].apiKeys = effectiveProviders[providerIndex].apiKeys.filter { !$0.isBlank }
            if effectiveProviders[providerIndex].apiKeys.isEmpty,
               !effectiveProviders[providerIndex].apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                effectiveProviders[providerIndex].apiKeys = [
                    EditableProviderKey(name: defaultProviderKeyName, value: effectiveProviders[providerIndex].apiKey, enabled: true)
                ]
            }
            if effectiveProviders[providerIndex].apiKeys.isEmpty {
                throw ConfigEditorError(message: "Provider \(providerName) needs at least one API key.")
            }

            var seenKeys: Set<String> = []
            for keyIndex in effectiveProviders[providerIndex].apiKeys.indices {
                let keyName = effectiveProviders[providerIndex].apiKeys[keyIndex].name.trimmingCharacters(in: .whitespacesAndNewlines)
                if keyName.isEmpty {
                    throw ConfigEditorError(message: "Provider \(providerName) has an API key without a label.")
                }
                if seenKeys.contains(keyName) {
                    throw ConfigEditorError(message: "Provider \(providerName) has duplicate API key label: \(keyName)")
                }
                if effectiveProviders[providerIndex].apiKeys[keyIndex].value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    throw ConfigEditorError(message: "Provider \(providerName) key \(keyName) needs a token.")
                }
                seenKeys.insert(keyName)
            }

            effectiveProviders[providerIndex].apiKey = effectiveProviders[providerIndex].apiKeys.first?.value ?? ""
            let firstKeyName = effectiveProviders[providerIndex].apiKeys.first?.name ?? defaultProviderKeyName
            for modelIndex in effectiveProviders[providerIndex].models.indices {
                modelNumber += 1
                let model = effectiveProviders[providerIndex].models[modelIndex]
                if model.modelName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    throw ConfigEditorError(message: "Model #\(modelNumber) needs a model name.")
                }
                if model.litellmModel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    throw ConfigEditorError(message: "Model #\(modelNumber) needs a provider model.")
                }
                if model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || !seenKeys.contains(model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines)) {
                    effectiveProviders[providerIndex].models[modelIndex].apiKeyName = firstKeyName
                    effectiveProviders[providerIndex].models[modelIndex].apiKey = effectiveProviders[providerIndex].apiKeys.first?.value ?? ""
                }
                let keyName = effectiveProviders[providerIndex].models[modelIndex].apiKeyName
                let keyEnabled = effectiveProviders[providerIndex].apiKeys.first { $0.name == keyName }?.enabled ?? true
                effectiveProviders[providerIndex].models[modelIndex].enabled =
                    effectiveProviders[providerIndex].enabled
                    && keyEnabled
                    && effectiveProviders[providerIndex].models[modelIndex].modelEnabled
            }
        }
        return effectiveProviders
    }

    @objc func textFieldAction(_ sender: NSTextField) {
        if isRenderingSelection {
            return
        }
        markProviderEditorDirty(for: sender)
        commitEditor()
    }
}
