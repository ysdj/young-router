import Cocoa

extension ModelConfigEditorController {
    func controlTextDidChange(_ obj: Notification) {
        if isRenderingSelection {
            return
        }
        markProviderEditorDirty(for: obj.object)
        if let field = obj.object as? NSTextField, field === providerApiBaseField {
            autofillProviderNameFromBaseURL()
        }
        synchronizeLiveEditorDraft()
        markPendingChanges()
    }

    func synchronizeLiveEditorDraft() {
        let wasRenderingSelection = isRenderingSelection
        isRenderingSelection = true
        commitEditor()
        isRenderingSelection = wasRenderingSelection
    }

    func controlTextDidEndEditing(_ obj: Notification) {
        if isRenderingSelection {
            return
        }
        if let field = obj.object as? NSTextField,
           field === providerApiBaseField || field === providerNameField {
            providerNameAutofillProviderID = nil
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
            return
        }

        reloadSelectionTablesPreserving(
            providerIndex: currentProviderIndex,
            modelIndex: currentModelIndex,
            providerKeyIndex: currentProviderKeyIndex
        )
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
            setEditorStatus("At least one upstream protocol is required.")
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
            refreshProviderBilling()
            let runID = UUID()
            modelAvailabilityProbeRuns[request.probeKey] = runID
            setModelProbePresentation(
                ModelProbePresentation(
                    state: .probing,
                    summary: "Probing...",
                    detail: "Checking model info and supported upstream APIs."
                ),
                for: request
            )
            refreshModelAvailabilityProbeControlsEnabled()
            runFullModelProbe(request: request, runID: runID, automatic: false)
        } catch {
            setEditorError("Model probe failed", message: error.localizedDescription)
        }
    }

    func recommendedUpstreamApiModes(
        from results: [UpstreamApiProbeResult],
        modelIdentifier: String
    ) -> [String] {
        probeProtocolRecommendation(
            priority: probeProtocolPriority(
                modelIdentifier: modelIdentifier,
                defaultPriority: upstreamApiModes
            ),
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

    func presentProbeStatus(
        _ summary: String,
        detail: String,
        state: ModelProbePresentation.State,
        for request: ModelAvailabilityProbeRequest
    ) {
        setModelProbePresentation(
            ModelProbePresentation(state: state, summary: summary, detail: detail),
            for: request
        )
    }

    func runFullModelProbe(
        request: ModelAvailabilityProbeRequest,
        runID: UUID,
        automatic: Bool,
        completion: (() -> Void)? = nil
    ) {
        presentProbeStatus(
            "Probing \(request.upstreamModel)...",
            detail: "Checking model info and supported upstream APIs.",
            state: .probing,
            for: request
        )
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
        reloadProbePresentationRows(for: request)
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
                let recommended = self.recommendedUpstreamApiModes(
                    from: results,
                    modelIdentifier: request.upstreamModel
                )
                guard !recommended.isEmpty else {
                    let detail = ([preflightDetail] + results.map { $0.detail }).joined(separator: "\n\n")
                    let hasInconclusive = results.contains { $0.availability == .inconclusive }
                    if !hasInconclusive {
                        self.runFullImageModelProbe(
                            request: request,
                            runID: runID,
                            preflightDetail: detail,
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
                    self.applyRecommendedProtocolOrder(
                        recommended,
                        request: request,
                        details: results,
                        preflightDetail: preflightDetail
                    )
                } else {
                    let currentModes = self.normalizedSupportedUpstreamApiModes(
                        for: self.providers[request.providerIndex].models[request.modelIndex]
                    )
                    if currentModes == recommended {
                        self.presentProbeStatus(
                            "Available",
                            detail: ([preflightDetail] + results.map { $0.detail }).joined(separator: "\n\n"),
                            state: .available,
                            for: request
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
            presentProbeStatus(
                "Probe failed",
                detail: error.localizedDescription,
                state: .inconclusive,
                for: request
            )
            finishFullModelProbe(request: request, completion: completion)
        }
    }

    func runFullImageModelProbe(
        request: ModelAvailabilityProbeRequest,
        runID: UUID,
        preflightDetail: String,
        completion: (() -> Void)?
    ) {
        do {
            let body = try modelAvailabilityImageGenerationProbeBody(model: request.upstreamModel)
            postJSONProbe(urls: request.imageGenerationURLs, apiKey: request.apiKey, apiBase: request.apiBase, body: body, timeout: 60) { [weak self] url, response, data, error in
                guard let self else { return }
                guard self.modelAvailabilityProbeRuns[request.probeKey] == runID else { return }
                let detail = error.map { "\(url.absoluteString)\n\($0.localizedDescription)" }
                    ?? response.map { self.probeDetail(surface: "image generation", url: url, statusCode: $0.statusCode, data: data) }
                    ?? "\(url.absoluteString)\nNo HTTP response returned."
                let outcome = response.map { self.parseModelAvailabilityImageGenerationProbeOutcome(statusCode: $0.statusCode, data: data) } ?? .inconclusive(detail)
                self.applyModelAvailabilityProbeOutcome(self.probeOutcome(outcome, prefixing: "\(preflightDetail)\n\n\(detail)"), request: request)
                self.finishFullModelProbe(request: request, completion: completion)
            }
        } catch {
            applyModelAvailabilityProbeOutcome(.inconclusive("\(preflightDetail)\n\n/images/generations probe failed: \(error.localizedDescription)"), request: request)
            finishFullModelProbe(request: request, completion: completion)
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
        alert.informativeText = ([preflightDetail, ""] + lines + ["", "Recommended protocol: \(recommendedOrder.map(upstreamApiDisplayName).joined(separator: " → "))"]).joined(separator: "\n")
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Keep")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            applyRecommendedProtocolOrder(
                recommendedOrder,
                request: request,
                details: details,
                preflightDetail: preflightDetail
            )
        default:
            presentProbeStatus(
                "Available",
                detail: ([preflightDetail] + details.map { $0.detail }).joined(separator: "\n\n"),
                state: .available,
                for: request
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

        var model = providers[request.providerIndex].models[request.modelIndex]
        model.upstreamApiMode = primary
        model.supportedUpstreamApiModes = recommendedOrder
        model.litellmModel = composedLiteLLMModel(
            upstreamModel: modelUpstreamPart(model.litellmModel),
            upstreamApiMode: primary
        )
        model.modelInfoExtra[upstreamApiModeOrderMetadataKey] = .array(
            completeOrder.map { .string($0) }
        )
        providers[request.providerIndex].models[request.modelIndex] = model
        if selectedModelProbeKey() == request.probeKey {
            displayedUpstreamApiModes = completeOrder
            setUpstreamApiSupportCheckboxes(recommendedOrder)
        }
        refreshUpstreamApiModeRows()
        markPendingChanges()
        reloadRouteTable(preserving: (request.providerIndex, request.modelIndex))
        presentProbeStatus(
            "Available",
            detail: ([preflightDetail] + details.map { $0.detail }).joined(separator: "\n\n"),
            state: .available,
            for: request
        )
    }

    func setModelCandidateFetchState(_ loading: Bool) {
        modelCandidateFetchInFlight = loading
        if loading {
            fetchModelsButton.title = "Fetching…"
        } else {
            fetchModelsButton.title = "Fetch"
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

        switch outcome {
        case .available(let detail):
            let model = providers[request.providerIndex].models[request.modelIndex]
            if modelEffectivelyEnabled(providerIndex: request.providerIndex, model: model) {
                presentProbeStatus("Available", detail: detail, state: .available, for: request)
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
                presentProbeStatus(
                    "Available, disabled: \(reason)",
                    detail: detail,
                    state: .available,
                    for: request
                )
            }
        case .unavailable(let detail):
            let inlineDetail = inlineProbeFailureDetail(from: detail)
            presentProbeStatus(
                inlineDetail.isEmpty ? "Unavailable" : "Unavailable: \(inlineDetail)",
                detail: detail,
                state: .unavailable,
                for: request
            )
        case .inconclusive(let detail):
            let inlineDetail = inlineProbeFailureDetail(from: detail)
            presentProbeStatus(
                inlineDetail.isEmpty ? "Uncertain" : "Uncertain: \(inlineDetail)",
                detail: detail,
                state: .inconclusive,
                for: request
            )
        }

        if selectedProviderIndex == request.providerIndex {
            modelTableView.reloadData(forRowIndexes: IndexSet(integer: request.modelIndex), columnIndexes: IndexSet(integersIn: 0..<modelTableView.numberOfColumns))
        }
        reloadRouteTable(preserving: (request.providerIndex, request.modelIndex))
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
        let selectAllButton = textButton(title: "All", toolTip: "Select all visible models", accessibilityLabel: "Select all visible models")
        selectAllButton.target = chooserController
        selectAllButton.action = #selector(FetchedModelChooserController.selectAllAction(_:))
        let invertSelectionButton = textButton(title: "Invert", toolTip: "Invert visible model selection", accessibilityLabel: "Invert visible model selection")
        invertSelectionButton.target = chooserController
        invertSelectionButton.action = #selector(FetchedModelChooserController.invertSelectionAction(_:))
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
        let addButton = textButton(title: "+", toolTip: "Add selected models", accessibilityLabel: "Add selected models")
        addButton.target = chooserController
        addButton.action = #selector(FetchedModelChooserController.addSelectedAction(_:))
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
            let initialApiMode = inferredPreferredUpstreamApiMode(
                modelIdentifier: upstream,
                defaultMode: defaultUpstreamApiMode
            )
            var model = EditableModel.blank()
            model.enabled = true
            model.modelEnabled = true
            model.modelName = upstream
            model.litellmModel = composedLiteLLMModel(
                upstreamModel: upstream,
                upstreamApiMode: initialApiMode
            )
            model.apiKeyName = key.name
            model.apiKey = key.value
            model.order = "1"
            model.sslVerify = ""
            model.supportsImageGeneration = false
            model.supportsImageGenerationPresent = false
            model.upstreamApiMode = initialApiMode
            model.supportedUpstreamApiModes = [initialApiMode]
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
                setModelProbePresentation(
                    ModelProbePresentation(
                        state: .probing,
                        summary: "Probing...",
                        detail: "Automatic probe \(position + 1) of \(indexes.count)."
                    ),
                    for: probeRequest
                )
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
