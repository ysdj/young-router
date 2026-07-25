import Cocoa

final class NavigationLinkButton: NSButton {
    private var hoverTrackingArea: NSTrackingArea?
    private var isHovering = false {
        didSet { updateNavigationAppearance() }
    }

    func setNavigationTitle(_ value: String) {
        title = value
        updateNavigationAppearance()
    }

    override var isEnabled: Bool {
        didSet {
            if !isEnabled {
                isHovering = false
            }
            updateNavigationAppearance()
        }
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let hoverTrackingArea {
            removeTrackingArea(hoverTrackingArea)
        }
        let trackingArea = NSTrackingArea(
            rect: .zero,
            options: [.activeInKeyWindow, .inVisibleRect, .mouseEnteredAndExited],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(trackingArea)
        hoverTrackingArea = trackingArea
    }

    override func resetCursorRects() {
        super.resetCursorRects()
        if isEnabled {
            addCursorRect(bounds, cursor: .pointingHand)
        }
    }

    override func mouseEntered(with event: NSEvent) {
        if isEnabled {
            isHovering = true
        }
    }

    override func mouseExited(with event: NSEvent) {
        isHovering = false
    }

    private func updateNavigationAppearance() {
        let color: NSColor
        if isEnabled {
            color = isHovering ? .controlAccentColor : .linkColor
        } else {
            color = .secondaryLabelColor
        }
        attributedTitle = NSAttributedString(
            string: title,
            attributes: [
                .font: font ?? NSFont.systemFont(ofSize: 13, weight: .semibold),
                .foregroundColor: color,
                .underlineStyle: isHovering && isEnabled && !title.isEmpty ? NSUnderlineStyle.single.rawValue : 0,
            ]
        )
        contentTintColor = color
        window?.invalidateCursorRects(for: self)
    }
}

final class ModelConfigEditorController: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSTextFieldDelegate, NSWindowDelegate {
    let root: String
    let bundleRoot: String
    let environment: [String: String]
    let onSaved: (ConfigEditorSaveResult) -> Void
    let onClose: () -> Void
    var providers: [EditableProvider] = []
    var configurationBaselineProviders: [EditableProvider] = []
    var configurationBaselineDocument: ConfigEditorDocument?
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
    var providerNameAutofillProviderID: UUID?
    var modelEditorTarget: ModelSelectionIdentity?
    // A table click can commit the previously displayed editor before AppKit
    // finishes changing the row selection. Keep the clicked deployment by ID
    // so a route reorder cannot turn that click into a different deployment.
    var pendingRouteSelectionIdentity: ModelSelectionIdentity?
    var providerEditorSourceModel: ModelSelectionIdentity?
    var modelCandidateRequestGeneration = 0
    var modelCandidateFetchInFlight = false
    var modelAvailabilityProbeRuns: [ModelProbeKey: UUID] = [:]
    var modelProbePresentations: [ModelProbeKey: ModelProbePresentation] = [:]
    var displayedModelProbePresentationKey: ModelProbeKey?
    var selectedModelInfoRequestGeneration = 0
    var selectedModelInfoInFlight = false
    var selectedModelImageGenerationEndpointDisabled = false
    var displayedUpstreamApiModes = ["openai/responses", "openai/chat", "anthropic"]
    var upstreamApiModeRows: [String: NSStackView] = [:]
    var upstreamApiModeRankLabels: [String: NSTextField] = [:]
    var upstreamApiModeMoveUpButtons: [String: NSButton] = [:]
    var upstreamApiModeMoveDownButtons: [String: NSButton] = [:]
    var runtimeApplyInFlight = false
    var runtimeApplyGeneration = 0
    let runtimeApplyLock = NSLock()
    var runtimeApplyProcess: Process?
    var fetchedModelChooserController: FetchedModelChooserController?
    var hasPendingChanges = false
    var loadedConfigRevision: JSONValue?
    var sourceDocument: ConfigEditorDocument?
    var providerBilling: ProviderBillingPayload?
    var providerBillingFailureDetail: String?
    var providerBillingRefreshInFlight = false
    var providerBillingRefreshGeneration = 0
    var providerBillingRefreshTimer: Timer?
    var externalImportInFlight = false
    var configurationLoadGeneration = 0
    var configurationLoadInFlight = false

    let providerTableView = NSTableView()
    let modelTableView = NSTableView()
    let routeTableView = NSTableView()
    let providerNameColumnIdentifier = NSUserInterfaceItemIdentifier("providerName")
    let providerCountColumnIdentifier = NSUserInterfaceItemIdentifier("providerCount")
    let modelNameColumnIdentifier = NSUserInterfaceItemIdentifier("modelName")
    let modelUpstreamColumnIdentifier = NSUserInterfaceItemIdentifier("modelUpstream")
    let modelBillingColumnIdentifier = NSUserInterfaceItemIdentifier("modelBilling")
    let modelApiKeyOrderColumnIdentifier = NSUserInterfaceItemIdentifier("modelApiKeyOrder")
    let providerKeyNameColumnIdentifier = NSUserInterfaceItemIdentifier("providerKeyName")
    let routeModelColumnIdentifier = NSUserInterfaceItemIdentifier("routeModel")
    let routeOrderColumnIdentifier = NSUserInterfaceItemIdentifier("routeOrder")
    let routeProviderKeyColumnIdentifier = NSUserInterfaceItemIdentifier("routeProviderKey")
    let routeUpstreamColumnIdentifier = NSUserInterfaceItemIdentifier("routeUpstream")
    var editorWorkspaceStack: NSStackView?
    var modeWorkspaceColumn: NSStackView?
    var modeWorkspaceHost: NSView?
    var providersWorkspace: NSView?
    var routesWorkspace: NSView?
    var providersContentStack: NSStackView?
    var providerPane: NSView?
    var providerPaneWidthConstraint: NSLayoutConstraint?
    var modelsRoutesPane: NSView?
    var modelsView: NSView?
    var detailScrollView: NSScrollView?
    var detailDocumentView: NSView?
    var modelTableScrollView: NSScrollView?
    var routeTableScrollView: NSScrollView?
    var editorFooterView: NSView?
    var detailPaneMinimumWidthConstraint: NSLayoutConstraint?
    var providerDetailView: NSView?
    var modelDetailView: NSView?
    let upstreamApiModes = ["openai/responses", "openai/chat", "anthropic"]
    let defaultUpstreamApiMode = "openai/responses"
    let modelFormLabelWidth: CGFloat = 96
    let upstreamApiModeOrderMetadataKey = "x-litellm-menu-upstream-url-surface-order"
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

    struct RouteDeploymentRow {
        var providerIndex: Int
        var modelIndex: Int
        var publicModel: String
        var providerName: String
        var keyName: String
        var upstreamModel: String
        var order: Decimal?
        var enabled: Bool
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

    struct ModelProbePresentation {
        enum State {
            case probing
            case available
            case unavailable
            case inconclusive

            var label: String {
                switch self {
                case .probing: return "Probing..."
                case .available: return "Available"
                case .unavailable: return "Unavailable"
                case .inconclusive: return "Uncertain"
                }
            }
        }

        var state: State
        var summary: String
        var detail: String
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
            if !id.isEmpty { facts.append("deployment_id=\(id)") }
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
        control.setWidth(112, forSegment: 0)
        control.setWidth(112, forSegment: 1)
        control.widthAnchor.constraint(equalToConstant: 224).isActive = true
        control.heightAnchor.constraint(equalToConstant: 28).isActive = true
        return control
    }()

    lazy var importSourcePopupButton: NSPopUpButton = {
        let popup = NSPopUpButton(frame: .zero, pullsDown: true)
        popup.bezelStyle = .rounded
        popup.addItem(withTitle: "Import From")
        popup.addItem(withTitle: "Current Codex")
        popup.lastItem?.tag = 1
        popup.addItem(withTitle: "Configuration File…")
        popup.lastItem?.tag = 2
        popup.addItem(withTitle: "CC Switch / New API Link…")
        popup.lastItem?.tag = 3
        popup.target = self
        popup.action = #selector(importSourceSelected(_:))
        popup.toolTip = "Import providers and models from a selected source"
        popup.setAccessibilityLabel("Import providers and models from a selected source")
        popup.widthAnchor.constraint(equalToConstant: 152).isActive = true
        return popup
    }()

    func refreshViewModeButtons() {
        viewModeControl.selectedSegment = viewMode == .routes ? 1 : 0
    }

    lazy var providerEnabledCheckbox: NSButton = {
        let button = NSButton(checkboxWithTitle: "Enabled", target: self, action: #selector(formCheckboxChanged(_:)))
        button.toolTip = "Enable provider"
        button.setAccessibilityLabel("Enable provider")
        return button
    }()
    lazy var providerNameField = makeTextField(preferredWidth: 240, minWidth: 160)
    lazy var providerApiBaseField = makeTextField(preferredWidth: 240, minWidth: 160)
    let providerKeyTableView = NSTableView()
    lazy var providerKeyNameField = makeTextField(preferredWidth: 170, minWidth: 110)
    lazy var providerApiKeyField = makeAPIKeyField(preferredWidth: 170, minWidth: 110)
    lazy var addProviderKeyButton: NSButton = {
        let button = textButton(title: "+", toolTip: "Add API key", accessibilityLabel: "Add API key")
        button.target = self
        button.action = #selector(addProviderKey)
        return button
    }()
    lazy var deleteProviderKeyButton: NSButton = {
        let button = textButton(title: "−", toolTip: "Remove API key", accessibilityLabel: "Remove API key")
        button.target = self
        button.action = #selector(deleteProviderKey)
        return button
    }()
    lazy var enabledCheckbox: NSButton = {
        let button = NSButton(checkboxWithTitle: "Enabled", target: self, action: #selector(formCheckboxChanged(_:)))
        button.toolTip = "Enable model"
        button.setAccessibilityLabel("Enable model")
        return button
    }()
    lazy var probeModelAvailabilityButton: NSButton = {
        let button = NSButton(title: "Probe", target: self, action: #selector(probeModelAvailability))
        button.bezelStyle = .rounded
        button.toolTip = "Check all three API protocols and recommend an order when needed"
        return button
    }()
    lazy var modelProbeStatusLabel: NSTextField = {
        let label = NSTextField(labelWithString: "")
        label.textColor = .secondaryLabelColor
        label.usesSingleLineMode = true
        label.lineBreakMode = .byTruncatingTail
        label.maximumNumberOfLines = 1
        label.setContentHuggingPriority(.defaultLow, for: .horizontal)
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        label.isHidden = true
        return label
    }()
    lazy var modelBillingStatusLabel: NSTextField = {
        let label = NSTextField(labelWithString: "")
        label.textColor = .secondaryLabelColor
        label.lineBreakMode = .byTruncatingTail
        label.usesSingleLineMode = true
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }()
    lazy var modelUsageStatusLabel: NSTextField = {
        let label = NSTextField(labelWithString: "")
        label.textColor = .secondaryLabelColor
        label.lineBreakMode = .byTruncatingTail
        label.usesSingleLineMode = true
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }()
    lazy var modelMultiplierStatusLabel: NSTextField = {
        let label = NSTextField(labelWithString: "")
        label.textColor = .secondaryLabelColor
        label.lineBreakMode = .byTruncatingTail
        label.usesSingleLineMode = true
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }()
    lazy var providerEditorTitleLabel: NSTextField = {
        let label = NSTextField(labelWithString: "Provider")
        label.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        label.textColor = .secondaryLabelColor
        label.lineBreakMode = .byTruncatingMiddle
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }()
    lazy var providerReturnToModelButton: NavigationLinkButton = {
        let button = NavigationLinkButton(title: "", target: self, action: #selector(providerReturnToModelClicked(_:)))
        button.bezelStyle = .inline
        button.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        button.toolTip = "Back to the source model"
        button.setAccessibilityLabel("Back to source model")
        button.isHidden = true
        button.isEnabled = false
        return button
    }()
    lazy var modelBreadcrumbProviderButton: NavigationLinkButton = {
        let button = NavigationLinkButton(title: "", target: self, action: #selector(modelBreadcrumbProviderClicked(_:)))
        button.bezelStyle = .inline
        button.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        button.toolTip = "Edit this model's provider"
        button.setAccessibilityLabel("Edit selected model provider")
        return button
    }()
    lazy var modelBreadcrumbModelLabel: NSTextField = {
        let label = NSTextField(labelWithString: "")
        label.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        label.lineBreakMode = .byTruncatingMiddle
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }()
    lazy var modelNameField = makeFlexibleTextField()
    lazy var modelProviderPopupButton: NSPopUpButton = {
        let popup = NSPopUpButton()
        popup.target = self
        popup.action = #selector(modelProviderSelectionChanged(_:))
        popup.toolTip = "Move this deployment to another provider"
        popup.setAccessibilityLabel("Model provider")
        return popup
    }()
    lazy var modelApiKeyPopupButton: NSPopUpButton = {
        let popup = NSPopUpButton()
        popup.target = self
        popup.action = #selector(modelApiKeySelectionChanged(_:))
        return popup
    }()
    lazy var fetchModelsButton: NSButton = {
        let button = NSButton(title: "Fetch", target: self, action: #selector(fetchModelCandidates))
        button.bezelStyle = .rounded
        button.toolTip = "Fetch models from /v1/models"
        button.setAccessibilityLabel("Fetch models")
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
    lazy var upstreamModelField = makeFlexibleTextField()
    lazy var orderField = makeFlexibleTextField()
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
        stack.spacing = 6
        return stack
    }()
    lazy var deleteProviderButton: NSButton = {
        let button = textButton(title: "−", toolTip: "Remove provider", accessibilityLabel: "Remove provider")
        button.target = self
        button.action = #selector(deleteProvider)
        return button
    }()
    lazy var addModelButton: NSButton = {
        let button = textButton(title: "+", toolTip: "Add model", accessibilityLabel: "Add model")
        button.target = self
        button.action = #selector(addModel)
        return button
    }()
    lazy var duplicateModelButton: NSButton = {
        let button = textButton(title: "⧉", toolTip: "Duplicate model", accessibilityLabel: "Duplicate model")
        button.target = self
        button.action = #selector(duplicateModel)
        return button
    }()
    lazy var deleteModelButton: NSButton = {
        let button = textButton(title: "−", toolTip: "Remove model", accessibilityLabel: "Remove model")
        button.target = self
        button.action = #selector(deleteModel)
        return button
    }()
    lazy var routeMoveUpButton: NSButton = {
        let button = textButton(title: "↑", toolTip: "Move route up", accessibilityLabel: "Move route up")
        button.target = self
        button.action = #selector(moveRouteUp)
        return button
    }()
    lazy var routeMoveDownButton: NSButton = {
        let button = textButton(title: "↓", toolTip: "Move route down", accessibilityLabel: "Move route down")
        button.target = self
        button.action = #selector(moveRouteDown)
        return button
    }()
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
        let reopeningExistingWindow = window != nil
        if window == nil {
            buildWindow()
            prepareEditorSkeleton()
            window.center()
        }

        presentEditorWindow()
        if reopeningExistingWindow {
            configureProviderBillingRefreshTimer(refreshImmediately: true)
        }
    }

    func prepareEditorSkeleton() {
        providerTableView.reloadData()
        modelTableView.reloadData()
        reloadRouteTable()
        renderProviderSelection()
        applyEditorViewMode()
        captureConfigurationBaseline()
    }

    func presentEditorWindow() {
        guard let window else { return }
        beginSettingsWindowPresentation(window)
        if window.isMiniaturized {
            window.deminiaturize(nil)
        }
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        window.makeFirstResponder(nil)
    }

    func loadConfigurationInBackground() {
        guard !configurationLoadInFlight, !hasPendingChanges else { return }
        configurationLoadGeneration += 1
        let generation = configurationLoadGeneration
        configurationLoadInFlight = true
        setEditorStatus("Loading configuration…")

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result: Result<ConfigEditorLoadPayload, Error>
            do {
                result = .success(try self.loadConfigPayload())
            } catch {
                result = .failure(error)
            }
            DispatchQueue.main.async {
                guard self.configurationLoadGeneration == generation else { return }
                self.configurationLoadInFlight = false
                guard !self.hasPendingChanges else { return }
                switch result {
                case .success(let payload):
                    self.applyLoadedConfiguration(payload)
                case .failure(let error):
                    self.setEditorStatus(
                        "Configuration could not be loaded.",
                        tooltip: error.localizedDescription
                    )
                }
            }
        }
    }

    func applyLoadedConfiguration(_ payload: ConfigEditorLoadPayload) {
        let previouslySelectedProviderName = selectedProviderIndex.map {
            providers[$0].name.trimmingCharacters(in: .whitespacesAndNewlines)
        }

        providers = payload.providers
        loadedConfigRevision = payload.revision
        sourceDocument = payload.document
        modelAvailabilityProbeRuns.removeAll()
        modelProbePresentations.removeAll()
        displayedModelProbePresentationKey = nil
        detailMode = .none
        providerEditorTargetIndex = nil
        providerEditorTargetID = nil
        providerKeyEditorTarget = nil
        providerEditorDirty = false
        modelEditorTarget = nil
        pendingRouteSelectionIdentity = nil
        providerEditorSourceModel = nil
        isRenderingSelection = true
        providerTableView.deselectAll(nil)
        modelTableView.deselectAll(nil)
        providerKeyTableView.deselectAll(nil)
        routeTableView.deselectAll(nil)
        isRenderingSelection = false
        providerTableView.reloadData()
        if providers.isEmpty {
            renderProviderSelection()
        } else {
            let providerIndex = previouslySelectedProviderName.flatMap { name in
                providers.firstIndex {
                    $0.name.trimmingCharacters(in: .whitespacesAndNewlines)
                        .localizedCaseInsensitiveCompare(name) == .orderedSame
                }
            } ?? 0
            // A provider selection populates the model list without opening a
            // deployment inspector until the user explicitly chooses a model.
            showProvider(at: providerIndex)
        }
        reloadRouteTable()
        applyEditorViewMode()
        configureProviderBillingRefreshTimer(refreshImmediately: true)
        captureConfigurationBaseline()
        setEditorStatus("")
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        requestEditorClose()
        return false
    }

    func windowWillClose(_ notification: Notification) {
        // Fallback for unexpected closes. Normal editor closes are orderOut-only
        // to avoid AppKit transform-animation lifetime crashes seen on macOS 26.
        let onClose = self.onClose
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
            onClose()
        }
        if let closedWindow = notification.object as? NSWindow {
            endSettingsWindowPresentation(closedWindow)
        }
    }
}
