import Cocoa

extension ModelConfigEditorController {
    func needsBrowserCompatibleHeaders(apiBase: String) -> Bool {
        let host = apiBaseHost(apiBase)
        return browserCompatibleHeaderHosts.contains { allowed in
            host == allowed || host.hasSuffix(".\(allowed)")
        }
    }

    func applyBrowserCompatibleHeadersIfNeeded(to request: inout URLRequest, apiBase: String) {
        guard needsBrowserCompatibleHeaders(apiBase: apiBase) else { return }
        for (header, value) in browserCompatibleHeaders {
            request.setValue(value, forHTTPHeaderField: header)
        }
    }

    func postJSONProbe(
        urls: [URL],
        apiKey: String,
        apiBase: String,
        body: Data,
        timeout: TimeInterval,
        retryStatusCodes: Set<Int> = [404, 405],
        extraHeaders: [String: String] = [:],
        completion: @escaping (URL, HTTPURLResponse?, Data?, Error?) -> Void
    ) {
        guard let firstURL = urls.first else {
            completion(
                URL(string: "about:blank")!,
                nil,
                nil,
                ConfigEditorError(message: "No probe URL could be built.")
            )
            return
        }

        func attempt(_ index: Int) {
            let url = urls.indices.contains(index) ? urls[index] : firstURL
            var urlRequest = URLRequest(url: url)
            urlRequest.httpMethod = "POST"
            urlRequest.timeoutInterval = timeout
            urlRequest.setValue("application/json", forHTTPHeaderField: "Accept")
            urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
            urlRequest.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
            for (header, value) in extraHeaders {
                urlRequest.setValue(value, forHTTPHeaderField: header)
            }
            urlRequest.httpBody = body
            self.applyBrowserCompatibleHeadersIfNeeded(to: &urlRequest, apiBase: apiBase)

            URLSession.shared.dataTask(with: urlRequest) { data, response, error in
                DispatchQueue.main.async {
                    let httpResponse = response as? HTTPURLResponse
                    let hasNext = index + 1 < urls.count
                    if hasNext {
                        if let statusCode = httpResponse?.statusCode,
                           retryStatusCodes.contains(statusCode) {
                            attempt(index + 1)
                            return
                        }
                        if error != nil {
                            attempt(index + 1)
                            return
                        }
                    }
                    completion(url, httpResponse, data, error)
                }
            }.resume()
        }

        attempt(0)
    }

    func getJSONCandidate(
        urls: [URL],
        apiKey: String?,
        timeout: TimeInterval,
        retryStatusCodes: Set<Int> = [404, 405],
        completion: @escaping (URL, HTTPURLResponse?, Data?, Error?) -> Void
    ) {
        guard let firstURL = urls.first else {
            completion(
                URL(string: "about:blank")!,
                nil,
                nil,
                ConfigEditorError(message: "No request URL could be built.")
            )
            return
        }

        func attempt(_ index: Int) {
            let url = urls.indices.contains(index) ? urls[index] : firstURL
            var urlRequest = URLRequest(url: url)
            urlRequest.httpMethod = "GET"
            urlRequest.timeoutInterval = timeout
            urlRequest.setValue("application/json", forHTTPHeaderField: "Accept")
            if let apiKey, !apiKey.isEmpty {
                urlRequest.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
            }
            self.applyBrowserCompatibleHeadersIfNeeded(to: &urlRequest, apiBase: url.absoluteString)

            URLSession.shared.dataTask(with: urlRequest) { data, response, error in
                DispatchQueue.main.async {
                    let httpResponse = response as? HTTPURLResponse
                    let hasNext = index + 1 < urls.count
                    if hasNext {
                        if let statusCode = httpResponse?.statusCode,
                           retryStatusCodes.contains(statusCode) {
                            attempt(index + 1)
                            return
                        }
                        if error != nil {
                            attempt(index + 1)
                            return
                        }
                    }
                    completion(url, httpResponse, data, error)
                }
            }.resume()
        }

        attempt(0)
    }

    func apiBaseHost(_ apiBase: String) -> String {
        let trimmed = apiBase.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        let urlString = trimmed.contains("://") ? trimmed : "https://\(trimmed)"
        return URL(string: urlString)?.host?.lowercased() ?? ""
    }

    func localLiteLLMModelInfoRequest() -> URLRequest {
        var request = URLRequest(url: localLiteLLMModelInfoURL)
        request.httpMethod = "GET"
        request.timeoutInterval = 12
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(localLiteLLMMasterKey)", forHTTPHeaderField: "Authorization")
        return request
    }

    func fetchLiteLLMModelInfoCapability(
        lookup: LiteLLMModelInfoLookup,
        completion: @escaping (Result<LiteLLMModelInfoCapability?, ConfigEditorError>) -> Void
    ) {
        URLSession.shared.dataTask(with: localLiteLLMModelInfoRequest()) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                if let error {
                    completion(.failure(ConfigEditorError(message: error.localizedDescription)))
                    return
                }
                guard let httpResponse = response as? HTTPURLResponse else {
                    completion(.failure(ConfigEditorError(message: "No HTTP response returned from LiteLLM /model/info.")))
                    return
                }
                do {
                    let capability = try self.parseLiteLLMModelInfoCapability(
                        statusCode: httpResponse.statusCode,
                        data: data,
                        lookup: lookup
                    )
                    completion(.success(capability))
                } catch {
                    completion(.failure(ConfigEditorError(message: error.localizedDescription)))
                }
            }
        }.resume()
    }

    func parseLiteLLMModelInfoCapability(
        statusCode: Int,
        data: Data?,
        lookup: LiteLLMModelInfoLookup
    ) throws -> LiteLLMModelInfoCapability? {
        guard (200...299).contains(statusCode), let data else {
            let snippet = responseSnippet(data)
            throw ConfigEditorError(message: snippet.isEmpty ? "HTTP \(statusCode)" : "HTTP \(statusCode)\n\(snippet)")
        }
        let object = try JSONSerialization.jsonObject(with: data)
        let rows = liteLLMModelInfoRows(from: object)
        var best: (score: Int, capability: LiteLLMModelInfoCapability)?
        for row in rows {
            guard let scored = liteLLMModelInfoCapability(from: row, lookup: lookup) else {
                continue
            }
            if best == nil || scored.score > best!.score {
                best = scored
            }
        }
        return best?.capability
    }

    func liteLLMModelInfoRows(from value: Any) -> [[String: Any]] {
        if let dict = value as? [String: Any] {
            if let data = dict["data"] {
                return liteLLMModelInfoRows(from: data)
            }
            if dict["model_info"] != nil || dict["litellm_params"] != nil || dict["model_name"] != nil {
                return [dict]
            }
        }
        if let array = value as? [Any] {
            return array.flatMap { liteLLMModelInfoRows(from: $0) }
        }
        return []
    }

    func liteLLMModelInfoCapability(
        from row: [String: Any],
        lookup: LiteLLMModelInfoLookup
    ) -> (score: Int, capability: LiteLLMModelInfoCapability)? {
        let modelInfo = row["model_info"] as? [String: Any] ?? [:]
        let params = row["litellm_params"] as? [String: Any] ?? [:]
        let id = jsonString(modelInfo["id"])
        let modelName = jsonString(row["model_name"])
        let litellmModel = jsonString(params["model"])
        let apiBase = jsonString(params["api_base"])
        let mode = jsonString(modelInfo["mode"]).lowercased()
        let upstreamApiMode = jsonString(modelInfo["upstream_url_surface"])
        let supportsFlag = jsonBool(modelInfo["supports_responses_image_generation_tool"])
        let supportsResponsesEndpointFlag = jsonBool(modelInfo["supports_responses_endpoint"])
        let provider = jsonString(modelInfo["provider"])
        let key = jsonString(modelInfo["key"])

        let idMatches = !lookup.deploymentToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && valuesMatch(id, lookup.deploymentToken)
        let publicMatches = valuesMatch(modelName, lookup.publicModel)
        let litellmMatches = valuesMatch(litellmModel, lookup.litellmModel)
            || valuesMatch(modelUpstreamPart(litellmModel), lookup.upstreamModel)
        let apiBaseMatches = urlsMatch(apiBase, lookup.apiBase)

        let score: Int
        let matchedBy: String
        if idMatches {
            score = 100
            matchedBy = "deployment token"
        } else if publicMatches && litellmMatches && apiBaseMatches {
            score = 90
            matchedBy = "model_name + litellm_params.model + api_base"
        } else if publicMatches && litellmMatches {
            score = 80
            matchedBy = "model_name + litellm_params.model"
        } else if litellmMatches && apiBaseMatches {
            score = 70
            matchedBy = "litellm_params.model + api_base"
        } else if publicMatches && apiBaseMatches {
            score = 60
            matchedBy = "model_name + api_base"
        } else {
            return nil
        }

        return (
            score,
            LiteLLMModelInfoCapability(
                id: id,
                modelName: modelName,
                litellmModel: litellmModel,
                apiBase: apiBase,
                mode: mode,
                upstreamApiMode: upstreamApiMode,
                supportsImageGenerationFlag: supportsFlag,
                supportsResponsesEndpointFlag: supportsResponsesEndpointFlag,
                provider: provider,
                key: key,
                matchedBy: matchedBy
            )
        )
    }

    func missingLiteLLMModelInfoMessage(lookup: LiteLLMModelInfoLookup) -> String {
        let facts = [
            "model_name=\(lookup.publicModel)",
            "litellm_params.model=\(lookup.litellmModel)",
            "api_base=\(lookup.apiBase)",
            "deployment_token=\(lookup.deploymentToken.isEmpty ? "(blank)" : lookup.deploymentToken)",
        ]
        return "LiteLLM /model/info did not return a matching deployment. " + facts.joined(separator: ", ")
    }

    func jsonString(_ value: Any?) -> String {
        if let string = value as? String {
            return string.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if let number = value as? NSNumber {
            return number.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return ""
    }

    func jsonBool(_ value: Any?) -> Bool? {
        if let bool = value as? Bool {
            return bool
        }
        if let number = value as? NSNumber {
            return number.boolValue
        }
        if let string = value as? String {
            let normalized = string.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if ["1", "true", "yes", "on"].contains(normalized) { return true }
            if ["0", "false", "no", "off"].contains(normalized) { return false }
        }
        return nil
    }

    func normalizedMatchValue(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    func normalizedURLMatchValue(_ value: String) -> String {
        normalizedMatchValue(value).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    func valuesMatch(_ left: String, _ right: String) -> Bool {
        let normalizedLeft = normalizedMatchValue(left)
        let normalizedRight = normalizedMatchValue(right)
        return !normalizedLeft.isEmpty && normalizedLeft == normalizedRight
    }

    func urlsMatch(_ left: String, _ right: String) -> Bool {
        let normalizedLeft = normalizedURLMatchValue(left)
        let normalizedRight = normalizedURLMatchValue(right)
        return !normalizedLeft.isEmpty && normalizedLeft == normalizedRight
    }

    func refreshModelApiKeyPopup(providerIndex: Int, selected: String) {
        let keys = normalizedProviderKeys(providerIndex)
        modelApiKeyPopupButton.removeAllItems()
        for key in keys {
            modelApiKeyPopupButton.addItem(withTitle: key.displayName)
        }

        let selectedName = selected.trimmingCharacters(in: .whitespacesAndNewlines)
        if !selectedName.isEmpty, keys.contains(where: { $0.name == selectedName }) {
            modelApiKeyPopupButton.selectItem(withTitle: selectedName)
        } else if let first = keys.first {
            modelApiKeyPopupButton.selectItem(withTitle: first.displayName)
        }
        modelApiKeyPopupButton.isEnabled = selectedModelIndex != nil && !keys.isEmpty
    }

    func tokenPreview(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "(no token)" }
        guard trimmed.count > 12 else { return trimmed }
        return "\(trimmed.prefix(6))...\(trimmed.suffix(4))"
    }

    func modelCandidateKeyTitle(_ key: EditableProviderKey) -> String {
        let disabledSuffix = key.enabled ? "" : " [off]"
        return "\(key.displayName) / \(tokenPreview(key.value))\(disabledSuffix)"
    }

    func selectedModelCandidateKeyName() -> String {
        (modelCandidateApiKeyPopupButton.selectedItem?.representedObject as? String) ?? emptyModelCandidateKeyName
    }

    func refreshModelCandidateApiKeyPopup(providerIndex: Int?, selected: String = "") {
        let selectedName = selected.trimmingCharacters(in: .whitespacesAndNewlines)
        modelCandidateApiKeyPopupButton.removeAllItems()

        modelCandidateApiKeyPopupButton.addItem(withTitle: emptyModelCandidateKeyTitle)
        modelCandidateApiKeyPopupButton.lastItem?.representedObject = emptyModelCandidateKeyName

        if let providerIndex, providerIndex >= 0, providerIndex < providers.count {
            for key in normalizedProviderKeys(providerIndex) {
                modelCandidateApiKeyPopupButton.addItem(withTitle: modelCandidateKeyTitle(key))
                modelCandidateApiKeyPopupButton.lastItem?.representedObject = key.name
            }
        }

        if !selectedName.isEmpty,
           let item = modelCandidateApiKeyPopupButton.itemArray.first(where: { ($0.representedObject as? String) == selectedName }) {
            modelCandidateApiKeyPopupButton.select(item)
        } else {
            modelCandidateApiKeyPopupButton.selectItem(at: 0)
        }
        refreshModelCandidateControlsEnabled()
    }

    func refreshModelCandidateControlsEnabled() {
        fetchModelsButton.isEnabled = selectedProviderIndex != nil && !modelCandidateFetchInFlight
        modelCandidateApiKeyPopupButton.isEnabled = selectedProviderIndex != nil && !modelCandidateFetchInFlight
    }

    func refreshModelAvailabilityProbeControlsEnabled() {
        let inFlight = selectedModelProbeKey().map { modelAvailabilityProbeRuns[$0] != nil } ?? false
        probeModelAvailabilityButton.title = inFlight ? "Probing..." : "Probe Model"
        probeModelAvailabilityButton.isEnabled = selectedModelIndex != nil && !inFlight
    }

    func refreshResponsesEndpointProbeControlsEnabled() {
        let inFlight = selectedModelProbeKey().map { responsesEndpointProbeRuns[$0] != nil } ?? false
        probeResponsesEndpointButton.title = inFlight ? "Probing..." : "Probe URL"
        probeResponsesEndpointButton.isEnabled = selectedModelIndex != nil && !inFlight
    }

    func selectedModelProbeKey() -> ModelProbeKey? {
        guard let providerIndex = selectedProviderIndex,
              let modelIndex = selectedModelIndex,
              providerIndex >= 0,
              providerIndex < providers.count,
              modelIndex >= 0,
              modelIndex < providers[providerIndex].models.count else {
            return nil
        }
        return ModelProbeKey(
            providerID: providers[providerIndex].editorID,
            modelID: providers[providerIndex].models[modelIndex].editorID
        )
    }

    func providerKeyForModelProbe(providerIndex: Int, model: EditableModel) -> EditableProviderKey? {
        let keys = normalizedProviderKeys(providerIndex)
        let modelKeyName = model.apiKeyName.trimmingCharacters(in: .whitespacesAndNewlines)
        return keys.first(where: { $0.name == modelKeyName })
            ?? keys.first(where: { $0.enabled })
            ?? keys.first
    }

    func modelProbeRequestStillMatches(_ request: ModelAvailabilityProbeRequest) -> Bool {
        guard request.providerIndex >= 0,
              request.providerIndex < providers.count,
              providers[request.providerIndex].editorID == request.providerEditorID,
              request.modelIndex >= 0,
              request.modelIndex < providers[request.providerIndex].models.count,
              providers[request.providerIndex].models[request.modelIndex].editorID == request.modelEditorID else {
            return false
        }

        let model = providers[request.providerIndex].models[request.modelIndex]
        let key = providerKeyForModelProbe(providerIndex: request.providerIndex, model: model)
        return routePublicModelName(model) == request.publicModel
            && model.litellmModel.trimmingCharacters(in: .whitespacesAndNewlines) == request.litellmModel
            && modelUpstreamPart(model.litellmModel).trimmingCharacters(in: .whitespacesAndNewlines) == request.upstreamModel
            && modelEffectiveAPIBase(providerIndex: request.providerIndex, model: model) == request.apiBase.trimmingCharacters(in: .whitespacesAndNewlines)
            && model.deploymentToken.trimmingCharacters(in: .whitespacesAndNewlines) == request.deploymentToken
            && key?.name == request.keyName
            && key?.value == request.apiKey
    }

    func currentModelCandidateRequest() throws -> ModelCandidateRequest {
        guard let providerIndex = selectedProviderIndex else {
            throw ConfigEditorError(message: "Select a provider before fetching candidates.")
        }
        let provider = providers[providerIndex]
        let baseURL = provider.apiBase.trimmingCharacters(in: .whitespacesAndNewlines)
        if baseURL.isEmpty {
            throw ConfigEditorError(message: "Provider \(provider.displayName) has no Base URL.")
        }
        let keyName = selectedModelCandidateKeyName().trimmingCharacters(in: .whitespacesAndNewlines)
        let key = keyName.isEmpty ? nil : normalizedProviderKeys(providerIndex).first(where: { $0.name == keyName })
        if !keyName.isEmpty, key == nil {
            throw ConfigEditorError(message: "Provider key \(keyName) was not found.")
        }
        let apiKey = key?.value.trimmingCharacters(in: .whitespacesAndNewlines)
        if let key, apiKey?.isEmpty != false {
            throw ConfigEditorError(message: "Provider key \(key.displayName) has no token.")
        }
        let adapter = selectedModelIndex.map {
            splitLiteLLMModel(providers[providerIndex].models[$0].litellmModel).0
        } ?? "openai"
        let urls = endpointURLCandidates(
            baseURL: baseURL.trimmingCharacters(in: CharacterSet(charactersIn: "/")),
            endpoint: "models"
        )
        guard !urls.isEmpty else {
            throw ConfigEditorError(message: "Invalid /v1/models URL for Base URL: \(baseURL)")
        }
        return ModelCandidateRequest(
            providerIndex: providerIndex,
            providerEditorID: provider.editorID,
            keyEditorID: key?.editorID,
            keyName: key?.name ?? emptyModelCandidateKeyName,
            keyDisplayName: key.map { modelCandidateKeyTitle($0) } ?? emptyModelCandidateKeyTitle,
            adapter: adapter.isEmpty ? "openai" : adapter,
            urls: urls,
            apiKey: apiKey
        )
    }

    func currentModelAvailabilityProbeRequest() throws -> ModelAvailabilityProbeRequest {
        guard let providerIndex = selectedProviderIndex,
              let modelIndex = selectedModelIndex else {
            throw ConfigEditorError(message: "Select a model deployment before probing availability.")
        }
        let provider = providers[providerIndex]
        let model = providers[providerIndex].models[modelIndex]
        let modelBaseURL = model.apiBase.trimmingCharacters(in: .whitespacesAndNewlines)
        let baseURL = modelBaseURL.isEmpty ? provider.apiBase.trimmingCharacters(in: .whitespacesAndNewlines) : modelBaseURL
        if baseURL.isEmpty {
            throw ConfigEditorError(message: "Provider \(provider.displayName) has no Base URL.")
        }

        let upstreamModel = modelUpstreamPart(model.litellmModel).trimmingCharacters(in: .whitespacesAndNewlines)
        if upstreamModel.isEmpty {
            throw ConfigEditorError(message: "Selected model has no upstream model.")
        }

        let key = providerKeyForModelProbe(providerIndex: providerIndex, model: model)
        guard let key else {
            throw ConfigEditorError(message: "Provider \(provider.displayName) has no API key.")
        }
        if key.value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw ConfigEditorError(message: "Provider key \(key.displayName) has no token.")
        }

        let normalizedBaseURL = baseURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let chatURLs = endpointURLCandidates(baseURL: normalizedBaseURL, endpoint: "chat/completions")
        guard !chatURLs.isEmpty else {
            throw ConfigEditorError(message: "Invalid /v1/chat/completions URL for Base URL: \(baseURL)")
        }
        let responsesURLs = endpointURLCandidates(baseURL: normalizedBaseURL, endpoint: "responses")
        guard !responsesURLs.isEmpty else {
            throw ConfigEditorError(message: "Invalid /v1/responses URL for Base URL: \(baseURL)")
        }
        let anthropicURLs = endpointURLCandidates(baseURL: normalizedBaseURL, endpoint: "messages")
        guard !anthropicURLs.isEmpty else {
            throw ConfigEditorError(message: "Invalid /v1/messages URL for Base URL: \(baseURL)")
        }
        let imageGenerationURLs = endpointURLCandidates(baseURL: normalizedBaseURL, endpoint: "images/generations")
        guard !imageGenerationURLs.isEmpty else {
            throw ConfigEditorError(message: "Invalid /v1/images/generations URL for Base URL: \(baseURL)")
        }

        return ModelAvailabilityProbeRequest(
            providerIndex: providerIndex,
            providerEditorID: provider.editorID,
            modelIndex: modelIndex,
            modelEditorID: model.editorID,
            providerName: provider.displayName,
            keyName: key.name,
            publicModel: routePublicModelName(model),
            litellmModel: model.litellmModel.trimmingCharacters(in: .whitespacesAndNewlines),
            upstreamModel: upstreamModel,
            apiBase: baseURL,
            chatURLs: chatURLs,
            responsesURLs: responsesURLs,
            anthropicURLs: anthropicURLs,
            imageGenerationURLs: imageGenerationURLs,
            apiKey: key.value,
            deploymentToken: model.deploymentToken.trimmingCharacters(in: .whitespacesAndNewlines),
            supportsImageGeneration: model.supportsImageGeneration
        )
    }

    func endpointURLCandidates(baseURL: String, endpoint: String) -> [URL] {
        let base = baseURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let endpointPath = endpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !base.isEmpty, !endpointPath.isEmpty else { return [] }

        var strings = ["\(base)/\(endpointPath)"]
        if !base.lowercased().hasSuffix("/v1") {
            strings.append("\(base)/v1/\(endpointPath)")
        }

        var urls: [URL] = []
        var seen: Set<String> = []
        for string in strings {
            guard !seen.contains(string), let url = URL(string: string) else { continue }
            seen.insert(string)
            urls.append(url)
        }
        return urls
    }

    func modelAvailabilityProbeBody(model: String) throws -> Data {
        let payload: [String: Any] = [
            "model": model,
            "messages": [
                ["role": "user", "content": "Say pong only."],
            ],
            "max_tokens": 8,
        ]
        return try JSONSerialization.data(withJSONObject: payload, options: [])
    }

    func responsesEndpointProbeBody(model: String) throws -> Data {
        let payload: [String: Any] = [
            "model": model,
            "input": "Say pong only.",
            "max_output_tokens": 8,
        ]
        return try JSONSerialization.data(withJSONObject: payload, options: [])
    }

    func anthropicMessagesProbeBody(model: String) throws -> Data {
        let payload: [String: Any] = [
            "model": model,
            "messages": [
                ["role": "user", "content": "Say pong only."],
            ],
            "max_tokens": 8,
        ]
        return try JSONSerialization.data(withJSONObject: payload, options: [])
    }

    func modelAvailabilityImageGenerationProbeBody(model: String, size: String = "1024x1024") throws -> Data {
        let payload: [String: Any] = [
            "model": model,
            "prompt": "minimal probe image",
            "n": 1,
            "size": size,
        ]
        return try JSONSerialization.data(withJSONObject: payload, options: [])
    }

    func parseModelAvailabilityProbeOutcome(statusCode: Int, data: Data?) -> ModelAvailabilityProbeOutcome {
        let snippet = responseSnippet(data)
        if (200...299).contains(statusCode) {
            return .available(snippet.isEmpty ? "/chat/completions returned HTTP \(statusCode)." : "/chat/completions returned HTTP \(statusCode).\n\(snippet)")
        }
        return .unavailable(snippet.isEmpty ? "HTTP \(statusCode)" : "HTTP \(statusCode)\n\(snippet)")
    }

    func parseModelAvailabilityImageGenerationProbeOutcome(statusCode: Int, data: Data?) -> ModelAvailabilityProbeOutcome {
        let snippet = responseSnippet(data)
        if (200...299).contains(statusCode) {
            return .available(snippet.isEmpty ? "/images/generations returned HTTP \(statusCode)." : "/images/generations returned HTTP \(statusCode).\n\(snippet)")
        }
        if imageGenerationProbeParameterError(statusCode: statusCode, snippet: snippet) {
            let detail = snippet.isEmpty ? "HTTP \(statusCode)" : "HTTP \(statusCode)\n\(snippet)"
            return .inconclusive("The image probe request was rejected as invalid, so model availability was not changed.\n\(detail)")
        }
        return .unavailable(snippet.isEmpty ? "HTTP \(statusCode)" : "HTTP \(statusCode)\n\(snippet)")
    }

    func apiEndpointExists(statusCode: Int, data: Data?) -> Bool? {
        let snippet = responseSnippet(data)
        if (200...299).contains(statusCode) {
            return true
        }
        if statusCode == 404 || statusCode == 405 {
            return false
        }
        if statusCode == 400 || statusCode == 422 {
            return !snippet.lowercased().contains("not found")
        }
        return nil
    }

    func probeDetail(surface: String, url: URL, statusCode: Int, data: Data?) -> String {
        let snippet = responseSnippet(data)
        let status = snippet.isEmpty ? "HTTP \(statusCode)" : "HTTP \(statusCode)\n\(snippet)"
        return "\(surface) probe URL: \(url.absoluteString)\n\(status)"
    }

    func imageGenerationProbeParameterError(statusCode: Int, snippet: String) -> Bool {
        guard statusCode == 400 || statusCode == 422 else { return false }
        let text = snippet.lowercased()
        let mentionsSize = text.contains("invalid size")
            || text.contains("resolution")
            || text.contains("pixel budget")
            || text.contains("minimum pixel")
            || text.contains("size")
        return mentionsSize
            && (text.contains("below")
                || text.contains("minimum")
                || text.contains("unsupported")
                || text.contains("invalid")
                || text.contains("not supported"))
    }

    func responseSnippet(_ data: Data?) -> String {
        guard let data, !data.isEmpty else { return "" }
        let text = String(data: data, encoding: .utf8) ?? "\(data.count) bytes"
        let compact = text.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if compact.count <= 1200 {
            return compact
        }
        return String(compact.prefix(1200)) + "..."
    }

    func inlineProbeFailureDetail(from detail: String) -> String {
        let lines = detail
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !lines.isEmpty else { return "" }

        let httpIndex = lines.lastIndex {
            $0.range(of: #"^HTTP\s+\d+"#, options: .regularExpression) != nil
        }
        let httpLine = httpIndex.map { lines[$0] }
        let afterHTTP = httpIndex.map { Array(lines.dropFirst($0 + 1)) } ?? []

        if let jsonLine = afterHTTP.first(where: { $0.hasPrefix("{") || $0.hasPrefix("[") }),
           let summary = jsonErrorSummary(from: jsonLine) {
            return clippedStatusDetail(([httpLine, summary].compactMap { $0 }).joined(separator: ": "))
        }

        if let httpLine,
           afterHTTP.isEmpty {
            return clippedStatusDetail(httpLine)
        }

        if let httpLine,
           let bodyLine = afterHTTP.first(where: { !$0.isEmpty }) {
            return clippedStatusDetail("\(httpLine): \(bodyLine)")
        }

        let nonPreflight = lines.filter {
            !$0.hasPrefix("LiteLLM /model/info reports ")
                && !$0.hasPrefix("LiteLLM /model/info matched ")
        }
        return clippedStatusDetail(nonPreflight.last ?? lines.last ?? "")
    }

    func jsonErrorSummary(from text: String) -> String? {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) else {
            return nil
        }
        return jsonErrorSummary(fromJSONObject: object)
    }

    func jsonErrorSummary(fromJSONObject object: Any) -> String? {
        if let dict = object as? [String: Any] {
            if let nested = dict["error"] as? [String: Any],
               let summary = jsonErrorSummary(fromJSONObject: nested) {
                return summary
            }
            if let nested = dict["error"] as? String,
               !nested.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return nested.trimmingCharacters(in: .whitespacesAndNewlines)
            }

            let message = firstString(in: dict, keys: ["message", "detail", "reason"])
            if let message,
               let embeddedSummary = embeddedJSONErrorSummary(in: message) {
                return embeddedSummary
            }

            var parts: [String] = []
            if let code = firstString(in: dict, keys: ["code", "error_code"]) {
                parts.append("code=\(code)")
            }
            if let type = firstString(in: dict, keys: ["type", "error_type"]) {
                parts.append("type=\(type)")
            }
            if let message {
                parts.append(message)
            }
            return parts.isEmpty ? nil : parts.joined(separator: " ")
        }

        if let array = object as? [Any] {
            for item in array {
                if let summary = jsonErrorSummary(fromJSONObject: item) {
                    return summary
                }
            }
        }

        return nil
    }

    func embeddedJSONErrorSummary(in text: String) -> String? {
        let characters = Array(text)
        var startIndex: Int?
        var depth = 0
        var inString = false
        var escaped = false

        for (index, character) in characters.enumerated() {
            if startIndex == nil {
                if character == "{" || character == "[" {
                    startIndex = index
                    depth = 1
                    inString = false
                    escaped = false
                }
                continue
            }

            if inString {
                if escaped {
                    escaped = false
                } else if character == "\\" {
                    escaped = true
                } else if character == "\"" {
                    inString = false
                }
                continue
            }

            if character == "\"" {
                inString = true
            } else if character == "{" || character == "[" {
                depth += 1
            } else if character == "}" || character == "]" {
                depth -= 1
                if depth == 0, let fragmentStartIndex = startIndex {
                    let fragment = String(characters[fragmentStartIndex...index])
                    if let summary = jsonErrorSummary(from: fragment) {
                        return summary
                    }
                    startIndex = nil
                    depth = 0
                }
            }
        }

        return nil
    }

    func firstString(in dict: [String: Any], keys: [String]) -> String? {
        for key in keys {
            if let string = dict[key] as? String {
                let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty { return trimmed }
            }
            if let number = dict[key] as? NSNumber {
                return number.stringValue
            }
        }
        return nil
    }

    func clippedStatusDetail(_ text: String, limit: Int = 360) -> String {
        let compact = text.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if compact.count <= limit {
            return compact
        }
        return String(compact.prefix(limit)) + "..."
    }

    func parseModelCandidates(data: Data) throws -> [String] {
        let object = try JSONSerialization.jsonObject(with: data)
        return parseModelCandidateValues(from: object)
    }

    func parseModelCandidateValues(from value: Any) -> [String] {
        if let string = value as? String {
            return [string]
        }
        if let array = value as? [Any] {
            return array.flatMap { parseModelCandidateValues(from: $0) }
        }
        if let dict = value as? [String: Any] {
            if let data = dict["data"] {
                return parseModelCandidateValues(from: data)
            }
            if let models = dict["models"] {
                return parseModelCandidateValues(from: models)
            }
            for key in ["id", "name", "model", "model_id"] {
                if let string = dict[key] as? String, !string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    return [string]
                }
            }
        }
        return []
    }

}
