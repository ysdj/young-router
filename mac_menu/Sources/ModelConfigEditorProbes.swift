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
        if sender === providerEnabledCheckbox {
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
        if modes.isEmpty {
            sender.state = .on
            setEditorStatus("At least one upstream protocol is required.", color: .systemOrange)
            return
        }
        setUpstreamApiSupportCheckboxes(modes)
        commitEditor()
        markPendingChanges()
    }

    @objc func moveUpstreamApiModeUp(_ sender: NSButton) {
        moveSelectedUpstreamApiMode(sender.identifier?.rawValue ?? "", delta: -1)
    }

    @objc func moveUpstreamApiModeDown(_ sender: NSButton) {
        moveSelectedUpstreamApiMode(sender.identifier?.rawValue ?? "", delta: 1)
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
            runFullModelProbe(request: request, runID: runID, automatic: false)
        } catch {
            setEditorError("Model probe failed", message: error.localizedDescription)
        }
    }

    func recommendedUpstreamApiModes(from results: [UpstreamApiProbeResult]) -> [String] {
        probeProtocolRecommendation(
            priority: upstreamApiModes,
            availableModes: results.filter { $0.isAvailable }.map { $0.mode }
        ).supported
    }

    func upstreamApiProbeSummary(_ result: UpstreamApiProbeResult) -> String {
        switch result.availability {
        case .available:
            return "Available"
        case .unavailable:
            return "Unavailable"
        case .inconclusive:
            return "Uncertain"
        }
    }

    func runFullModelProbe(
        request: ModelAvailabilityProbeRequest,
        runID: UUID,
        automatic: Bool,
        completion: (() -> Void)? = nil
    ) {
        setEditorStatus("Full probe: checking model info and all API protocols for \(request.upstreamModel)...")
        fetchLiteLLMModelInfoCapability(lookup: request.modelInfoLookup) { [weak self] result in
            guard let self else { return }
            guard self.modelAvailabilityProbeRuns[request.probeKey] == runID else { return }
            let detail: String
            let capability: LiteLLMModelInfoCapability?
            switch result {
            case .success(let value):
                capability = value
                detail = value?.summary ?? self.missingLiteLLMModelInfoMessage(lookup: request.modelInfoLookup)
            case .failure(let error):
                capability = nil
                detail = "LiteLLM /model/info preflight unavailable: \(error.localizedDescription)"
            }
            if capability?.isImageGenerationEndpointModel == true {
                self.runFullImageModelProbe(
                    request: request,
                    runID: runID,
                    preflightDetail: detail,
                    completion: completion
                )
                return
            }
            self.runFullProtocolProbe(
                request: request,
                runID: runID,
                preflightDetail: detail,
                automatic: automatic,
                completion: completion
            )
        }
    }

    func finishFullModelProbe(
        request: ModelAvailabilityProbeRequest,
        completion: (() -> Void)? = nil
    ) {
        modelAvailabilityProbeRuns.removeValue(forKey: request.probeKey)
        refreshModelAvailabilityProbeControlsEnabled()
        completion?()
    }

    func runFullProtocolProbe(
        request: ModelAvailabilityProbeRequest,
        runID: UUID,
        preflightDetail: String,
        automatic: Bool,
        completion: (() -> Void)?
    ) {
        do {
            let probes: [(String, [URL], Data, [String: String])] = [
                ("openai/responses", request.responsesURLs, try responsesEndpointProbeBody(model: request.upstreamModel), [:]),
                ("openai/chat", request.chatURLs, try modelAvailabilityProbeBody(model: request.upstreamModel), [:]),
                (
                    "anthropic",
                    request.anthropicURLs,
                    try anthropicMessagesProbeBody(model: request.upstreamModel),
                    [
                        "anthropic-version": "2023-06-01",
                        "x-api-key": request.apiKey,
                    ]
                ),
            ]
            var results: [UpstreamApiProbeResult] = []

            func finish() {
                guard self.modelProbeRequestStillMatches(request) else {
                    self.finishFullModelProbe(request: request, completion: completion)
                    return
                }
                let recommended = self.recommendedUpstreamApiModes(from: results)
                guard !recommended.isEmpty else {
                    let detail = ([preflightDetail] + results.map { $0.detail }).joined(separator: "\n\n")
                    let hasInconclusive = results.contains { $0.availability == .inconclusive }
                    if !hasInconclusive {
                        self.runFullImageModelProbe(
                            request: request,
                            runID: runID,
                            preflightDetail: detail,
                            knownImageModel: false,
                            completion: completion
                        )
                        return
                    }
                    self.applyModelAvailabilityProbeOutcome(
                        .inconclusive(detail),
                        request: request
                    )
                    self.finishFullModelProbe(request: request, completion: completion)
                    return
                }
                if automatic {
                    self.applyFullProbeSelection(
                        recommended,
                        recommendedOrder: recommended,
                        request: request,
                        details: results,
                        preflightDetail: preflightDetail
                    )
                } else {
                    let currentModes = self.normalizedSupportedUpstreamApiModes(
                        for: self.providers[request.providerIndex].models[request.modelIndex]
                    )
                    if currentModes == recommended {
                        self.setEditorStatus(
                            "Probe complete. Available protocols are already saved in fallback order.",
                            tooltip: ([preflightDetail] + results.map { $0.detail }).joined(separator: "\n\n")
                        )
                    } else {
                        self.presentFullProbeRecommendation(
                            recommendedOrder: recommended,
                            request: request,
                            details: results,
                            preflightDetail: preflightDetail
                        )
                    }
                }
                self.finishFullModelProbe(request: request, completion: completion)
            }

            func run(_ index: Int) {
                if index >= probes.count { finish(); return }
                let (mode, urls, body, headers) = probes[index]
                self.postJSONProbe(
                    urls: urls, apiKey: request.apiKey, apiBase: request.apiBase,
                    body: body, timeout: 45, extraHeaders: headers
                ) { [weak self] url, response, data, error in
                    guard let self else { return }
                    guard self.modelAvailabilityProbeRuns[request.probeKey] == runID else { return }
                    guard self.modelProbeRequestStillMatches(request) else {
                        self.finishFullModelProbe(request: request, completion: completion)
                        return
                    }
                    let availability = response.map {
                        self.upstreamApiProbeAvailability(statusCode: $0.statusCode, data: data)
                    } ?? .inconclusive
                    let detail: String
                    if let response {
                        detail = self.probeDetail(surface: mode, url: url, statusCode: response.statusCode, data: data)
                    } else {
                        detail = "\(mode) probe URL: \(url.absoluteString)\n\(error?.localizedDescription ?? "No HTTP response returned.")"
                    }
                    results.append(UpstreamApiProbeResult(mode: mode, availability: availability, detail: detail))
                    run(index + 1)
                }
            }
            run(0)
        } catch {
            setEditorError("Full probe failed", message: error.localizedDescription)
            finishFullModelProbe(request: request, completion: completion)
        }
    }

    func runFullImageModelProbe(
        request: ModelAvailabilityProbeRequest,
        runID: UUID,
        preflightDetail: String,
        knownImageModel: Bool = true,
        completion: (() -> Void)?
    ) {
        if knownImageModel {
            markModelAsImageGenerationEndpoint(request: request)
        }
        do {
            let body = try modelAvailabilityImageGenerationProbeBody(model: request.upstreamModel)
            postJSONProbe(urls: request.imageGenerationURLs, apiKey: request.apiKey, apiBase: request.apiBase, body: body, timeout: 60) { [weak self] url, response, data, error in
                guard let self else { return }
                guard self.modelAvailabilityProbeRuns[request.probeKey] == runID else { return }
                let detail = error.map { "\(url.absoluteString)\n\($0.localizedDescription)" }
                    ?? response.map { self.probeDetail(surface: "image generation", url: url, statusCode: $0.statusCode, data: data) }
                    ?? "\(url.absoluteString)\nNo HTTP response returned."
                let outcome = response.map { self.parseModelAvailabilityImageGenerationProbeOutcome(statusCode: $0.statusCode, data: data) } ?? .inconclusive(detail)
                if case .available = outcome {
                    self.markModelAsImageGenerationEndpoint(request: request)
                }
                self.applyModelAvailabilityProbeOutcome(self.probeOutcome(outcome, prefixing: "\(preflightDetail)\n\n\(detail)"), request: request)
                self.finishFullModelProbe(request: request, completion: completion)
            }
        } catch {
            applyModelAvailabilityProbeOutcome(.inconclusive("\(preflightDetail)\n\n/images/generations probe failed: \(error.localizedDescription)"), request: request)
            finishFullModelProbe(request: request, completion: completion)
        }
    }

    func markModelAsImageGenerationEndpoint(request: ModelAvailabilityProbeRequest) {
        guard modelProbeRequestStillMatches(request) else { return }
        var model = providers[request.providerIndex].models[request.modelIndex]
        let originalModel = model
        model.modelInfoExtra["mode"] = .string("image_generation")
        model.supportsImageGeneration = false
        model.supportsImageGenerationPresent = false
        providers[request.providerIndex].models[request.modelIndex] = model
        markPendingChangesIfNeeded(model != originalModel, updateStatus: false)
        if selectedModelProbeKey() == request.probeKey {
            selectedModelImageGenerationEndpointDisabled = true
            refreshResponsesEndpointSupportControls()
        }
    }

    func probeOutcome(_ outcome: ModelAvailabilityProbeOutcome, prefixing detail: String) -> ModelAvailabilityProbeOutcome {
        switch outcome {
        case .available(let message): return .available("\(detail)\n\(message)")
        case .unavailable(let message): return .unavailable("\(detail)\n\(message)")
        case .inconclusive(let message): return .inconclusive("\(detail)\n\(message)")
        }
    }

    func presentFullProbeRecommendation(
        recommendedOrder: [String],
        request: ModelAvailabilityProbeRequest,
        details: [UpstreamApiProbeResult],
        preflightDetail: String
    ) {
        let alert = NSAlert()
        alert.messageText = "Probe recommendation"
        let lines = details.map { result in
            return "\(self.upstreamApiDisplayName(result.mode)): \(self.upstreamApiProbeSummary(result))"
        }
        alert.informativeText = ([preflightDetail, ""] + lines + ["", "Recommended fallback order: \(recommendedOrder.map(upstreamApiDisplayName).joined(separator: " → "))"]).joined(separator: "\n")
        alert.addButton(withTitle: "Save Supported Protocols")
        alert.addButton(withTitle: "Keep Current Order")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            applyRecommendedProtocolOrder(
                recommendedOrder,
                request: request,
                details: details,
                preflightDetail: preflightDetail
            )
        default:
            setEditorStatus(
                "Probe complete. Current protocol order kept.",
                tooltip: ([preflightDetail] + details.map { $0.detail }).joined(separator: "\n\n")
            )
        }
    }

    func applyRecommendedProtocolOrder(
        _ recommendedOrder: [String],
        request: ModelAvailabilityProbeRequest,
        details: [UpstreamApiProbeResult],
        preflightDetail: String
    ) {
        guard modelProbeRequestStillMatches(request) else { return }
        let completeOrder = recommendedOrder
            + upstreamApiModes.filter { !recommendedOrder.contains($0) }
        guard let primary = recommendedOrder.first else { return }

        displayedUpstreamApiModes = completeOrder
        var model = providers[request.providerIndex].models[request.modelIndex]
        model.upstreamApiMode = primary
        model.supportedUpstreamApiModes = recommendedOrder
        model.litellmModel = composedLiteLLMModel(
            upstreamModel: modelUpstreamPart(model.litellmModel),
            upstreamApiMode: primary
        )
        providers[request.providerIndex].models[request.modelIndex] = model
        if selectedModelProbeKey() == request.probeKey {
            setUpstreamApiSupportCheckboxes(recommendedOrder)
        }
        persistDisplayedUpstreamApiModeOrder(
            providerIndex: request.providerIndex,
            modelIndex: request.modelIndex
        )
        refreshUpstreamApiModeRows()
        markPendingChanges()
        reloadRouteTable(preserving: (request.providerIndex, request.modelIndex))
        refreshRuntimeMap()
        setEditorStatus(
            "Probe complete. Supported protocols saved in recommended fallback order.",
            tooltip: ([preflightDetail] + details.map { $0.detail }).joined(separator: "\n\n")
        )
    }

    func applyFullProbeSelection(
        _ selected: [String],
        recommendedOrder: [String],
        request: ModelAvailabilityProbeRequest,
        details: [UpstreamApiProbeResult],
        preflightDetail: String
    ) {
        guard modelProbeRequestStillMatches(request), let primary = selected.first else { return }
        var model = providers[request.providerIndex].models[request.modelIndex]
        model.modelEnabled = true
        model.enabled = modelEffectivelyEnabled(providerIndex: request.providerIndex, model: model)
        model.upstreamApiMode = primary
        model.supportedUpstreamApiModes = selected
        model.litellmModel = composedLiteLLMModel(
            upstreamModel: modelUpstreamPart(model.litellmModel),
            upstreamApiMode: primary
        )
        providers[request.providerIndex].models[request.modelIndex] = model
        displayedUpstreamApiModes = recommendedOrder + upstreamApiModes.filter { !recommendedOrder.contains($0) }
        persistDisplayedUpstreamApiModeOrder(
            providerIndex: request.providerIndex,
            modelIndex: request.modelIndex
        )
        if selectedModelProbeKey() == request.probeKey {
            setUpstreamApiSupportCheckboxes(selected)
            enabledCheckbox.state = .on
        }
        markPendingChanges()
        reloadRouteTable(preserving: (request.providerIndex, request.modelIndex))
        refreshRuntimeMap()
        setEditorStatus("Probe complete: supported protocols saved in fallback order.", tooltip: ([preflightDetail] + details.map { $0.detail }).joined(separator: "\n\n"))
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
        let contentHeight: CGFloat = 132 + listHeight + 52
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: contentWidth, height: contentHeight),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "Choose Models to Add"
        panel.minSize = NSSize(width: 520, height: 340)
        panel.isReleasedWhenClosed = false
        panel.delegate = chooserController
        chooserController.modalWindow = panel

        let content = NSView()
        panel.contentView = content

        let titleLabel = NSTextField(labelWithString: "Choose models to add")
        titleLabel.font = NSFont.systemFont(ofSize: 16, weight: .semibold)

        let subtitleLabel = NSTextField(labelWithString: "Provider: \(providerName)    Key: \(keyName)")
        subtitleLabel.textColor = .secondaryLabelColor
        subtitleLabel.lineBreakMode = .byTruncatingMiddle

        let searchField = NSSearchField()
        searchField.placeholderString = "Search models"
        searchField.sendsSearchStringImmediately = true
        searchField.sendsWholeSearchString = false

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
        let resultCountLabel = NSTextField(labelWithString: "")
        resultCountLabel.textColor = .secondaryLabelColor
        resultCountLabel.alignment = .right
        resultCountLabel.usesSingleLineMode = true
        resultCountLabel.setContentHuggingPriority(.required, for: .horizontal)
        resultCountLabel.setContentCompressionResistancePriority(.required, for: .horizontal)
        selectionControls.addArrangedSubview(resultCountLabel)

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

        chooserController.configureControls(
            searchField: searchField,
            scrollView: scroll,
            resultCountLabel: resultCountLabel,
            selectAllButton: selectAllButton,
            invertSelectionButton: invertSelectionButton,
            addButton: addButton,
            minimumListHeight: listHeight
        )

        for view in [titleLabel, subtitleLabel, searchField, selectionControls, scroll, cancelButton, addButton] {
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

            searchField.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            searchField.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            searchField.topAnchor.constraint(equalTo: subtitleLabel.bottomAnchor, constant: 12),
            searchField.heightAnchor.constraint(equalToConstant: 28),

            selectionControls.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            selectionControls.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            selectionControls.topAnchor.constraint(equalTo: searchField.bottomAnchor, constant: 8),
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

        panel.initialFirstResponder = searchField
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
        var addedIndexes: [Int] = []
        for upstream in models {
            var model = EditableModel.blank()
            model.enabled = true
            model.modelEnabled = true
            model.modelName = upstream
            model.litellmModel = composedLiteLLMModel(
                upstreamModel: upstream,
                upstreamApiMode: defaultUpstreamApiMode
            )
            model.apiKeyName = key.name
            model.apiKey = key.value
            model.order = "1"
            model.sslVerify = ""
            model.supportsImageGeneration = false
            model.supportsImageGenerationPresent = false
            model.upstreamApiMode = defaultUpstreamApiMode
            model.supportedUpstreamApiModes = [defaultUpstreamApiMode]
            providers[providerIndex].models.append(model)
            addedIndexes.append(providers[providerIndex].models.count - 1)
            addedCount += 1
        }

        providerTableView.reloadData()
        modelTableView.reloadData()
        if addedCount > 0 {
            let lastIndex = max(0, providers[providerIndex].models.count - 1)
            let lastIdentity = modelSelectionIdentity(providerIndex: providerIndex, modelIndex: lastIndex)
            reloadRouteTable(preserving: lastIdentity)
            showModel(providerIndex: providerIndex, modelIndex: lastIndex)
            runAutomaticFullProbes(providerIndex: providerIndex, modelIndexes: addedIndexes)
        } else {
            reloadRouteTable()
        }
        refreshRuntimeMap()
        markPendingChangesIfNeeded(addedCount > 0)
        if addedCount == 0 {
            setEditorStatus("No models added to \(provider.displayName).")
        }
    }

    func runAutomaticFullProbes(providerIndex: Int, modelIndexes: [Int]) {
        let indexes = modelIndexes
        guard !indexes.isEmpty else { return }

        func run(_ position: Int) {
            guard position < indexes.count else {
                refreshModelAvailabilityProbeControlsEnabled()
                setEditorStatus("Added models: full probes complete.")
                return
            }
            let modelIndex = indexes[position]
            do {
                let probeRequest = try modelAvailabilityProbeRequest(providerIndex: providerIndex, modelIndex: modelIndex)
                let runID = UUID()
                modelAvailabilityProbeRuns[probeRequest.probeKey] = runID
                setEditorStatus("Full probe \(position + 1) of \(indexes.count): \(probeRequest.upstreamModel)...")
                runFullModelProbe(
                    request: probeRequest,
                    runID: runID,
                    automatic: true,
                    completion: { run(position + 1) }
                )
            } catch {
                setEditorStatus("Added models; automatic full probe skipped for one model: \(error.localizedDescription)")
                run(position + 1)
            }
        }
        run(0)
        refreshModelAvailabilityProbeControlsEnabled()
    }
}
