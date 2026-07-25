import Cocoa
import UniformTypeIdentifiers

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

    @objc func modelProviderSelectionChanged(_ sender: NSPopUpButton) {
        commitModelEditor()
        guard let sourceIdentity = modelEditorTarget,
              let destinationProviderID = sender.selectedItem?.representedObject as? UUID,
              let source = modelSelectionIndices(for: sourceIdentity),
              let destinationProviderIndex = providers.firstIndex(where: { $0.editorID == destinationProviderID }),
              source.provider != destinationProviderIndex,
              providers.indices.contains(source.provider),
              providers[source.provider].models.indices.contains(source.model) else {
            return
        }

        let sourceProviderIndex = source.provider
        let sourceModelIndex = source.model
        let sourceModel = providers[sourceProviderIndex].models[sourceModelIndex]
        // A probe result belongs to the old provider/model pair. Once the
        // deployment is moved, that result is no longer meaningful.
        invalidateModelProbePresentation(providerIndex: sourceProviderIndex, modelIndex: sourceModelIndex)
        ensureProviderHasKey(destinationProviderIndex)
        let destinationProviderName = providers[destinationProviderIndex].name
        let destinationKey = normalizedProviderKeys(destinationProviderIndex).first

        var movedModel = sourceModel
        movedModel.provider = destinationProviderName
        movedModel.apiBase = ""
        movedModel.apiKeyName = destinationKey?.name ?? ""
        movedModel.apiKey = destinationKey?.value ?? ""
        movedModel.enabled = movedModel.modelEnabled
        providers[sourceProviderIndex].models.remove(at: sourceModelIndex)
        providers[destinationProviderIndex].models.append(movedModel)

        selectedModelInfoRequestGeneration += 1
        markPendingChanges()
        let destinationIdentity = modelSelectionIdentity(
            providerIndex: destinationProviderIndex,
            modelIndex: providers[destinationProviderIndex].models.count - 1
        )
        modelEditorTarget = nil
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  let destinationIdentity,
                  let destination = self.modelSelectionIndices(for: destinationIdentity) else {
                return
            }
            self.reloadRouteTable(preserving: destinationIdentity)
            self.showModel(providerIndex: destination.provider, modelIndex: destination.model)
            self.setEditorStatus("Moved deployment to \(self.providers[destinationProviderIndex].displayName).")
        }
    }

    @objc func modelBreadcrumbProviderClicked(_ sender: NSButton) {
        guard let target = modelEditorTarget,
              let current = modelSelectionIndices(for: target) else { return }
        commitModelEditor()
        providerEditorSourceModel = target
        showProvider(at: current.provider, preservingModelSource: true)
    }

    @objc func providerReturnToModelClicked(_ sender: NSButton) {
        guard let source = providerEditorSourceModel,
              let current = modelSelectionIndices(for: source) else {
            providerEditorSourceModel = nil
            renderProviderEditorHeader()
            return
        }
        commitProviderEditor()
        showModel(providerIndex: current.provider, modelIndex: current.model)
    }

    @objc func moveRouteUp() {
        moveSelectedRoute(by: -1)
    }

    @objc func moveRouteDown() {
        moveSelectedRoute(by: 1)
    }

    @objc func importSourceSelected(_ sender: NSPopUpButton) {
        defer { sender.selectItem(at: 0) }
        switch sender.selectedTag() {
        case 1:
            importExternalProvidersFromSource(arguments: ["codex-current"])
        case 2:
            let panel = NSOpenPanel()
            panel.title = "Import External Provider Configuration"
            panel.message = "Choose a Codex, OpenAI-compatible, CLIProxyAPI, CC-Switch, or New API configuration file."
            panel.canChooseDirectories = false
            panel.canChooseFiles = true
            panel.allowsMultipleSelection = false
            panel.allowedContentTypes = [
                .json,
                .init(filenameExtension: "toml")!,
                .init(filenameExtension: "yaml")!,
                .init(filenameExtension: "yml")!,
                .init(filenameExtension: "sql")!,
            ]
            guard panel.runModal() == .OK, let url = panel.url else { return }
            importExternalProvidersFromSource(arguments: ["--input", url.path])
        case 3:
            importExternalProvidersFromLink()
        default:
            return
        }
    }

    func importExternalProvidersFromLink() {
        let alert = NSAlert()
        alert.messageText = "Paste Provider Import Link"
        alert.informativeText = "Paste a CC Switch or New API provider link. The embedded API key is masked and the imported values remain a draft until Apply."
        alert.alertStyle = .informational

        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 460, height: 24))
        field.placeholderString = "ccswitch://v1/import?..."
        alert.accessoryView = field
        alert.addButton(withTitle: "Import")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let link = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !link.isEmpty, let input = link.data(using: .utf8) else {
            showAlert(title: "Import providers failed", message: "Paste one CC Switch or New API provider link.")
            return
        }
        importExternalProvidersFromSource(arguments: ["--link-stdin"], standardInput: input)
    }

    func importExternalProvidersFromSource(arguments: [String], standardInput: Data? = nil) {
        guard !externalImportInFlight else { return }
        setExternalImportInFlight(true)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result: Result<ExternalProviderImportPayload, Error>
            do {
                result = .success(
                    try self.importExternalProviders(
                        arguments: arguments,
                        standardInput: standardInput
                    )
                )
            } catch {
                result = .failure(error)
            }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.setExternalImportInFlight(false)
                switch result {
                case .success(let payload):
                    self.mergeImportedProviders(payload.providers)
                    self.setEditorStatus(
                        "Imported \(payload.summary.providers) provider(s) and \(payload.summary.models) model(s).",
                        tooltip: "Source: \(payload.source)\nApply to save these changes."
                    )
                case .failure(let error):
                    self.showAlert(title: "Import providers failed", message: error.localizedDescription)
                }
            }
        }
    }

    func setExternalImportInFlight(_ inFlight: Bool) {
        externalImportInFlight = inFlight
        importSourcePopupButton.isEnabled = !inFlight
        applyButton.isEnabled = !inFlight && !runtimeApplyInFlight && hasPendingChanges
        if inFlight {
            setEditorStatus("Importing providers and models...")
        }
    }

    func adoptImportedConfiguration(
        providers nextProviders: [EditableProvider],
        document: ConfigEditorDocument,
        sourceDescription: String
    ) {
        configurationLoadGeneration += 1
        configurationLoadInFlight = false
        providers = nextProviders
        sourceDocument = document
        modelAvailabilityProbeRuns.removeAll()
        modelProbePresentations.removeAll()
        displayedModelProbePresentationKey = nil
        selectedModelInfoRequestGeneration += 1
        providerEditorDirty = false
        modelEditorTarget = nil
        providerEditorSourceModel = nil
        reloadImportedProviderDraft()
        markPendingChanges()
        setEditorStatus("Imported configuration draft.", tooltip: "Source: \(sourceDescription)\nApply to replace the saved configuration.")
    }

    func mergeImportedProviders(_ incoming: [EditableProvider]) {
        commitEditor()
        var merged = providers
        let existingNames = Set(merged.map { $0.name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() })
        var nextNames = existingNames
        for var provider in incoming where !provider.isBlank {
            provider.name = uniqueImportedProviderName(provider.name, used: &nextNames)
            provider.editorID = UUID()
            provider.apiKeys = provider.apiKeys.map { key in
                var next = key
                next.editorID = UUID()
                return next
            }
            provider.models = provider.models.map { model in
                var next = model
                next.editorID = UUID()
                next.provider = provider.name
                return next
            }
            merged.append(provider)
        }
        guard merged.count != providers.count else {
            showAlert(title: "Nothing imported", message: "The selected source has no usable provider and model entries.")
            return
        }
        providers = merged
        reloadImportedProviderDraft()
        markPendingChanges()
    }

    func uniqueImportedProviderName(_ proposed: String, used: inout Set<String>) -> String {
        let trimmed = proposed.trimmingCharacters(in: .whitespacesAndNewlines)
        let base = trimmed.isEmpty ? "Imported provider" : trimmed
        var candidate = base
        var suffix = 2
        while used.contains(candidate.lowercased()) {
            candidate = "\(base) \(suffix)"
            suffix += 1
        }
        used.insert(candidate.lowercased())
        return candidate
    }

    func suggestedProviderName(fromBaseURL value: String) -> String? {
        let host = apiBaseHost(value)
        guard !host.isEmpty else { return nil }

        let labels = host.split(separator: ".").map(String.init)
        guard !labels.isEmpty else { return nil }
        guard labels.count > 1 else { return labels[0] }

        let genericSecondLevelDomains: Set<String> = ["ac", "co", "com", "edu", "gov", "net", "org"]
        let secondLevelDomain = labels[labels.count - 2]
        if labels.count > 2,
           host.split(separator: ".").last?.count == 2,
           genericSecondLevelDomains.contains(secondLevelDomain) {
            return labels[labels.count - 3]
        }
        return secondLevelDomain
    }

    func uniqueProviderName(for suggested: String, excluding providerIndex: Int) -> String {
        let base = suggested.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !base.isEmpty else { return "" }
        let used = Set(providers.enumerated().compactMap { index, provider in
            index == providerIndex
                ? nil
                : provider.name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        })
        if !used.contains(base.lowercased()) {
            return base
        }

        var suffix = 2
        var candidate = "\(base) (\(suffix))"
        while used.contains(candidate.lowercased()) {
            suffix += 1
            candidate = "\(base) (\(suffix))"
        }
        return candidate
    }

    func autofillProviderNameFromBaseURL() {
        guard let providerIndex = providerEditorTargetIndex,
              let providerID = providerEditorTargetID,
              providerIndex >= 0,
              providerIndex < providers.count,
              (providerNameField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                || providerNameAutofillProviderID == providerID),
              let suggested = suggestedProviderName(fromBaseURL: providerApiBaseField.stringValue) else {
            return
        }
        let name = uniqueProviderName(for: suggested, excluding: providerIndex)
        guard providerNameField.stringValue != name else { return }
        providerNameField.stringValue = name
        providerNameAutofillProviderID = providerID
        providerEditorDirty = true
    }

    func reloadImportedProviderDraft() {
        providerTableView.reloadData()
        modelTableView.reloadData()
        reloadRouteTable()
        if providers.isEmpty {
            renderProviderSelection()
        } else {
            showProvider(at: 0)
        }
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
        let providerName = providerNameField.stringValue
        providers[providerIndex].name = providerName
        if originalProvider.name != providerName {
            for modelIndex in providers[providerIndex].models.indices {
                providers[providerIndex].models[modelIndex].provider = providerName
            }
        }
        providers[providerIndex].enabled = providerEnabledCheckbox.state == .on
        for modelIndex in providers[providerIndex].models.indices {
            providers[providerIndex].models[modelIndex].enabled = providers[providerIndex].models[modelIndex].modelEnabled
        }
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
        if changed {
            invalidateProviderProbePresentations(providerIndex: providerIndex)
        }
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
        renderProviderEditorHeader()
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
        providers[providerIndex].models[modelIndex].enabled = providers[providerIndex].models[modelIndex].modelEnabled
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
        let changed = providers[providerIndex].models[modelIndex] != originalModel
        if changed {
            invalidateModelProbePresentation(providerIndex: providerIndex, modelIndex: modelIndex)
        }
        markPendingChangesIfNeeded(changed)
        if selectedProviderIndex == providerIndex {
            modelTableView.reloadData(forRowIndexes: IndexSet(integer: modelIndex), columnIndexes: IndexSet(integersIn: 0..<modelTableView.numberOfColumns))
        }
        modelEditorTarget = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: modelIndex)
        renderModelBreadcrumb(providerIndex: providerIndex, modelIndex: modelIndex)
        reloadRouteTable(preserving: modelEditorTarget)
    }

    func modelUpstreamPart(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
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
                    EditableProviderKey(name: defaultProviderKeyName, value: effectiveProviders[providerIndex].apiKey)
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
                    throw ConfigEditorError(message: "Provider \(providerName) API key \(keyName) needs a value.")
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
                if parseOrder(model.order) == nil {
                    throw ConfigEditorError(message: "Model #\(modelNumber) needs a numeric order.")
                }
                if model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || !seenKeys.contains(model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines)) {
                    effectiveProviders[providerIndex].models[modelIndex].apiKeyName = firstKeyName
                    effectiveProviders[providerIndex].models[modelIndex].apiKey = effectiveProviders[providerIndex].apiKeys.first?.value ?? ""
                }
                effectiveProviders[providerIndex].models[modelIndex].enabled =
                    effectiveProviders[providerIndex].models[modelIndex].modelEnabled
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
