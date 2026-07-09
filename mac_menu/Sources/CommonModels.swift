import Cocoa

let inlineStatusLimit = 180
let alertMessageLimit = 420

func singleLineDisplayText(_ value: String) -> String {
    value
        .trimmingCharacters(in: .whitespacesAndNewlines)
        .components(separatedBy: .whitespacesAndNewlines)
        .filter { !$0.isEmpty }
        .joined(separator: " ")
}

func elidedDisplayText(_ value: String, limit: Int) -> String {
    let display = singleLineDisplayText(value)
    guard display.count > limit else { return display }
    let cutoff = max(1, limit - 1)
    let index = display.index(display.startIndex, offsetBy: cutoff)
    return "\(display[..<index])…"
}

func shortAlertMessage(_ message: String) -> String {
    let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
    return elidedDisplayText(text.isEmpty ? "No output." : text, limit: alertMessageLimit)
}

func isValidTCPPortText(_ value: String) -> Bool {
    guard let port = Int(value), port >= 1, port <= 65535 else {
        return false
    }
    return String(port) == value
}

func localServicePort(runtimeRoot: String, environment: [String: String]) -> String {
    let defaultPort = "4000"
    if let rawValue = environment["LITELLM_PORT"] {
        let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if isValidTCPPortText(value) {
            return value
        }
    }

    let settingsPath = "\(runtimeRoot)/runtime-settings.env"
    guard let text = try? String(contentsOfFile: settingsPath, encoding: .utf8) else {
        return defaultPort
    }
    for rawLine in text.components(separatedBy: .newlines) {
        let line = rawLine.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)[0]
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard line.hasPrefix("LITELLM_PORT=") else { continue }
        let value = line.dropFirst("LITELLM_PORT=".count)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if isValidTCPPortText(value) {
            return value
        }
    }
    return defaultPort
}

struct AppError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

enum JSONValue: Codable, Equatable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unsupported JSON value")
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null:
            try container.encodeNil()
        case .bool(let value):
            try container.encode(value)
        case .int(let value):
            try container.encode(value)
        case .double(let value):
            try container.encode(value)
        case .string(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        }
    }
}

struct EditableModel: Codable, Equatable {
    var enabled: Bool
    var modelEnabled: Bool
    var provider: String
    var modelName: String
    var litellmModel: String
    var apiBase: String
    var apiKey: String
    var apiKeyName: String
    var order: String
    var sslVerify: String
    var sslVerifyPresent: Bool
    var deploymentToken: String
    var supportsVision: Bool
    var supportsVisionPresent: Bool
    var supportsImageGeneration: Bool
    var supportsImageGenerationPresent: Bool
    var upstreamApiMode: String
    var upstreamApiModePresent: Bool
    var supportedUpstreamApiModes: [String]
    var supportedUpstreamApiModesPresent: Bool
    var supportsResponsesEndpoint: Bool
    var supportsResponsesEndpointPresent: Bool
    var entryExtra: [String: JSONValue]
    var litellmExtra: [String: JSONValue]
    var modelInfoExtra: [String: JSONValue]
    var editorID = UUID()

    enum CodingKeys: String, CodingKey {
        case enabled
        case modelEnabled = "model_enabled"
        case provider
        case modelName = "model_name"
        case litellmModel = "litellm_model"
        case apiBase = "api_base"
        case apiKey = "api_key"
        case apiKeyName = "api_key_name"
        case order
        case sslVerify = "ssl_verify"
        case sslVerifyPresent = "ssl_verify_present"
        case deploymentToken = "deployment_id"
        case supportsVision = "supports_vision"
        case supportsVisionPresent = "supports_vision_present"
        case supportsImageGeneration = "supports_responses_image_generation_tool"
        case supportsImageGenerationPresent = "supports_responses_image_generation_tool_present"
        case upstreamApiMode = "upstream_url_surface"
        case upstreamApiModePresent = "upstream_url_surface_present"
        case supportedUpstreamApiModes = "supported_upstream_url_surfaces"
        case supportedUpstreamApiModesPresent = "supported_upstream_url_surfaces_present"
        case supportsResponsesEndpoint = "supports_responses_endpoint"
        case supportsResponsesEndpointPresent = "supports_responses_endpoint_present"
        case entryExtra = "entry_extra"
        case litellmExtra = "litellm_extra"
        case modelInfoExtra = "model_info_extra"
    }

    static func blank() -> EditableModel {
        EditableModel(
            enabled: true,
            modelEnabled: true,
            provider: "",
            modelName: "",
            litellmModel: "openai/",
            apiBase: "",
            apiKey: "",
            apiKeyName: "",
            order: "1",
            sslVerify: "",
            sslVerifyPresent: false,
            deploymentToken: "",
            supportsVision: true,
            supportsVisionPresent: true,
            supportsImageGeneration: false,
            supportsImageGenerationPresent: false,
            upstreamApiMode: "openai/responses",
            upstreamApiModePresent: false,
            supportedUpstreamApiModes: ["openai/responses"],
            supportedUpstreamApiModesPresent: false,
            supportsResponsesEndpoint: true,
            supportsResponsesEndpointPresent: false,
            entryExtra: [:],
            litellmExtra: [:],
            modelInfoExtra: [:]
        )
    }

    init(
        enabled: Bool,
        modelEnabled: Bool,
        provider: String,
        modelName: String,
        litellmModel: String,
        apiBase: String,
        apiKey: String,
        apiKeyName: String,
        order: String,
        sslVerify: String,
        sslVerifyPresent: Bool,
        deploymentToken: String,
        supportsVision: Bool,
        supportsVisionPresent: Bool,
        supportsImageGeneration: Bool,
        supportsImageGenerationPresent: Bool,
        upstreamApiMode: String,
        upstreamApiModePresent: Bool,
        supportedUpstreamApiModes: [String],
        supportedUpstreamApiModesPresent: Bool,
        supportsResponsesEndpoint: Bool,
        supportsResponsesEndpointPresent: Bool,
        entryExtra: [String: JSONValue],
        litellmExtra: [String: JSONValue],
        modelInfoExtra: [String: JSONValue]
    ) {
        self.enabled = enabled
        self.modelEnabled = modelEnabled
        self.provider = provider
        self.modelName = modelName
        self.litellmModel = litellmModel
        self.apiBase = apiBase
        self.apiKey = apiKey
        self.apiKeyName = apiKeyName
        self.order = order
        self.sslVerify = sslVerify
        self.sslVerifyPresent = sslVerifyPresent
        self.deploymentToken = deploymentToken
        self.supportsVision = supportsVision
        self.supportsVisionPresent = supportsVisionPresent
        self.supportsImageGeneration = supportsImageGeneration
        self.supportsImageGenerationPresent = supportsImageGenerationPresent
        self.upstreamApiMode = upstreamApiMode
        self.upstreamApiModePresent = upstreamApiModePresent
        self.supportedUpstreamApiModes = supportedUpstreamApiModes
        self.supportedUpstreamApiModesPresent = supportedUpstreamApiModesPresent
        self.supportsResponsesEndpoint = supportsResponsesEndpoint
        self.supportsResponsesEndpointPresent = supportsResponsesEndpointPresent
        self.entryExtra = entryExtra
        self.litellmExtra = litellmExtra
        self.modelInfoExtra = modelInfoExtra
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            enabled: try container.decodeIfPresent(Bool.self, forKey: .enabled) ?? true,
            modelEnabled: try container.decodeIfPresent(Bool.self, forKey: .modelEnabled) ?? true,
            provider: try container.decodeIfPresent(String.self, forKey: .provider) ?? "",
            modelName: try container.decodeIfPresent(String.self, forKey: .modelName) ?? "",
            litellmModel: try container.decodeIfPresent(String.self, forKey: .litellmModel) ?? "openai/",
            apiBase: try container.decodeIfPresent(String.self, forKey: .apiBase) ?? "",
            apiKey: try container.decodeIfPresent(String.self, forKey: .apiKey) ?? "",
            apiKeyName: try container.decodeIfPresent(String.self, forKey: .apiKeyName) ?? "",
            order: try container.decodeIfPresent(String.self, forKey: .order) ?? "1",
            sslVerify: try container.decodeIfPresent(String.self, forKey: .sslVerify) ?? "",
            sslVerifyPresent: try container.decodeIfPresent(Bool.self, forKey: .sslVerifyPresent) ?? false,
            deploymentToken: try container.decodeIfPresent(String.self, forKey: .deploymentToken) ?? "",
            supportsVision: try container.decodeIfPresent(Bool.self, forKey: .supportsVision) ?? true,
            supportsVisionPresent: try container.decodeIfPresent(Bool.self, forKey: .supportsVisionPresent) ?? true,
            supportsImageGeneration: try container.decodeIfPresent(Bool.self, forKey: .supportsImageGeneration) ?? false,
            supportsImageGenerationPresent: try container.decodeIfPresent(Bool.self, forKey: .supportsImageGenerationPresent) ?? false,
            upstreamApiMode: try container.decodeIfPresent(String.self, forKey: .upstreamApiMode) ?? "openai/responses",
            upstreamApiModePresent: try container.decodeIfPresent(Bool.self, forKey: .upstreamApiModePresent) ?? false,
            supportedUpstreamApiModes: try container.decodeIfPresent([String].self, forKey: .supportedUpstreamApiModes) ?? ["openai/responses"],
            supportedUpstreamApiModesPresent: try container.decodeIfPresent(Bool.self, forKey: .supportedUpstreamApiModesPresent) ?? false,
            supportsResponsesEndpoint: try container.decodeIfPresent(Bool.self, forKey: .supportsResponsesEndpoint) ?? true,
            supportsResponsesEndpointPresent: try container.decodeIfPresent(Bool.self, forKey: .supportsResponsesEndpointPresent) ?? false,
            entryExtra: try container.decodeIfPresent([String: JSONValue].self, forKey: .entryExtra) ?? [:],
            litellmExtra: try container.decodeIfPresent([String: JSONValue].self, forKey: .litellmExtra) ?? [:],
            modelInfoExtra: try container.decodeIfPresent([String: JSONValue].self, forKey: .modelInfoExtra) ?? [:]
        )
    }

    var displayName: String {
        let name = modelName.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty { return name }
        let upstream = litellmModel.trimmingCharacters(in: .whitespacesAndNewlines)
        return upstream.isEmpty ? "New model" : upstream
    }

    var isBlank: Bool {
        [
            modelName,
            litellmModel,
            order,
            sslVerify,
            deploymentToken,
        ].allSatisfy { $0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
}

struct EditableProviderKey: Codable, Equatable {
    var name: String
    var value: String
    var enabled: Bool
    var editorID = UUID()

    enum CodingKeys: String, CodingKey {
        case name
        case value
        case enabled
    }

    static func blank() -> EditableProviderKey {
        EditableProviderKey(name: "default", value: "", enabled: true)
    }

    var displayName: String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "New key" : trimmed
    }

    var isBlank: Bool {
        name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

struct EditableProvider: Codable, Equatable {
    var name: String
    var enabled: Bool
    var apiBase: String
    var apiKey: String
    var apiKeys: [EditableProviderKey]
    var models: [EditableModel]
    var extra: [String: JSONValue]
    var editorID = UUID()

    enum CodingKeys: String, CodingKey {
        case name
        case enabled
        case apiBase = "api_base"
        case apiKey = "api_key"
        case apiKeys = "api_keys"
        case models
        case extra
    }

    static func blank() -> EditableProvider {
        EditableProvider(name: "", enabled: true, apiBase: "", apiKey: "", apiKeys: [EditableProviderKey.blank()], models: [], extra: [:])
    }

    var displayName: String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "New provider" : trimmed
    }

    var isBlank: Bool {
        name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && apiBase.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && apiKeys.allSatisfy { $0.isBlank }
            && models.allSatisfy { $0.isBlank }
    }
}

struct ConfigEditorLoadPayload: Codable {
    var providers: [EditableProvider]
    var revision: JSONValue?
}

struct ConfigEditorSavePayload: Codable {
    var providers: [EditableProvider]
    var expectedRevision: JSONValue?

    enum CodingKeys: String, CodingKey {
        case providers
        case expectedRevision = "expected_revision"
    }
}

struct ConfigEditorSaveResult: Codable {
    var providers: Int
    var active: Int
    var disabled: Int
    var backup: String
    var disabledPath: String
    var disabledBackup: String
    var revision: JSONValue?

    enum CodingKeys: String, CodingKey {
        case providers
        case active
        case disabled
        case backup
        case disabledPath = "disabled_path"
        case disabledBackup = "disabled_backup"
        case revision
    }
}

struct ConfigEditorError: LocalizedError {
    var message: String
    var errorDescription: String? { message }
}

final class FlippedDocumentView: NSView {
    override var isFlipped: Bool { true }
}

let defaultWebDAVRemoteName = "litellm-menu-config.json"
let defaultWebDAVSyncIntervalMinutes = 30
let defaultWebDAVTimeoutSeconds = 30
