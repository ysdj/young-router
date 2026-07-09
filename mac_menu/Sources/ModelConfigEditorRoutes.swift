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
        if modes.isEmpty || !model.supportedUpstreamApiModesPresent {
            if model.supportsResponsesEndpointPresent && !model.supportsResponsesEndpoint {
                modes = ["openai/chat"]
            } else {
                modes = [normalizedUpstreamApiMode(model.upstreamApiMode)]
            }
        }
        if modes.isEmpty {
            modes = [defaultUpstreamApiMode]
        }
        return modes
    }

    func effectiveUpstreamApiMode(from modes: [String], fallback: String = "") -> String {
        let normalizedModes = normalizedUpstreamApiModes(modes)
        for preferred in effectiveUpstreamApiModePreference {
            if normalizedModes.contains(preferred) {
                return preferred
            }
        }
        if let first = normalizedModes.first {
            return first
        }
        return normalizedUpstreamApiMode(fallback)
    }

    func setUpstreamApiSupportCheckboxes(_ modes: [String]) {
        supportsOpenAIChatCheckbox.state = modes.contains("openai/chat") ? .on : .off
        supportsOpenAIResponsesCheckbox.state = modes.contains("openai/responses") ? .on : .off
        supportsAnthropicCheckbox.state = modes.contains("anthropic") ? .on : .off
    }

    func selectedSupportedUpstreamApiModes() -> [String] {
        var modes: [String] = []
        if supportsOpenAIChatCheckbox.state == .on { modes.append("openai/chat") }
        if supportsOpenAIResponsesCheckbox.state == .on { modes.append("openai/responses") }
        if supportsAnthropicCheckbox.state == .on { modes.append("anthropic") }
        if modes.isEmpty {
            modes.append(defaultUpstreamApiMode)
        }
        return modes
    }

    func refreshResponsesEndpointSupportControls() {
        let hasModel = selectedModelIndex != nil
        supportsOpenAIChatCheckbox.isEnabled = hasModel
        supportsOpenAIResponsesCheckbox.isEnabled = hasModel
        supportsAnthropicCheckbox.isEnabled = hasModel
        probeResponsesEndpointButton.toolTip = "Probe and check all API surfaces exposed by this upstream URL"
        refreshResponsesEndpointProbeControlsEnabled()
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
        var lines: [String] = []
        var runningProviders = 0
        var stoppedProviders = 0
        var runningModels = 0
        var stoppedModels = 0

        for providerIndex in providers.indices {
            let provider = providers[providerIndex]
            let providerRuns = provider.enabled && provider.models.contains { modelEffectivelyEnabled(providerIndex: providerIndex, model: $0) }
            if providerRuns {
                runningProviders += 1
            } else {
                stoppedProviders += 1
            }
            lines.append("\(providerRuns ? "RUN" : "OFF") provider \(provider.displayName)\(provider.enabled ? "" : " [disabled]")")

            for key in normalizedProviderKeys(providerIndex) {
                let keyRuns = provider.enabled && key.enabled && provider.models.contains { $0.apiKeyName == key.name && $0.modelEnabled }
                lines.append("  \(keyRuns ? "RUN" : "OFF") key \(key.displayName)\(key.enabled ? "" : " [disabled]")")
                for model in provider.models where model.apiKeyName == key.name {
                    let modelRuns = modelEffectivelyEnabled(providerIndex: providerIndex, model: model)
                    if modelRuns {
                        runningModels += 1
                    } else {
                        stoppedModels += 1
                    }
                    var reasons: [String] = []
                    if !provider.enabled { reasons.append("provider disabled") }
                    if !key.enabled { reasons.append("key disabled") }
                    if !model.modelEnabled { reasons.append("model disabled") }
                    let suffix = reasons.isEmpty ? "" : " [" + reasons.joined(separator: ", ") + "]"
                    lines.append("    \(modelRuns ? "RUN" : "OFF") model \(model.displayName) -> \(modelUpstreamPart(model.litellmModel))\(suffix)")
                }
            }

            let unassigned = provider.models.filter { model in
                !normalizedProviderKeys(providerIndex).contains { $0.name == model.apiKeyName }
            }
            for model in unassigned {
                stoppedModels += 1
                lines.append("  OFF model \(model.displayName) [missing key: \(model.apiKeyName)]")
            }
        }

        let summary = "Running providers: \(runningProviders)  Off providers: \(stoppedProviders)  Running models: \(runningModels)  Off models: \(stoppedModels)"
        appendHookFallbackMap(to: &lines)
        runtimeMapTextView?.string = ([summary, ""] + lines).joined(separator: "\n")
        scrollRuntimeMapToTop()
    }

    func appendHookFallbackMap(to lines: inout [String]) {
        let deployments = runtimeDeployments()
        let activeDeployments = deployments.filter { $0.enabled }
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

        lines.append("")
        lines.append("Routing diagram (config.yaml + litellm_menu/callbacks.py)")
        lines.append("Legend: o1/o2/o3 = config order. [headers] = hook adds browser headers.")

        if modelNames.isEmpty {
            lines.append("  no model groups")
            return
        }

        for modelName in modelNames {
            let group = (grouped[modelName] ?? []).sorted(by: runtimeDeploymentComesBefore)
            let activeGroup = group.filter { $0.enabled }
            lines.append("")
            lines.append("+-- \(modelName)")
            lines.append("|   config deployments")
            for deployment in group {
                lines.append("|   |-- \(formatRuntimeDeploymentDetail(deployment))")
            }
            if activeGroup.isEmpty {
                lines.append("|")
                lines.append("|   result: no RUN deployments for this model")
                continue
            }
            lines.append("|")
            lines.append("|   chat /v1/chat/completions")
            lines.append("|     request")
            lines.append("|       -> try config order")
            for step in orderFlowSteps(activeGroup) {
                lines.append("|          \(step)")
            }
            lines.append("|       => first upstream that succeeds")
            appendResponsesRoute(to: &lines, deployments: activeGroup, requireImageGeneration: false)
            appendResponsesRoute(to: &lines, deployments: activeGroup, requireImageGeneration: true)
        }

        let headerDeployments = activeDeployments.filter { needsBrowserCompatibleHeaders(apiBase: $0.apiBase) }
        if !headerDeployments.isEmpty {
            lines.append("")
            lines.append("Header hook")
            lines.append("  headers.example calls get browser-like headers before upstream:")
            for deployment in headerDeployments.sorted(by: runtimeDeploymentComesBefore) {
                lines.append("  - \(formatRuntimeDeployment(deployment, includeStatus: false, includeOrder: true))")
            }
        }
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
        guard let textView = runtimeMapTextView else { return }
        DispatchQueue.main.async { [weak self, weak textView] in
            guard let textView else { return }
            textView.scrollToBeginningOfDocument(nil)
            if let scrollView = self?.runtimeMapScrollView {
                scrollView.reflectScrolledClipView(scrollView.contentView)
            }
        }
    }

    func scrollTableToTop(_ tableView: NSTableView) {
        guard tableView.numberOfRows > 0 else { return }
        DispatchQueue.main.async { [weak tableView] in
            guard let tableView, tableView.numberOfRows > 0 else { return }
            tableView.scrollRowToVisible(0)
        }
    }

    func appendResponsesRoute(to lines: inout [String], deployments: [RuntimeDeployment], requireImageGeneration: Bool) {
        let title = requireImageGeneration ? "responses + image_generation" : "responses /v1/responses"
        lines.append("|")
        lines.append("|   \(title)")
        lines.append("|     request")

        let candidates = deployments
        if requireImageGeneration {
            lines.append("|       -> keep normal RUN list")
            lines.append("|       -> runtime fallback can force tool_choice=image_generation after an unsupported/empty response")
        }

        let browserCompatible = candidates.filter { needsBrowserCompatibleHeaders(apiBase: $0.apiBase) }
        if browserCompatible.isEmpty {
            lines.append("|       -> no headers.example candidate")
            lines.append("|       -> fall back to config order")
            for step in orderFlowSteps(candidates) {
                lines.append("|          \(step)")
            }
            return
        }

        let selected = lowestOrderDeployments(browserCompatible)
        let selectedIds = Set(selected.map { $0.id })
        let skipped = candidates.filter { !selectedIds.contains($0.id) }
        lines.append("|       -> hook keeps headers.example only")
        for deployment in browserCompatible.sorted(by: runtimeDeploymentComesBefore) {
            lines.append("|          \(formatRuntimeDeployment(deployment, includeStatus: false, includeOrder: true))")
        }
        lines.append("|       -> lowest order wins")
        lines.append("|       => final pick: \(formatRuntimeDeployments(selected, includeStatus: false))")
        if !skipped.isEmpty {
            lines.append("|          skipped: \(formatRuntimeDeployments(skipped, includeStatus: false))")
        }
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
                    upstreamApiMode: normalizedUpstreamApiMode(for: model),
                    supportedUpstreamApiModes: normalizedSupportedUpstreamApiModes(for: model),
                    supportsResponsesEndpoint: normalizedSupportedUpstreamApiModes(for: model).contains("openai/responses")
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

    func orderFallbackDescription(_ deployments: [RuntimeDeployment]) -> String {
        let grouped = Dictionary(grouping: deployments) { $0.order }
        let orders = grouped.keys.sorted { orderSortValue($0) < orderSortValue($1) }
        return orders.map { order in
            let orderLabel = order.map { "o\($0)" } ?? "o-"
            let deploymentsForOrder = (grouped[order] ?? []).sorted(by: runtimeDeploymentComesBefore)
            return "\(orderLabel) \(formatRuntimeDeployments(deploymentsForOrder, includeStatus: false, includeOrder: false))"
        }.joined(separator: " -> ")
    }

    func orderFlowSteps(_ deployments: [RuntimeDeployment]) -> [String] {
        let grouped = Dictionary(grouping: deployments) { $0.order }
        let orders = grouped.keys.sorted { orderSortValue($0) < orderSortValue($1) }
        return orders.enumerated().map { index, order in
            let prefix = index == 0 ? "start" : "if previous fails"
            let orderLabel = order.map { "o\($0)" } ?? "o-"
            let deploymentsForOrder = (grouped[order] ?? []).sorted(by: runtimeDeploymentComesBefore)
            return "\(prefix) -> \(orderLabel) \(formatRuntimeDeployments(deploymentsForOrder, includeStatus: false, includeOrder: false))"
        }
    }

    func lowestOrderDeployments(_ deployments: [RuntimeDeployment]) -> [RuntimeDeployment] {
        let orders = deployments.compactMap { $0.order }
        guard let minOrder = orders.min() else {
            return deployments.sorted(by: runtimeDeploymentComesBefore)
        }
        return deployments
            .filter { $0.order == minOrder }
            .sorted(by: runtimeDeploymentComesBefore)
    }

    func formatRuntimeDeployments(_ deployments: [RuntimeDeployment], includeStatus: Bool, includeOrder: Bool = true) -> String {
        let sorted = deployments.sorted(by: runtimeDeploymentComesBefore)
        if sorted.isEmpty {
            return "none"
        }
        return sorted.map { formatRuntimeDeployment($0, includeStatus: includeStatus, includeOrder: includeOrder) }.joined(separator: " | ")
    }

    func formatRuntimeDeployment(_ deployment: RuntimeDeployment, includeStatus: Bool, includeOrder: Bool) -> String {
        var parts: [String] = []
        if includeOrder {
            parts.append(deployment.order.map { "o\($0)" } ?? "o-")
        }
        parts.append("\(deployment.providerName)/\(deployment.keyName)")
        if !deployment.upstreamModel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            parts.append(deployment.upstreamModel)
        }

        var flags: [String] = []
        if needsBrowserCompatibleHeaders(apiBase: deployment.apiBase) {
            flags.append("browser-headers")
        }
        if deployment.supportsImageGeneration {
            flags.append("image")
        }
        flags.append("use \(deployment.upstreamApiMode)")
        if deployment.supportedUpstreamApiModes.count > 1 {
            flags.append("supports \(deployment.supportedUpstreamApiModes.joined(separator: "+"))")
        }
        if includeStatus, !deployment.enabled {
            flags.append("off: \(runtimeDeploymentOffReason(deployment))")
        }
        let suffix = flags.isEmpty ? "" : " [" + flags.joined(separator: ", ") + "]"
        return parts.joined(separator: " ") + suffix
    }

    func formatRuntimeDeploymentDetail(_ deployment: RuntimeDeployment) -> String {
        let status = deployment.enabled ? "RUN" : "OFF"
        let order = deployment.order.map { "o\($0)" } ?? "o-"
        let upstream = deployment.upstreamModel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? "(no upstream model)"
            : deployment.upstreamModel
        let host = apiBaseHost(deployment.apiBase)
        let hostText = host.isEmpty ? "(no base URL)" : host
        var flags: [String] = []
        if needsBrowserCompatibleHeaders(apiBase: deployment.apiBase) {
            flags.append("hook adds headers")
        }
        if deployment.supportsImageGeneration {
            flags.append("image-capable")
        }
        flags.append("use \(deployment.upstreamApiMode)")
        if deployment.supportedUpstreamApiModes.count > 1 {
            flags.append("supports \(deployment.supportedUpstreamApiModes.joined(separator: "+"))")
        }
        if !deployment.enabled {
            flags.append("off: \(runtimeDeploymentOffReason(deployment))")
        }
        let suffix = flags.isEmpty ? "" : " [" + flags.joined(separator: ", ") + "]"
        return "\(status) \(order) \(deployment.providerName)/\(deployment.keyName) -> \(upstream) @ \(hostText)\(suffix)"
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
