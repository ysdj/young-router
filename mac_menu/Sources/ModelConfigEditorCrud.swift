import Cocoa

extension ModelConfigEditorController {
    @objc func addProviderKey() {
        commitEditor()
        guard let providerIndex = selectedProviderIndex else { return }
        let name = uniqueProviderKeyName(providerIndex: providerIndex, preferred: defaultProviderKeyName)
        providers[providerIndex].apiKeys.append(EditableProviderKey(name: name, value: "", enabled: true))
        markPendingChanges()
        providerKeyTableView.reloadData()
        reloadRouteTable()
        showProviderKey(at: providers[providerIndex].apiKeys.count - 1)
    }

    @objc func deleteProviderKey() {
        guard let providerIndex = selectedProviderIndex,
              let keyIndex = selectedProviderKeyIndex else { return }
        if providers[providerIndex].apiKeys.count <= 1 {
            return
        }

        let providerID = providers[providerIndex].editorID
        let key = providers[providerIndex].apiKeys[keyIndex]
        let keyID = key.editorID
        let replacement = providers[providerIndex].apiKeys.enumerated().first { $0.offset != keyIndex }?.element.name ?? defaultProviderKeyName
        let alert = NSAlert()
        alert.messageText = "Delete provider API key?"
        alert.informativeText = "Models using \(key.displayName) will be moved to \(replacement)."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Delete")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        guard let currentProviderIndex = providers.firstIndex(where: { $0.editorID == providerID }),
              let currentKeyIndex = providers[currentProviderIndex].apiKeys.firstIndex(where: { $0.editorID == keyID }),
              providers[currentProviderIndex].apiKeys.count > 1 else { return }

        let currentKey = providers[currentProviderIndex].apiKeys[currentKeyIndex]
        let currentReplacement = providers[currentProviderIndex].apiKeys.enumerated().first { $0.offset != currentKeyIndex }?.element.name ?? defaultProviderKeyName
        providerKeyEditorTarget = nil
        providerEditorDirty = false

        providers[currentProviderIndex].apiKeys.remove(at: currentKeyIndex)
        markPendingChanges()
        for modelIndex in providers[currentProviderIndex].models.indices where providers[currentProviderIndex].models[modelIndex].apiKeyName == currentKey.name {
            providers[currentProviderIndex].models[modelIndex].apiKeyName = currentReplacement
        }
        providers[currentProviderIndex].apiKey = normalizedProviderKeys(currentProviderIndex).first?.value ?? ""
        providerKeyTableView.reloadData()
        modelTableView.reloadData()
        reloadRouteTable()
        showProviderKey(at: min(currentKeyIndex, providers[currentProviderIndex].apiKeys.count - 1))
    }

    @objc func addProvider() {
        commitEditor()
        providers.append(.blank())
        markPendingChanges()
        providerTableView.reloadData()
        reloadRouteTable()
        showProvider(at: providers.count - 1)
    }

    @objc func deleteProvider() {
        guard let providerIndex = selectedProviderIndex else { return }
        let providerID = providers[providerIndex].editorID
        let alert = NSAlert()
        alert.messageText = "Delete provider?"
        alert.informativeText = "\(providers[providerIndex].displayName) (\(providers[providerIndex].models.count) models)"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Delete")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        guard let currentProviderIndex = providers.firstIndex(where: { $0.editorID == providerID }) else { return }
        providerEditorTargetIndex = nil
        providerEditorTargetID = nil
        providerKeyEditorTarget = nil
        providerEditorDirty = false
        modelEditorTarget = nil
        selectedModelInfoRequestGeneration += 1

        providers.remove(at: currentProviderIndex)
        markPendingChanges()
        providerTableView.reloadData()
        modelTableView.reloadData()
        reloadRouteTable()
        scrollTableToTop(modelTableView)
        if providers.isEmpty {
            renderProviderSelection()
        } else {
            showProvider(at: min(currentProviderIndex, providers.count - 1))
        }
    }

    @objc func addModel() {
        commitEditor()
        guard let providerIndex = selectedProviderIndex else { return }
        ensureProviderHasKey(providerIndex)
        var model = EditableModel.blank()
        if let key = normalizedProviderKeys(providerIndex).first {
            model.apiKeyName = key.name
            model.apiKey = key.value
        }
        providers[providerIndex].models.append(model)
        markPendingChanges()
        providerTableView.reloadData(forRowIndexes: IndexSet(integer: providerIndex), columnIndexes: IndexSet(integersIn: 0..<providerTableView.numberOfColumns))
        modelTableView.reloadData()
        let addedModelIndex = providers[providerIndex].models.count - 1
        let addedIdentity = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: addedModelIndex)
        reloadRouteTable(preserving: addedIdentity)
        showModel(providerIndex: providerIndex, modelIndex: addedModelIndex)
    }

    @objc func duplicateModel() {
        commitEditor()
        guard let providerIndex = selectedProviderIndex,
              let modelIndex = selectedModelIndex else { return }
        var copy = providers[providerIndex].models[modelIndex]
        copy.editorID = UUID()
        copy.deploymentToken = ""
        providers[providerIndex].models.insert(copy, at: modelIndex + 1)
        markPendingChanges()
        providerTableView.reloadData(forRowIndexes: IndexSet(integer: providerIndex), columnIndexes: IndexSet(integersIn: 0..<providerTableView.numberOfColumns))
        modelTableView.reloadData()
        let copiedModelIndex = modelIndex + 1
        let copiedIdentity = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: copiedModelIndex)
        reloadRouteTable(preserving: copiedIdentity)
        showModel(providerIndex: providerIndex, modelIndex: copiedModelIndex)
    }

    @objc func deleteModel() {
        guard let providerIndex = selectedProviderIndex,
              let modelIndex = selectedModelIndex else { return }
        let providerID = providers[providerIndex].editorID
        let modelID = providers[providerIndex].models[modelIndex].editorID
        let alert = NSAlert()
        alert.messageText = "Delete model?"
        alert.informativeText = providers[providerIndex].models[modelIndex].displayName
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Delete")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        guard let currentProviderIndex = providers.firstIndex(where: { $0.editorID == providerID }),
              let currentModelIndex = providers[currentProviderIndex].models.firstIndex(where: { $0.editorID == modelID }) else { return }

        providers[currentProviderIndex].models.remove(at: currentModelIndex)
        modelEditorTarget = nil
        selectedModelInfoRequestGeneration += 1
        markPendingChanges()
        providerTableView.reloadData(forRowIndexes: IndexSet(integer: currentProviderIndex), columnIndexes: IndexSet(integersIn: 0..<providerTableView.numberOfColumns))
        modelTableView.reloadData()
        if providers[currentProviderIndex].models.isEmpty {
            reloadRouteTable()
            showProvider(at: currentProviderIndex)
        } else {
            let nextModelIndex = min(currentModelIndex, providers[currentProviderIndex].models.count - 1)
            let nextIdentity = modelSelectionIdentity(providerIndex: currentProviderIndex, modelIndex: nextModelIndex)
            reloadRouteTable(preserving: nextIdentity)
            showModel(providerIndex: currentProviderIndex, modelIndex: nextModelIndex)
        }
    }

    @objc func save() {
        guard hasPendingChanges else { return }
        let generation = beginLatestRuntimeApply()
        do {
            let providersToSave = try validatedProvidersForSave()
            let expectedRevision = loadedConfigRevision
            setRuntimeApplyInFlight(true)
            setEditorStatus("Saving config...")

            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                guard let self else { return }
                let result: Result<ConfigEditorSaveResult, Error>
                do {
                    result = .success(
                        try self.saveProviders(
                            providersToSave,
                            expectedRevision: expectedRevision
                        )
                    )
                } catch {
                    result = .failure(error)
                }

                DispatchQueue.main.async {
                    guard self.runtimeApplyGeneration == generation else { return }
                    switch result {
                    case .success(let saveResult):
                        self.loadedConfigRevision = saveResult.revision
                        self.setPendingChanges(false)
                        self.reloadRouteTable()
                        self.refreshRuntimeMap()
                        self.applyRuntimeConfigAfterSave(
                            saveResult,
                            generation: generation
                        )
                    case .failure(let error):
                        self.setRuntimeApplyInFlight(false)
                        self.setEditorError(
                            "Apply failed",
                            message: error.localizedDescription
                        )
                    }
                }
            }
        } catch {
            if runtimeApplyGeneration == generation {
                setRuntimeApplyInFlight(false)
            }
            setEditorError("Apply failed", message: error.localizedDescription)
        }
    }

    @objc func cancel() {
        window.orderOut(nil)
    }

    func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = shortAlertMessage(message)
        alert.alertStyle = .warning
        alert.runModal()
    }
}
