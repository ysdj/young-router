import Cocoa

final class ModelConfigEditorController: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSTextFieldDelegate, NSWindowDelegate {
    let root: String
    let bundleRoot: String
    let environment: [String: String]
    let onSaved: (ConfigEditorSaveResult) -> Void
    let onClose: () -> Void
    var providers: [EditableProvider] = []
    var window: NSWindow!
    enum DetailMode {
        case provider
        case model
        case none
    }
    enum EditorViewMode {
        case providers
        case routes
    }
    var detailMode: DetailMode = .none
    var viewMode: EditorViewMode = .providers
    var isRenderingSelection = false
    var providerEditorTargetIndex: Int?
    var providerEditorTargetID: UUID?
    var providerKeyEditorTarget: (provider: Int, providerID: UUID, key: Int, keyID: UUID)?
    var providerEditorDirty = false
    var modelEditorTarget: ModelSelectionIdentity?
    var modelCandidateRequestGeneration = 0
    var modelCandidateFetchInFlight = false
    var modelAvailabilityProbeRuns: [ModelProbeKey: UUID] = [:]
    var selectedModelInfoRequestGeneration = 0
    var selectedModelInfoInFlight = false
    var selectedModelImageGenerationEndpointDisabled = false
    var displayedUpstreamApiModes = ["openai/responses", "openai/chat", "anthropic"]
    var upstreamApiProbeSummaries: [String: String] = [:]
    var upstreamApiProbeDetails: [String: String] = [:]
    var upstreamApiProbeKey: ModelProbeKey?
    var upstreamApiModeRows: [String: NSStackView] = [:]
    var upstreamApiModeRankLabels: [String: NSTextField] = [:]
    var upstreamApiModeStatusLabels: [String: NSTextField] = [:]
    var runtimeApplyInFlight = false
    var runtimeApplyGeneration = 0
    let runtimeApplyLock = NSLock()
    var runtimeApplyProcess: Process?
    var fetchedModelChooserController: FetchedModelChooserController?
    var hasPendingChanges = false
    var loadedConfigRevision: JSONValue?

    let providerTableView = NSTableView()
    let modelTableView = NSTableView()
    let routeTableView = NSTableView()
    let providerNameColumnIdentifier = NSUserInterfaceItemIdentifier("providerName")
    let providerCountColumnIdentifier = NSUserInterfaceItemIdentifier("providerCount")
    let modelNameColumnIdentifier = NSUserInterfaceItemIdentifier("modelName")
    let modelUpstreamColumnIdentifier = NSUserInterfaceItemIdentifier("modelUpstream")
    let modelRouteColumnIdentifier = NSUserInterfaceItemIdentifier("modelRoute")
    let providerKeyNameColumnIdentifier = NSUserInterfaceItemIdentifier("providerKeyName")
    let routeModelColumnIdentifier = NSUserInterfaceItemIdentifier("routeModel")
    let routeOrderColumnIdentifier = NSUserInterfaceItemIdentifier("routeOrder")
    let routeProviderKeyColumnIdentifier = NSUserInterfaceItemIdentifier("routeProviderKey")
    let routeUpstreamColumnIdentifier = NSUserInterfaceItemIdentifier("routeUpstream")
    let routeStatusColumnIdentifier = NSUserInterfaceItemIdentifier("routeStatus")
    let runtimeMapColumnIdentifier = NSUserInterfaceItemIdentifier("runtimeMap")
    var providerCascadeView: NSView?
    var routesListView: NSView?
    var providerDetailView: NSView?
    var modelDetailView: NSView?
    let runtimeMapTableView = NSTableView()
    var runtimeMapScrollView: NSScrollView?
    var runtimeMapRows: [RuntimeMapRow] = []
    let adapterOptions = [
        "openai",
        "anthropic",
        "gemini",
        "azure",
        "bedrock",
        "vertex_ai",
        "openrouter",
        "deepseek",
        "xai",
        "groq",
        "mistral",
        "cohere",
        "ollama",
    ]
    let upstreamApiModes = ["openai/chat", "openai/responses", "anthropic"]
    let defaultUpstreamApiMode = "openai/responses"
    let customAdapterTitle = "Custom"
    let defaultProviderKeyName = "default"
    let emptyModelCandidateKeyName = ""
    let emptyModelCandidateKeyTitle = "(empty)"
    let browserCompatibleHeaderHosts: Set<String> = ["headers.example"]
    let browserCompatibleHeaders: [String: String] = [
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    ]
    var localLiteLLMModelInfoURL: URL {
        URL(string: "http://127.0.0.1:\(localServicePort(runtimeRoot: root, environment: environment))/model/info")!
    }
    let localLiteLLMMasterKey = "sk-local-litellm"

    struct RuntimeDeployment {
        var id: String
        var publicModel: String
        var providerName: String
        var keyName: String
        var upstreamModel: String
        var apiBase: String
        var order: Int?
        var providerEnabled: Bool
        var keyEnabled: Bool
        var modelEnabled: Bool
        var missingKey: Bool
        var supportsImageGeneration: Bool
        var isImageGenerationEndpoint: Bool
        var supportedUpstreamApiModes: [String]

        var enabled: Bool {
            providerEnabled && keyEnabled && modelEnabled && !missingKey
        }
    }

    struct RuntimeMapSummaryRow {
        var modelCount: Int
        var runningCount: Int
        var offCount: Int
    }

    struct RuntimeMapModelRow {
        var publicModel: String
        var runningCount: Int
        var offCount: Int
    }

    struct RuntimeMapOrderRow {
        var order: Int?
        var previousOrder: Int?
        var isFirst: Bool
        var runningCount: Int
        var offCount: Int
    }

    enum RuntimeMapRow {
        case summary(RuntimeMapSummaryRow)
        case model(RuntimeMapModelRow)
        case order(RuntimeMapOrderRow)
        case deployment(RuntimeDeployment)
        case empty
    }

    struct RouteDeploymentRow {
        var providerIndex: Int
        var modelIndex: Int
        var publicModel: String
        var providerName: String
        var keyName: String
        var upstreamModel: String
        var order: Int?
        var enabled: Bool
    }

    struct RouteModelGroupRow {
        var publicModel: String
        var routeCount: Int
        var runningCount: Int
        var offCount: Int
    }

    enum RouteTableRow {
        case modelGroup(RouteModelGroupRow)
        case deployment(RouteDeploymentRow)
    }

    struct LiteLLMModelInfoLookup {
        var publicModel: String
        var litellmModel: String
        var upstreamModel: String
        var apiBase: String
        var deploymentToken: String
    }

    struct ModelSelectionIdentity: Equatable {
        var provider: Int
        var providerID: UUID
        var model: Int
        var modelID: UUID
    }

    struct ModelProbeKey: Hashable {
        var providerID: UUID
        var modelID: UUID
    }

    struct ModelAvailabilityProbeRequest {
        var providerIndex: Int
        var providerEditorID: UUID
        var modelIndex: Int
        var modelEditorID: UUID
        var providerName: String
        var keyName: String
        var publicModel: String
        var litellmModel: String
        var upstreamModel: String
        var apiBase: String
        var chatURLs: [URL]
        var responsesURLs: [URL]
        var anthropicURLs: [URL]
        var imageGenerationURLs: [URL]
        var apiKey: String
        var deploymentToken: String
        var supportsImageGeneration: Bool

        var probeKey: ModelProbeKey {
            ModelProbeKey(providerID: providerEditorID, modelID: modelEditorID)
        }

        var modelInfoLookup: LiteLLMModelInfoLookup {
            LiteLLMModelInfoLookup(
                publicModel: publicModel,
                litellmModel: litellmModel,
                upstreamModel: upstreamModel,
                apiBase: apiBase,
                deploymentToken: deploymentToken
            )
        }
    }

    struct ModelCandidateRequest {
        var providerIndex: Int
        var providerEditorID: UUID
        var keyEditorID: UUID?
        var keyName: String
        var keyDisplayName: String
        var adapter: String
        var urls: [URL]
        var apiKey: String?
    }

    enum ModelAvailabilityProbeOutcome {
        case available(String)
        case unavailable(String)
        case inconclusive(String)
    }

    enum UpstreamApiProbeAvailability {
        case available
        case unavailable
        case inconclusive
    }

    struct UpstreamApiProbeResult {
        var mode: String
        var availability: UpstreamApiProbeAvailability
        var detail: String

        var isAvailable: Bool {
            availability == .available
        }
    }

    struct LiteLLMModelInfoCapability {
        var id: String
        var modelName: String
        var litellmModel: String
        var apiBase: String
        var mode: String
        var upstreamApiMode: String
        var supportsImageGenerationFlag: Bool?
        var provider: String
        var key: String
        var matchedBy: String

        var isImageGenerationEndpointModel: Bool {
            mode == "image_generation"
        }

        var supportsImageGeneration: Bool {
            supportsImageGenerationFlag == true
        }

        var summary: String {
            var facts: [String] = []
            if !id.isEmpty { facts.append("token=\(id)") }
            if !mode.isEmpty { facts.append("mode=\(mode)") }
            if !upstreamApiMode.isEmpty { facts.append("upstream_url_surface=\(upstreamApiMode)") }
            if let supportsImageGenerationFlag {
                facts.append("supports_responses_image_generation_tool=\(supportsImageGenerationFlag)")
            }
            if !provider.isEmpty { facts.append("provider=\(provider)") }
            if !key.isEmpty { facts.append("key=\(key)") }
            if !matchedBy.isEmpty { facts.append("matched by \(matchedBy)") }
            return facts.isEmpty ? "LiteLLM /model/info matched this deployment." : facts.joined(separator: ", ")
        }
    }

    lazy var viewModeControl: NSSegmentedControl = {
        let control = NSSegmentedControl(
            labels: ["Providers", "Routes"],
            trackingMode: .selectOne,
            target: self,
            action: #selector(editorViewModeChanged(_:))
        )
        control.segmentStyle = .rounded
        control.selectedSegment = 0
        control.setWidth(128, forSegment: 0)
        control.setWidth(128, forSegment: 1)
        control.widthAnchor.constraint(equalToConstant: 256).isActive = true
        control.heightAnchor.constraint(equalToConstant: 28).isActive = true
        return control
    }()

    func refreshViewModeButtons() {
        viewModeControl.selectedSegment = viewMode == .routes ? 1 : 0
    }

    lazy var providerEnabledCheckbox: NSButton = {
        NSButton(checkboxWithTitle: "Provider enabled", target: self, action: #selector(formCheckboxChanged(_:)))
    }()
    lazy var providerNameField = makeTextField(width: 430)
    lazy var providerApiBaseField = makeTextField(width: 430)
    let providerKeyTableView = NSTableView()
    lazy var providerKeyEnabledCheckbox: NSButton = {
        NSButton(checkboxWithTitle: "API key enabled", target: self, action: #selector(formCheckboxChanged(_:)))
    }()
    lazy var providerKeyNameField = makeTextField(width: 250)
    lazy var providerApiKeyField = makeTokenField(width: 250)
    lazy var addProviderKeyButton = NSButton(title: "Add Key", target: self, action: #selector(addProviderKey))
    lazy var deleteProviderKeyButton = NSButton(title: "Delete Key", target: self, action: #selector(deleteProviderKey))
    lazy var enabledCheckbox: NSButton = {
        NSButton(checkboxWithTitle: "Model enabled", target: self, action: #selector(formCheckboxChanged(_:)))
    }()
    lazy var probeModelAvailabilityButton: NSButton = {
        let button = NSButton(title: "Probe & Recommend", target: self, action: #selector(probeModelAvailability))
        button.bezelStyle = .rounded
        button.toolTip = "Probe model availability and all three API protocols, then recommend a minimal ordered selection"
        return button
    }()
    lazy var modelNameField = makeTextField(width: 430)
    lazy var modelApiKeyPopupButton: NSPopUpButton = {
        let popup = NSPopUpButton()
        popup.target = self
        popup.action = #selector(modelApiKeySelectionChanged(_:))
        popup.widthAnchor.constraint(equalToConstant: 240).isActive = true
        return popup
    }()
    lazy var adapterPopupButton: NSPopUpButton = {
        let popup = NSPopUpButton()
        popup.addItems(withTitles: adapterOptions + [customAdapterTitle])
        popup.target = self
        popup.action = #selector(adapterSelectionChanged(_:))
        popup.widthAnchor.constraint(equalToConstant: 180).isActive = true
        return popup
    }()
    lazy var customAdapterField: NSTextField = {
        let field = makeTextField(width: 240)
        field.isHidden = true
        return field
    }()
    lazy var fetchModelsButton: NSButton = {
        let button = NSButton(title: "Fetch /v1/models", target: self, action: #selector(fetchModelCandidates))
        button.bezelStyle = .rounded
        return button
    }()
    lazy var modelCandidateApiKeyPopupButton: NSPopUpButton = {
        let popup = NSPopUpButton()
        popup.target = self
        popup.action = #selector(modelCandidateApiKeySelectionChanged(_:))
        popup.widthAnchor.constraint(equalToConstant: 190).isActive = true
        popup.toolTip = "API key used only for Fetch /v1/models"
        return popup
    }()
    lazy var upstreamModelField = makeTextField(width: 430)
    lazy var orderField = makeTextField(width: 160)
    lazy var supportsOpenAIChatCheckbox: NSButton = {
        NSButton(checkboxWithTitle: "openai/chat", target: self, action: #selector(upstreamApiSupportChanged(_:)))
    }()
    lazy var supportsOpenAIResponsesCheckbox: NSButton = {
        NSButton(checkboxWithTitle: "openai/responses", target: self, action: #selector(upstreamApiSupportChanged(_:)))
    }()
    lazy var supportsAnthropicCheckbox: NSButton = {
        NSButton(checkboxWithTitle: "anthropic", target: self, action: #selector(upstreamApiSupportChanged(_:)))
    }()
    lazy var upstreamApiModeStackView: NSStackView = {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        return stack
    }()
    lazy var deleteProviderButton = NSButton(title: "Delete", target: self, action: #selector(deleteProvider))
    lazy var addModelButton = NSButton(title: "Add", target: self, action: #selector(addModel))
    lazy var duplicateModelButton = NSButton(title: "Duplicate", target: self, action: #selector(duplicateModel))
    lazy var deleteModelButton = NSButton(title: "Delete", target: self, action: #selector(deleteModel))
    lazy var routeMoveUpButton = NSButton(title: "Move Up", target: self, action: #selector(moveRouteUp))
    lazy var routeMoveDownButton = NSButton(title: "Move Down", target: self, action: #selector(moveRouteDown))
    lazy var routeNormalizeButton = NSButton(title: "Normalize Order", target: self, action: #selector(normalizeRouteOrder))
    lazy var applyStatusLabel: NSTextField = {
        let label = NSTextField(labelWithString: "")
        label.textColor = .secondaryLabelColor
        label.usesSingleLineMode = true
        label.lineBreakMode = .byTruncatingTail
        label.setContentHuggingPriority(.fittingSizeCompression, for: .horizontal)
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }()
    lazy var applyButton: NSButton = {
        let button = NSButton(title: "Apply", target: self, action: #selector(save))
        button.isEnabled = false
        return button
    }()

    init(
        root: String,
        bundleRoot: String,
        environment: [String: String],
        onSaved: @escaping (ConfigEditorSaveResult) -> Void,
        onClose: @escaping () -> Void
    ) {
        self.root = root
        self.bundleRoot = bundleRoot
        self.environment = environment
        self.onSaved = onSaved
        self.onClose = onClose
        super.init()
    }

    func showWindow() {
        if let existingWindow = window, existingWindow.isVisible || existingWindow.isMiniaturized {
            NSApp.activate(ignoringOtherApps: true)
            if existingWindow.isMiniaturized {
                existingWindow.deminiaturize(nil)
            }
            existingWindow.makeKeyAndOrderFront(nil)
            existingWindow.makeFirstResponder(nil)
            return
        }

        do {
            let payload = try loadConfigPayload()
            providers = payload.providers
            loadedConfigRevision = payload.revision
        } catch {
            showAlert(title: "Open config editor failed", message: error.localizedDescription)
            return
        }

        if window == nil {
            buildWindow()
        }
        providerTableView.reloadData()
        if providers.isEmpty {
            renderProviderSelection()
        } else {
            showProvider(at: 0)
        }
        reloadRouteTable()
        applyEditorViewMode()

        window.center()
        refreshRuntimeMap()
        setPendingChanges(false)
        NSApp.activate(ignoringOtherApps: true)
        if window.isMiniaturized {
            window.deminiaturize(nil)
        }
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(nil)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    func windowWillClose(_ notification: Notification) {
        // Fallback for unexpected closes. Normal editor closes are orderOut-only
        // to avoid AppKit transform-animation lifetime crashes seen on macOS 26.
        let onClose = self.onClose
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            onClose()
        }
    }
}
