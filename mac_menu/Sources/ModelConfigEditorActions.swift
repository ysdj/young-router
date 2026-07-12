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
            providers[providerIndex].apiKeys[keyIndex].enabled = true
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
        let supportedApiModes = selectedSupportedUpstreamApiModes()
        providers[providerIndex].models[modelIndex].litellmModel = composedLiteLLMModel(
            upstreamModel: upstreamModelField.stringValue,
            upstreamApiMode: supportedApiModes[0]
        )
        let order = orderField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        providers[providerIndex].models[modelIndex].order = order.isEmpty ? "1" : order
        orderField.stringValue = providers[providerIndex].models[modelIndex].order
        providers[providerIndex].models[modelIndex].sslVerify = ""
        providers[providerIndex].models[modelIndex].sslVerifyPresent = false
        providers[providerIndex].models[modelIndex].upstreamApiMode = supportedApiModes[0]
        providers[providerIndex].models[modelIndex].supportedUpstreamApiModes = supportedApiModes
        persistDisplayedUpstreamApiModeOrder(providerIndex: providerIndex, modelIndex: modelIndex)
        markPendingChangesIfNeeded(providers[providerIndex].models[modelIndex] != originalModel)
        if selectedProviderIndex == providerIndex {
            modelTableView.reloadData(forRowIndexes: IndexSet(integer: modelIndex), columnIndexes: IndexSet(integersIn: 0..<modelTableView.numberOfColumns))
        }
        modelEditorTarget = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: modelIndex)
        reloadRouteTable(preserving: modelEditorTarget)
        refreshRuntimeMap()
    }

    func modelUpstreamPart(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        let legacyResponsesPrefix = "openai/responses/"
        if trimmed.hasPrefix(legacyResponsesPrefix) {
            return String(trimmed.dropFirst(legacyResponsesPrefix.count))
        }
        guard let slashIndex = trimmed.firstIndex(of: "/") else {
            return trimmed
        }
        let upstreamStart = trimmed.index(after: slashIndex)
        return String(trimmed[upstreamStart...])
    }

    func composedLiteLLMModel(upstreamModel: String, upstreamApiMode: String) -> String {
        let upstream = upstreamModel.trimmingCharacters(in: .whitespacesAndNewlines)
        let adapter = adapterName(forUpstreamApiMode: upstreamApiMode)
        if upstream.isEmpty {
            return "\(adapter)/"
        }
        return "\(adapter)/\(upstream)"
    }

    func adapterName(forUpstreamApiMode mode: String) -> String {
        normalizedUpstreamApiMode(mode) == "anthropic" ? "anthropic" : "openai"
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
                effectiveProviders[providerIndex].models[modelIndex].enabled =
                    effectiveProviders[providerIndex].enabled
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
