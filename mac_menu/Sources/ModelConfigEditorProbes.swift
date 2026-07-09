import Cocoa

extension ModelConfigEditorController {
    func controlTextDidChange(_ obj: Notification) {
        if isRenderingSelection {
            return
        }
        markProviderEditorDirty(for: obj.object)
        markPendingChanges()
    }

    func controlTextDidEndEditing(_ obj: Notification) {
        if isRenderingSelection {
            return
        }
        markProviderEditorDirty(for: obj.object)
        commitEditor()
    }

    @objc func formCheckboxChanged(_ sender: NSButton) {
        let currentProviderIndex = selectedProviderIndex ?? providerEditorTargetIndex ?? modelEditorTarget?.provider
        let currentModelIndex = selectedModelIndex ?? modelEditorTarget?.model
        let currentProviderKeyIndex = selectedProviderKeyIndex ?? providerKeyEditorTarget?.key
        let isModelCheckbox = sender === enabledCheckbox
        if sender === providerEnabledCheckbox || sender === providerKeyEnabledCheckbox {
            providerEditorDirty = true
        }
        commitEditor()
        markPendingChanges()
        if isModelCheckbox {
            if let providerIndex = currentProviderIndex,
               let modelIndex = currentModelIndex,
               providerIndex >= 0,
               providerIndex < providers.count,
               modelIndex >= 0,
               modelIndex < providers[providerIndex].models.count {
                isRenderingSelection = true
                modelTableView.reloadData(forRowIndexes: IndexSet(integer: modelIndex), columnIndexes: IndexSet(integersIn: 0..<modelTableView.numberOfColumns))
                modelTableView.selectRowIndexes(IndexSet(integer: modelIndex), byExtendingSelection: false)
                modelTableView.scrollRowToVisible(modelIndex)
                isRenderingSelection = false
                renderModelSelection()
            } else {
                modelTableView.reloadData()
            }
            refreshRuntimeMap()
            return
        }

        reloadSelectionTablesPreserving(
            providerIndex: currentProviderIndex,
            modelIndex: currentModelIndex,
            providerKeyIndex: currentProviderKeyIndex
        )
        refreshRuntimeMap()
    }

    @objc func modelApiKeySelectionChanged(_ sender: NSPopUpButton) {
        commitEditor()
        markPendingChanges()
    }

    @objc func modelCandidateApiKeySelectionChanged(_ sender: NSPopUpButton) {
        refreshModelCandidateControlsEnabled()
    }

    @objc func upstreamApiSupportChanged(_ sender: NSButton) {
        if isRenderingSelection {
            return
        }
        let modes = selectedSupportedUpstreamApiModes()
        let active = effectiveUpstreamApiMode(from: modes)
        setUpstreamApiSupportCheckboxes(modes)
        applyAdapterControls(forUpstreamApiMode: active)
        commitEditor()
        markPendingChanges()
    }

    @objc func adapterSelectionChanged(_ sender: NSPopUpButton) {
        customAdapterField.isHidden = !selectedAdapterIsCustom
        customAdapterField.isEnabled = selectedAdapterIsCustom && selectedModelIndex != nil
        commitEditor()
        markPendingChanges()
    }

    @objc func fetchModelCandidates() {
        do {
            commitEditor()
            let request = try currentModelCandidateRequest()
            let generation = modelCandidateRequestGeneration + 1
            modelCandidateRequestGeneration = generation
            setModelCandidateFetchState(true)
            setEditorStatus("Fetch models: requesting /v1/models with key \(request.keyDisplayName)...")

            getJSONCandidate(
                urls: request.urls,
                apiKey: request.apiKey,
                timeout: 30
            ) { [weak self] url, httpResponse, data, error in
                guard let self = self, self.modelCandidateRequestGeneration == generation else { return }
                self.setModelCandidateFetchState(false)

                if let error = error {
                    self.setEditorError("Fetch models failed", message: "\(url.absoluteString)\n\(error.localizedDescription)")
                    return
                }

                guard let httpResponse else {
                    self.setEditorError("Fetch models failed", message: "\(url.absoluteString)\nNo HTTP response returned.")
                    return
                }

                guard (200...299).contains(httpResponse.statusCode), let data = data else {
                    let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                    let message = body.isEmpty
                        ? "\(url.absoluteString)\nHTTP \(httpResponse.statusCode)"
                        : "\(url.absoluteString)\nHTTP \(httpResponse.statusCode)\n\(body)"
                    self.setEditorError("Fetch models failed", message: message)
                    return
                }

                do {
                    let models = try self.parseModelCandidates(data: data)
                    guard let providerIndex = self.providerIndex(for: request) else {
                        return
                    }
                    if self.selectedProviderIndex != providerIndex {
                        return
                    }
                    self.showFetchedModelChooser(models: models, request: request)
                } catch {
                    self.setEditorError("Fetch models failed", message: error.localizedDescription)
                }
            }
        } catch {
            setEditorError("Fetch models failed", message: error.localizedDescription)
        }
    }

    @objc func probeModelAvailability() {
        do {
            commitEditor()
            let request = try currentModelAvailabilityProbeRequest()
            if modelAvailabilityProbeRuns[request.probeKey] != nil {
                refreshModelAvailabilityProbeControlsEnabled()
                return
            }
            let runID = UUID()
            modelAvailabilityProbeRuns[request.probeKey] = runID
            refreshModelAvailabilityProbeControlsEnabled()
            setEditorStatus("Model probe: checking LiteLLM /model/info for \(request.providerName)/\(request.keyName) \(request.upstreamModel)...")

            fetchLiteLLMModelInfoCapability(lookup: request.modelInfoLookup) { [weak self] result in
                guard let self = self else { return }
                guard self.modelAvailabilityProbeRuns[request.probeKey] == runID else { return }
                guard self.modelProbeRequestStillMatches(request) else {
                    self.modelAvailabilityProbeRuns.removeValue(forKey: request.probeKey)
                    self.refreshModelAvailabilityProbeControlsEnabled()
                    return
                }
                switch result {
                case .success(let capability):
                    let detail = capability.map { "LiteLLM /model/info reports \($0.summary)." }
                        ?? self.missingLiteLLMModelInfoMessage(lookup: request.modelInfoLookup)
                    if let capability, capability.isImageGenerationEndpointModel {
                        self.runModelAvailabilityImageGenerationProbe(request: request, runID: runID, preflightDetail: detail)
                        return
                    }
                    self.runModelAvailabilityChatProbe(request: request, runID: runID, preflightDetail: detail)
                case .failure(let detail):
                    self.runModelAvailabilityChatProbe(
                        request: request,
                        runID: runID,
                        preflightDetail: "LiteLLM /model/info failed: \(detail.localizedDescription)"
                    )
                }
            }
        } catch {
            setEditorError("Model probe failed", message: error.localizedDescription)
        }
    }

    func runModelAvailabilityChatProbe(
        request: ModelAvailabilityProbeRequest,
        runID: UUID,
        preflightDetail: String
    ) {
        setEditorStatus("Model probe: probing /chat/completions for \(request.providerName)/\(request.keyName) \(request.upstreamModel)...")

        do {
            let body = try modelAvailabilityProbeBody(model: request.upstreamModel)
            postJSONProbe(
                urls: request.chatURLs,
                apiKey: request.apiKey,
                apiBase: request.apiBase,
                body: body,
                timeout: 45
            ) { [weak self] url, httpResponse, data, error in
                guard let self = self else { return }
                guard self.modelAvailabilityProbeRuns[request.probeKey] == runID else { return }
                guard self.modelProbeRequestStillMatches(request) else {
                    self.modelAvailabilityProbeRuns.removeValue(forKey: request.probeKey)
                    self.refreshModelAvailabilityProbeControlsEnabled()
                    return
                }
                self.modelAvailabilityProbeRuns.removeValue(forKey: request.probeKey)
                self.refreshModelAvailabilityProbeControlsEnabled()

                if let error = error {
                    self.applyModelAvailabilityProbeOutcome(
                        .unavailable("\(preflightDetail)\n\(url.path) failed: \(error.localizedDescription)"),
                        request: request
                    )
                    return
                }

                guard let httpResponse else {
                    self.applyModelAvailabilityProbeOutcome(
                        .unavailable("\(preflightDetail)\n\(url.path) failed: No HTTP response returned."),
                        request: request
                    )
                    return
                }

                let outcome = self.parseModelAvailabilityProbeOutcome(statusCode: httpResponse.statusCode, data: data)
                self.applyModelAvailabilityProbeOutcome(self.outcome(outcome, prefixing: "\(preflightDetail)\nProbe URL: \(url.absoluteString)"), request: request)
            }
        } catch {
            modelAvailabilityProbeRuns.removeValue(forKey: request.probeKey)
            refreshModelAvailabilityProbeControlsEnabled()
            applyModelAvailabilityProbeOutcome(
                .unavailable("\(preflightDetail)\n/chat/completions request failed: \(error.localizedDescription)"),
                request: request
            )
        }
    }

    func runModelAvailabilityImageGenerationProbe(
        request: ModelAvailabilityProbeRequest,
        runID: UUID,
        preflightDetail: String
    ) {
        setEditorStatus("Model probe: probing /images/generations for \(request.providerName)/\(request.keyName) \(request.upstreamModel)...")

        do {
            let body = try modelAvailabilityImageGenerationProbeBody(model: request.upstreamModel)
            postJSONProbe(
                urls: request.imageGenerationURLs,
                apiKey: request.apiKey,
                apiBase: request.apiBase,
                body: body,
                timeout: 60
            ) { [weak self] url, httpResponse, data, error in
                guard let self = self else { return }
                guard self.modelAvailabilityProbeRuns[request.probeKey] == runID else { return }
                guard self.modelProbeRequestStillMatches(request) else {
                    self.modelAvailabilityProbeRuns.removeValue(forKey: request.probeKey)
                    self.refreshModelAvailabilityProbeControlsEnabled()
                    return
                }
                self.modelAvailabilityProbeRuns.removeValue(forKey: request.probeKey)
                self.refreshModelAvailabilityProbeControlsEnabled()

                if let error = error {
                    self.applyModelAvailabilityProbeOutcome(
                        .unavailable("\(preflightDetail)\n\(url.path) failed: \(error.localizedDescription)"),
                        request: request
                    )
                    return
                }

                guard let httpResponse else {
                    self.applyModelAvailabilityProbeOutcome(
                        .unavailable("\(preflightDetail)\n\(url.path) failed: No HTTP response returned."),
                        request: request
                    )
                    return
                }

                let outcome = self.parseModelAvailabilityImageGenerationProbeOutcome(statusCode: httpResponse.statusCode, data: data)
                self.applyModelAvailabilityProbeOutcome(self.outcome(outcome, prefixing: "\(preflightDetail)\nProbe URL: \(url.absoluteString)"), request: request)
            }
        } catch {
            modelAvailabilityProbeRuns.removeValue(forKey: request.probeKey)
            refreshModelAvailabilityProbeControlsEnabled()
            applyModelAvailabilityProbeOutcome(
                .unavailable("\(preflightDetail)\n/images/generations request failed: \(error.localizedDescription)"),
                request: request
            )
        }
    }

    func outcome(_ outcome: ModelAvailabilityProbeOutcome, prefixing detail: String) -> ModelAvailabilityProbeOutcome {
        switch outcome {
        case .available(let message):
            return .available("\(detail)\n\(message)")
        case .unavailable(let message):
            return .unavailable("\(detail)\n\(message)")
        case .inconclusive(let message):
            return .inconclusive("\(detail)\n\(message)")
        }
    }

    @objc func probeResponsesEndpointSupport() {
        beginResponsesEndpointProbe(automatic: false)
    }

    func beginResponsesEndpointProbe(automatic: Bool) {
        do {
            if !automatic {
                commitEditor()
            }
            let request = try currentModelAvailabilityProbeRequest()
            if responsesEndpointProbeRuns[request.probeKey] != nil {
                refreshResponsesEndpointProbeControlsEnabled()
                return
            }
            let runID = UUID()
            responsesEndpointProbeRuns[request.probeKey] = runID
            refreshResponsesEndpointProbeControlsEnabled()
            runResponsesEndpointProbe(request: request, runID: runID, automatic: automatic)
        } catch {
            if automatic {
                setEditorStatus(
                    "Auto URL probe: skipped (\(error.localizedDescription))",
                    color: .secondaryLabelColor
                )
            } else {
                setEditorError("URL probe failed", message: error.localizedDescription)
            }
        }
    }

    func runResponsesEndpointProbe(
        request: ModelAvailabilityProbeRequest,
        runID: UUID,
        automatic: Bool
    ) {
        let prefix = automatic ? "Auto URL probe" : "URL probe"
        setEditorStatus("\(prefix): probing surfaces exposed by \(request.providerName)/\(request.keyName) \(request.upstreamModel)...")

        do {
            let responsesBody = try responsesEndpointProbeBody(model: request.upstreamModel)
            let chatBody = try modelAvailabilityProbeBody(model: request.upstreamModel)
            let anthropicBody = try anthropicMessagesProbeBody(model: request.upstreamModel)
            var detectedModes: [String] = []
            var details: [String] = []

            func addDetected(_ mode: String) {
                if !detectedModes.contains(mode) {
                    detectedModes.append(mode)
                }
            }

            func finish() {
                self.responsesEndpointProbeRuns.removeValue(forKey: request.probeKey)
                self.refreshResponsesEndpointProbeControlsEnabled()
                if detectedModes.isEmpty {
                    self.applyResponsesEndpointProbeOutcome(
                        .failed((details + ["No supported API surface was detected."]).joined(separator: "\n\n")),
                        request: request,
                        automatic: automatic
                    )
                    return
                }
                let active = self.effectiveUpstreamApiMode(from: detectedModes)
                self.applyResponsesEndpointProbeOutcome(
                    .detected(active, detectedModes, details.joined(separator: "\n\n")),
                    request: request,
                    automatic: automatic
                )
            }

            func probeAnthropic() {
                self.postJSONProbe(
                    urls: request.anthropicURLs,
                    apiKey: request.apiKey,
                    apiBase: request.apiBase,
                    body: anthropicBody,
                    timeout: 45,
                    extraHeaders: ["anthropic-version": "2023-06-01"]
                ) { [weak self] url, httpResponse, data, error in
                    guard let self = self else { return }
                    guard self.responsesEndpointProbeRuns[request.probeKey] == runID else { return }
                    guard self.modelProbeRequestStillMatches(request) else {
                        self.responsesEndpointProbeRuns.removeValue(forKey: request.probeKey)
                        self.refreshResponsesEndpointProbeControlsEnabled()
                        return
                    }
                    if let error {
                        details.append("anthropic probe URL: \(url.absoluteString)\n\(error.localizedDescription)")
                        finish()
                        return
                    }
                    guard let httpResponse else {
                        details.append("anthropic probe URL: \(url.absoluteString)\nNo HTTP response returned.")
                        finish()
                        return
                    }
                    let detail = self.probeDetail(surface: "anthropic", url: url, statusCode: httpResponse.statusCode, data: data)
                    details.append(detail)
                    if self.apiEndpointExists(statusCode: httpResponse.statusCode, data: data) == true {
                        addDetected("anthropic")
                    }
                    finish()
                }
            }

            func probeChat() {
                self.postJSONProbe(
                    urls: request.chatURLs,
                    apiKey: request.apiKey,
                    apiBase: request.apiBase,
                    body: chatBody,
                    timeout: 45
                ) { [weak self] url, httpResponse, data, error in
                    guard let self = self else { return }
                    guard self.responsesEndpointProbeRuns[request.probeKey] == runID else { return }
                    guard self.modelProbeRequestStillMatches(request) else {
                        self.responsesEndpointProbeRuns.removeValue(forKey: request.probeKey)
                        self.refreshResponsesEndpointProbeControlsEnabled()
                        return
                    }
                    if let error {
                        details.append("openai/chat probe URL: \(url.absoluteString)\n\(error.localizedDescription)")
                        probeAnthropic()
                        return
                    }
                    guard let httpResponse else {
                        details.append("openai/chat probe URL: \(url.absoluteString)\nNo HTTP response returned.")
                        probeAnthropic()
                        return
                    }
                    let detail = self.probeDetail(surface: "openai/chat", url: url, statusCode: httpResponse.statusCode, data: data)
                    details.append(detail)
                    if self.apiEndpointExists(statusCode: httpResponse.statusCode, data: data) == true {
                        addDetected("openai/chat")
                    }
                    probeAnthropic()
                }
            }

            postJSONProbe(
                urls: request.responsesURLs,
                apiKey: request.apiKey,
                apiBase: request.apiBase,
                body: responsesBody,
                timeout: 45
            ) { [weak self] url, httpResponse, data, error in
                guard let self = self else { return }
                guard self.responsesEndpointProbeRuns[request.probeKey] == runID else { return }
                guard self.modelProbeRequestStillMatches(request) else {
                    self.responsesEndpointProbeRuns.removeValue(forKey: request.probeKey)
                    self.refreshResponsesEndpointProbeControlsEnabled()
                    return
                }
                if let error {
                    details.append("openai/responses probe URL: \(url.absoluteString)\n\(error.localizedDescription)")
                    probeChat()
                    return
                }
                guard let httpResponse else {
                    details.append("openai/responses probe URL: \(url.absoluteString)\nNo HTTP response returned.")
                    probeChat()
                    return
                }
                let detail = self.probeDetail(surface: "openai/responses", url: url, statusCode: httpResponse.statusCode, data: data)
                details.append(detail)
                if self.apiEndpointExists(statusCode: httpResponse.statusCode, data: data) == true {
                    addDetected("openai/responses")
                }
                probeChat()
            }
        } catch {
            responsesEndpointProbeRuns.removeValue(forKey: request.probeKey)
            refreshResponsesEndpointProbeControlsEnabled()
            applyResponsesEndpointProbeOutcome(
                .failed("URL probe request failed: \(error.localizedDescription)"),
                request: request,
                automatic: automatic
            )
        }
    }

    func setModelCandidateFetchState(_ loading: Bool) {
        modelCandidateFetchInFlight = loading
        if loading {
            fetchModelsButton.title = "Fetching..."
        } else {
            fetchModelsButton.title = "Fetch /v1/models"
        }
        refreshModelCandidateControlsEnabled()
    }

    func applyModelAvailabilityProbeOutcome(
        _ outcome: ModelAvailabilityProbeOutcome,
        request: ModelAvailabilityProbeRequest
    ) {
        guard modelProbeRequestStillMatches(request) else {
            return
        }

        var model = providers[request.providerIndex].models[request.modelIndex]
        let originalModel = model
        let label = "\(request.providerName)/\(request.keyName) \(request.upstreamModel)"

        switch outcome {
        case .available(let detail):
            model.modelEnabled = true
            providers[request.providerIndex].models[request.modelIndex] = model
            providers[request.providerIndex].models[request.modelIndex].enabled =
                modelEffectivelyEnabled(providerIndex: request.providerIndex, model: model)
            if selectedProviderIndex == request.providerIndex, selectedModelIndex == request.modelIndex {
                enabledCheckbox.state = .on
            }
            if modelEffectivelyEnabled(providerIndex: request.providerIndex, model: model) {
                setEditorStatus("Model probe: available for \(label).", tooltip: detail)
            } else {
                let reason = routeOffReason(RouteDeploymentRow(
                    providerIndex: request.providerIndex,
                    modelIndex: request.modelIndex,
                    publicModel: routePublicModelName(model),
                    providerName: request.providerName,
                    keyName: request.keyName,
                    upstreamModel: request.upstreamModel,
                    order: parseOrder(model.order),
                    enabled: false
                ))
                setEditorStatus("Model probe: available for \(label), but route is still OFF (\(reason)).", color: .secondaryLabelColor, tooltip: detail)
            }
        case .unavailable(let detail):
            model.modelEnabled = false
            providers[request.providerIndex].models[request.modelIndex] = model
            providers[request.providerIndex].models[request.modelIndex].enabled = false
            if selectedProviderIndex == request.providerIndex, selectedModelIndex == request.modelIndex {
                enabledCheckbox.state = .off
            }
            let tooltip = "Model enabled was turned off for this deployment.\n\(detail)"
            let inlineDetail = inlineProbeFailureDetail(from: detail)
            let message = inlineDetail.isEmpty
                ? "Model probe: unavailable for \(label)."
                : "Model probe: \(inlineDetail) for \(label)."
            setEditorStatus(message, color: .secondaryLabelColor, tooltip: tooltip)
        case .inconclusive(let detail):
            let inlineDetail = inlineProbeFailureDetail(from: detail)
            let message = inlineDetail.isEmpty
                ? "Model probe: inconclusive for \(label); model enabled was not changed."
                : "Model probe: inconclusive (\(inlineDetail)) for \(label); model enabled was not changed."
            setEditorStatus(message, color: .secondaryLabelColor, tooltip: detail)
        }

        markPendingChangesIfNeeded(
            providers[request.providerIndex].models[request.modelIndex] != originalModel,
            updateStatus: false
        )
        if selectedProviderIndex == request.providerIndex {
            modelTableView.reloadData(forRowIndexes: IndexSet(integer: request.modelIndex), columnIndexes: IndexSet(integersIn: 0..<modelTableView.numberOfColumns))
        }
        reloadRouteTable(preserving: (request.providerIndex, request.modelIndex))
        refreshRuntimeMap()
    }

    func applyResponsesEndpointProbeOutcome(
        _ outcome: UpstreamApiModeProbeOutcome,
        request: ModelAvailabilityProbeRequest,
        automatic: Bool
    ) {
        guard modelProbeRequestStillMatches(request) else {
            return
        }

        var model = providers[request.providerIndex].models[request.modelIndex]
        let originalModel = model
        let label = "\(request.providerName)/\(request.keyName) \(request.upstreamModel)"
        let prefix = automatic ? "Auto URL probe" : "URL probe"

        switch outcome {
        case .detected(let mode, let supportedModes, let detail):
            let normalizedMode = normalizedUpstreamApiMode(mode)
            var normalizedSupportedModes: [String] = []
            for item in supportedModes {
                let supportedMode = normalizedUpstreamApiMode(item)
                if !normalizedSupportedModes.contains(supportedMode) {
                    normalizedSupportedModes.append(supportedMode)
                }
            }
            if !normalizedSupportedModes.contains(normalizedMode) {
                normalizedSupportedModes.append(normalizedMode)
            }
            model.upstreamApiMode = normalizedMode
            model.upstreamApiModePresent = true
            model.supportedUpstreamApiModes = normalizedSupportedModes
            model.supportedUpstreamApiModesPresent = true
            model.supportsResponsesEndpoint = normalizedSupportedModes.contains("openai/responses")
            model.supportsResponsesEndpointPresent = false
            model.litellmModel = litellmModel(model.litellmModel, settingAdapterFor: normalizedMode)
            providers[request.providerIndex].models[request.modelIndex] = model
            if selectedProviderIndex == request.providerIndex, selectedModelIndex == request.modelIndex {
                setUpstreamApiSupportCheckboxes(normalizedSupportedModes)
                setAdapterControls(from: model.litellmModel)
            }
            setEditorStatus("\(prefix): detected \(normalizedSupportedModes.joined(separator: ", ")) for \(label).", tooltip: "Effective: \(normalizedMode)\n\n\(detail)")
        case .failed(let detail):
            if automatic {
                setEditorStatus(
                    "\(prefix): inconclusive for \(label); URL surface settings were not changed.",
                    color: .secondaryLabelColor,
                    tooltip: detail
                )
            } else {
                setEditorError("\(prefix) failed", message: detail)
            }
            return
        }

        markPendingChangesIfNeeded(model != originalModel, updateStatus: false)
        if selectedProviderIndex == request.providerIndex {
            modelTableView.reloadData(forRowIndexes: IndexSet(integer: request.modelIndex), columnIndexes: IndexSet(integersIn: 0..<modelTableView.numberOfColumns))
        }
        reloadRouteTable(preserving: (request.providerIndex, request.modelIndex))
        refreshResponsesEndpointSupportControls()
        refreshRuntimeMap()
    }

    func showFetchedModelChooser(
        models: [String],
        request: ModelCandidateRequest
    ) {
        let fetchedModels = models
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !fetchedModels.isEmpty else {
            setEditorStatus("Fetch models: no models returned.", color: .secondaryLabelColor)
            return
        }

        guard let currentProviderIndex = providerIndex(for: request) else { return }
        let providerName = providers[currentProviderIndex].displayName
        let keyName = providerKey(for: request, providerIndex: currentProviderIndex).map { modelCandidateKeyTitle($0) } ?? request.keyDisplayName
        let contentWidth: CGFloat = 620
        let rowHeight: CGFloat = 28
        let listHeight = min(480, max(220, CGFloat(fetchedModels.count) * rowHeight + 2))
        let chooserController = FetchedModelChooserController(models: fetchedModels, width: contentWidth - 36)
        let panel = makeFetchedModelChooserPanel(
            providerName: providerName,
            keyName: keyName,
            modelCount: fetchedModels.count,
            contentWidth: contentWidth,
            listHeight: listHeight,
            chooserController: chooserController
        )

        fetchedModelChooserController = chooserController
        defer {
            panel.close()
            fetchedModelChooserController = nil
        }

        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        guard NSApp.runModal(for: panel) == .OK else { return }
        let selectedModels = chooserController.selectedModels

        guard !selectedModels.isEmpty else {
            setEditorStatus("Fetch models: no models selected.", color: .secondaryLabelColor)
            return
        }

        guard let addProviderIndex = providerIndex(for: request) else { return }
        addFetchedModels(selectedModels, providerIndex: addProviderIndex, request: request)
    }

    func makeFetchedModelChooserPanel(
        providerName: String,
        keyName: String,
        modelCount: Int,
        contentWidth: CGFloat,
        listHeight: CGFloat,
        chooserController: FetchedModelChooserController
    ) -> NSPanel {
        let contentHeight: CGFloat = 96 + listHeight + 52
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: contentWidth, height: contentHeight),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "Choose Models to Add"
        panel.minSize = NSSize(width: 520, height: 300)
        panel.isReleasedWhenClosed = false
        panel.delegate = chooserController
        chooserController.modalWindow = panel

        let content = NSView()
        panel.contentView = content

        let titleLabel = NSTextField(labelWithString: "Choose models to add")
        titleLabel.font = NSFont.systemFont(ofSize: 16, weight: .semibold)

        let subtitleLabel = NSTextField(labelWithString: "Provider: \(providerName)    Key: \(keyName)    Models: \(modelCount)")
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.lineBreakMode = .byTruncatingMiddle

        let selectionControls = NSStackView()
        selectionControls.orientation = .horizontal
        selectionControls.alignment = .centerY
        selectionControls.spacing = 8
        let selectAllButton = NSButton(title: "Select All", target: chooserController, action: #selector(FetchedModelChooserController.selectAllAction(_:)))
        selectAllButton.bezelStyle = .rounded
        let invertSelectionButton = NSButton(title: "Invert", target: chooserController, action: #selector(FetchedModelChooserController.invertSelectionAction(_:)))
        invertSelectionButton.bezelStyle = .rounded
        selectionControls.addArrangedSubview(selectAllButton)
        selectionControls.addArrangedSubview(invertSelectionButton)
        selectionControls.addArrangedSubview(spacer())

        let scroll = FetchedModelScrollView()
        scroll.wantsLayer = true
        scroll.borderType = .bezelBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = false
        scroll.hasHorizontalScroller = false
        scroll.usesPredominantAxisScrolling = true
        scroll.verticalScrollElasticity = .none
        scroll.documentView = chooserController.listView
        chooserController.listView.frame = NSRect(
            x: 0,
            y: 0,
            width: contentWidth - 36,
            height: max(listHeight, CGFloat(modelCount) * 28)
        )
        chooserController.listView.autoresizingMask = [.width]

        let cancelButton = NSButton(title: "Cancel", target: chooserController, action: #selector(FetchedModelChooserController.cancelAction(_:)))
        cancelButton.bezelStyle = .rounded
        cancelButton.keyEquivalent = "\u{1b}"
        let addButton = NSButton(title: "Add Selected", target: chooserController, action: #selector(FetchedModelChooserController.addSelectedAction(_:)))
        addButton.bezelStyle = .rounded
        addButton.keyEquivalent = "\r"

        for view in [titleLabel, subtitleLabel, selectionControls, scroll, cancelButton, addButton] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }

        NSLayoutConstraint.activate([
            titleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            titleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            titleLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),

            subtitleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            subtitleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            subtitleLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 4),

            selectionControls.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            selectionControls.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            selectionControls.topAnchor.constraint(equalTo: subtitleLabel.bottomAnchor, constant: 12),
            selectionControls.heightAnchor.constraint(equalToConstant: 28),

            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            scroll.topAnchor.constraint(equalTo: selectionControls.bottomAnchor, constant: 8),
            scroll.bottomAnchor.constraint(equalTo: cancelButton.topAnchor, constant: -16),

            addButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            addButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),

            cancelButton.trailingAnchor.constraint(equalTo: addButton.leadingAnchor, constant: -8),
            cancelButton.centerYAnchor.constraint(equalTo: addButton.centerYAnchor),
        ])

        panel.center()
        return panel
    }

    func providerIndex(for request: ModelCandidateRequest) -> Int? {
        if request.providerIndex >= 0,
           request.providerIndex < providers.count,
           providers[request.providerIndex].editorID == request.providerEditorID {
            return request.providerIndex
        }
        return providers.firstIndex(where: { $0.editorID == request.providerEditorID })
    }

    func providerKey(for request: ModelCandidateRequest, providerIndex: Int) -> EditableProviderKey? {
        ensureProviderHasKey(providerIndex)
        let keys = normalizedProviderKeys(providerIndex)
        if let keyEditorID = request.keyEditorID,
           let key = keys.first(where: { $0.editorID == keyEditorID }) {
            return key
        }
        if request.keyName.isEmpty {
            return keys.first
        }
        return keys.first { $0.name == request.keyName }
    }

    func addFetchedModels(_ models: [String], providerIndex: Int, request: ModelCandidateRequest) {
        guard providerIndex >= 0,
              providerIndex < providers.count,
              providers[providerIndex].editorID == request.providerEditorID else { return }
        ensureProviderHasKey(providerIndex)
        let provider = providers[providerIndex]
        let key = providerKey(for: request, providerIndex: providerIndex)
        guard let key else { return }

        var addedCount = 0
        for upstream in models {
            var model = EditableModel.blank()
            model.enabled = true
            model.modelEnabled = true
            model.modelName = upstream
            model.litellmModel = request.adapter.isEmpty ? upstream : "\(request.adapter)/\(upstream)"
            model.apiKeyName = key.name
            model.apiKey = key.value
            model.order = "1"
            model.sslVerify = ""
            model.supportsImageGeneration = false
            model.supportsImageGenerationPresent = false
            model.upstreamApiMode = defaultUpstreamApiMode
            model.upstreamApiModePresent = false
            model.supportedUpstreamApiModes = [defaultUpstreamApiMode]
            model.supportedUpstreamApiModesPresent = false
            model.supportsResponsesEndpoint = true
            model.supportsResponsesEndpointPresent = false
            providers[providerIndex].models.append(model)
            addedCount += 1
        }

        providerTableView.reloadData()
        modelTableView.reloadData()
        if addedCount > 0 {
            let lastIndex = max(0, providers[providerIndex].models.count - 1)
            let lastIdentity = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: lastIndex)
            reloadRouteTable(preserving: lastIdentity)
            showModel(providerIndex: providerIndex, modelIndex: lastIndex)
            beginResponsesEndpointProbe(automatic: true)
        } else {
            reloadRouteTable()
        }
        refreshRuntimeMap()
        markPendingChangesIfNeeded(addedCount > 0)
        setEditorStatus("Added \(addedCount) model\(addedCount == 1 ? "" : "s") to \(provider.displayName).")
    }
}
