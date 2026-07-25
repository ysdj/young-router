import Cocoa
import Darwin

// MARK: - Codable bridge for codex_config.py

struct CodexSettingsModel: Codable, Equatable {
    var model: String
    var provider: String
    var deploymentID: String
    var upstreamModel: String?
    var apiBase: String?

    enum CodingKeys: String, CodingKey {
        case model
        case provider
        case deploymentID = "deployment_id"
        case upstreamModel = "upstream_model"
        case apiBase = "api_base"
    }

    var displayTitle: String {
        let deployment = deploymentID.isEmpty ? "default deployment" : deploymentID
        return "\(model) · \(provider.isEmpty ? "LiteLLM" : provider) · \(deployment)"
    }
}

struct CodexSettingsPermissions: Codable, Equatable {
    var mode: String? = nil
    var sandboxMode: String? = nil
    var approvalPolicy: String? = nil
    var approvalsReviewer: String? = nil
    var defaultPermissions: String? = nil
    var networkAccess: Bool? = nil
    var writableRoots: [String]? = nil
    var conflict: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case mode
        case sandboxMode = "sandbox_mode"
        case approvalPolicy = "approval_policy"
        case approvalsReviewer = "approvals_reviewer"
        case defaultPermissions = "default_permissions"
        case networkAccess = "network_access"
        case writableRoots = "writable_roots"
        case conflict
    }
}

struct CodexSettingsProvider: Codable, Equatable {
    var id: String
    var name: String?
    var baseURL: String?
    var wireAPI: String?
    var envKey: String?
    var requiresOpenAIAuth: Bool?
    var authCommand: String?
    var authMode: String?

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case baseURL = "base_url"
        case wireAPI = "wire_api"
        case envKey = "env_key"
        case requiresOpenAIAuth = "requires_openai_auth"
        case authCommand = "auth_command"
        case authMode = "auth_mode"
    }

    static func blank() -> CodexSettingsProvider {
        CodexSettingsProvider(
            id: "provider",
            name: "",
            baseURL: "",
            wireAPI: "responses",
            envKey: "",
            requiresOpenAIAuth: false,
            authCommand: "",
            authMode: "none"
        )
    }
}

struct CodexSettingsMCPServer: Codable, Equatable {
    var id: String
    var enabled: Bool?
    var required: Bool?
    var transport: String?
    var command: String?
    var url: String?

    static func blank() -> CodexSettingsMCPServer {
        CodexSettingsMCPServer(
            id: "server",
            enabled: true,
            required: false,
            transport: "stdio",
            command: "",
            url: ""
        )
    }
}

struct CodexSettingsPlugin: Codable, Equatable {
    var id: String
    var enabled: Bool?

    static func blank() -> CodexSettingsPlugin {
        CodexSettingsPlugin(id: "plugin", enabled: true)
    }
}

struct CodexSettingsAdvanced: Codable, Equatable {
    var shellEnvironmentInherit: String? = nil
    var historyPersistence: String? = nil
    var agentsMaxThreads: String? = nil
    var agentsMaxDepth: String? = nil
    var fileOpener: String? = nil
    var mcpCredentialsStore: String? = nil

    enum CodingKeys: String, CodingKey {
        case shellEnvironmentInherit = "shell_environment_inherit"
        case historyPersistence = "history_persistence"
        case agentsMaxThreads = "agents_max_threads"
        case agentsMaxDepth = "agents_max_depth"
        case fileOpener = "file_opener"
        case mcpCredentialsStore = "mcp_oauth_credentials_store"
    }
}

struct CodexSettingsStructured: Codable, Equatable {
    var model: String? = nil
    var reviewModel: String? = nil
    var modelProvider: String? = nil
    var openAIBaseURL: String? = nil
    var apiKey: String? = nil
    var cliAuthCredentialsStore: String? = nil
    var forcedLoginMethod: String? = nil
    var modelReasoningEffort: String? = nil
    var planModeReasoningEffort: String? = nil
    var modelReasoningSummary: String? = nil
    var modelVerbosity: String? = nil
    var personality: String? = nil
    var serviceTier: String? = nil
    var webSearch: String? = nil
    var modelContextWindow: String? = nil
    var modelAutoCompactTokenLimit: String? = nil
    var toolOutputTokenLimit: String? = nil
    var features: [String: Bool?]? = nil
    var permissions: CodexSettingsPermissions? = nil
    var permissionProfiles: [String]? = nil
    var providers: [CodexSettingsProvider]? = nil
    var mcpServers: [CodexSettingsMCPServer]? = nil
    var plugins: [CodexSettingsPlugin]? = nil
    var advanced: CodexSettingsAdvanced? = nil

    enum CodingKeys: String, CodingKey {
        case model
        case reviewModel = "review_model"
        case modelProvider = "model_provider"
        case openAIBaseURL = "openai_base_url"
        case apiKey = "api_key"
        case cliAuthCredentialsStore = "cli_auth_credentials_store"
        case forcedLoginMethod = "forced_login_method"
        case modelReasoningEffort = "model_reasoning_effort"
        case planModeReasoningEffort = "plan_mode_reasoning_effort"
        case modelReasoningSummary = "model_reasoning_summary"
        case modelVerbosity = "model_verbosity"
        case personality
        case serviceTier = "service_tier"
        case webSearch = "web_search"
        case modelContextWindow = "model_context_window"
        case modelAutoCompactTokenLimit = "model_auto_compact_token_limit"
        case toolOutputTokenLimit = "tool_output_token_limit"
        case features
        case permissions
        case permissionProfiles = "permission_profiles"
        case providers
        case mcpServers = "mcp_servers"
        case plugins
        case advanced
    }
}

struct CodexSettingsPayload: Codable {
    var configText: String
    var authText: String
    var structured: CodexSettingsStructured
    var models: [CodexSettingsModel]
    var localBaseURL: String?
    var localAPIKey: String?
    var validationError: String?
    var warnings: [String]?
    var applied: Bool?

    enum CodingKeys: String, CodingKey {
        case configText = "config_text"
        case authText = "auth_text"
        case structured
        case models
        case localBaseURL = "local_base_url"
        case localAPIKey = "local_api_key"
        case validationError = "validation_error"
        case warnings
        case applied
    }
}

private struct CodexSettingsHelperError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private struct CodexSettingsSyncRequest: Encodable {
    var configText: String
    var authText: String
    var patch: CodexSettingsPatch?

    enum CodingKeys: String, CodingKey {
        case configText = "config_text"
        case authText = "auth_text"
        case patch
    }
}

private struct CodexSettingsPatch: Encodable {
    var structured: [String: CodexSettingsAnyValue]

    var isEmpty: Bool { structured.isEmpty }

    func merging(_ newer: CodexSettingsPatch) -> CodexSettingsPatch {
        CodexSettingsPatch(structured: structured.merging(newer.structured) { _, new in new })
    }
}

private struct CodexSettingsAnyValue: Encodable {
    private let encodeValue: (Encoder) throws -> Void

    init<Value: Encodable>(_ value: Value) {
        encodeValue = { encoder in try value.encode(to: encoder) }
    }

    static let null = CodexSettingsAnyValue(CodexSettingsNull())

    func encode(to encoder: Encoder) throws {
        try encodeValue(encoder)
    }
}

private struct CodexSettingsNull: Encodable {
    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encodeNil()
    }
}

private struct CodexSettingsLiteLLMSelection: Encodable {
    var model: String
    var provider: String
    var deploymentID: String

    enum CodingKeys: String, CodingKey {
        case model
        case provider
        case deploymentID = "deployment_id"
    }
}

private struct CodexSettingsDirectConnection: Encodable {
    var provider: String
    var baseURL: String?

    enum CodingKeys: String, CodingKey {
        case provider
        case baseURL = "base_url"
    }
}

// MARK: - Native editor

final class CodexConfigDialogController: NSObject, NSWindowDelegate, NSTextViewDelegate, NSTextFieldDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private enum ListTable {
        case providers
        case mcp
        case plugins
    }

    private let root: String
    private let bundleRoot: String
    private let environment: [String: String]
    private let onApplied: () -> Void
    private let onClose: () -> Void

    private var window: NSWindow?
    private var payload: CodexSettingsPayload?
    private var diskConfigText = ""
    private var diskAuthText = ""
    private var rawDraftIsValid = false
    private var isSynchronizing = false
    private var syncInFlight = false
    private var deferredSync: DispatchWorkItem?
    private var pendingStructuredPatch: CodexSettingsPatch?
    private var selectedProvider = -1
    private var selectedMCPServer = -1
    private var selectedPlugin = -1
    private var providers: [CodexSettingsProvider] = []
    private var mcpServers: [CodexSettingsMCPServer] = []
    private var plugins: [CodexSettingsPlugin] = []
    private var featureValues: [String: Bool?] = [:]
    private var lastLegacySandboxMode = "workspace-write"
    private var lastPermissionProfile = ":workspace"

    private let statusLabel = NSTextField(wrappingLabelWithString: "Loading Codex configuration…")
    private let validationLabel = NSTextField(wrappingLabelWithString: "")
    private let configTextView = NSTextView()
    private let authTextView = NSTextView()
    private let applyButton = NSButton(title: "Apply", target: nil, action: nil)
    private let closeButton = NSButton(title: "Close", target: nil, action: nil)
    private let reloadButton = NSButton(title: "Reload from Disk", target: nil, action: nil)

    private let deploymentPopup = NSPopUpButton()
    private let modelField = NSTextField()
    private let reviewModelField = NSTextField()
    private let providerPopup = NSPopUpButton()
    private let baseURLField = NSTextField()
    // API keys are configuration data in this editor, not passwords.  Keep
    // them visible so the structured control accurately mirrors auth.json.
    private let apiKeyField = NSTextField()
    private let authStorePopup = NSPopUpButton()
    private let forcedLoginPopup = NSPopUpButton()
    private let directConnectionHint = NSTextField(wrappingLabelWithString: "")
    private let reasoningPopup = NSPopUpButton()
    private let planReasoningPopup = NSPopUpButton()
    private let reasoningSummaryPopup = NSPopUpButton()
    private let verbosityPopup = NSPopUpButton()
    private let personalityPopup = NSPopUpButton()
    private let tierPopup = NSPopUpButton()
    private let webSearchPopup = NSPopUpButton()
    private let contextWindowField = NSTextField()
    private let autoCompactField = NSTextField()
    private let toolOutputField = NSTextField()
    private let permissionMode = NSSegmentedControl(labels: ["Legacy sandbox", "Permission profile"], trackingMode: .selectOne, target: nil, action: nil)
    private let sandboxPopup = NSPopUpButton()
    private let approvalPopup = NSPopUpButton()
    private let reviewerPopup = NSPopUpButton()
    private let permissionProfilePopup = NSPopUpButton()
    private let networkAccessCheckbox = NSButton(checkboxWithTitle: "Allow outbound network in workspace sandbox", target: nil, action: nil)
    private let writableRootsField = NSTextField()
    private let providerTable = NSTableView()
    private let providerIDField = NSTextField()
    private let providerNameField = NSTextField()
    private let providerBaseURLField = NSTextField()
    private let providerWireAPIPopup = NSPopUpButton()
    private let providerEnvKeyField = NSTextField()
    private let providerRequiresOpenAIAuth = NSButton(checkboxWithTitle: "Use OpenAI auth", target: nil, action: nil)
    private let providerAuthCommandField = NSTextField()
    private let mcpTable = NSTableView()
    private let mcpIDField = NSTextField()
    private let mcpTransportPopup = NSPopUpButton()
    private let mcpCommandField = NSTextField()
    private let mcpURLField = NSTextField()
    private let mcpEnabledCheckbox = NSButton(checkboxWithTitle: "Enabled", target: nil, action: nil)
    private let mcpRequiredCheckbox = NSButton(checkboxWithTitle: "Required at startup", target: nil, action: nil)
    private let pluginTable = NSTableView()
    private let pluginIDField = NSTextField()
    private let pluginEnabledCheckbox = NSButton(checkboxWithTitle: "Enabled", target: nil, action: nil)
    private let shellEnvironmentPopup = NSPopUpButton()
    private let historyPopup = NSPopUpButton()
    private let agentsThreadsField = NSTextField()
    private let agentsDepthField = NSTextField()
    private let fileOpenerPopup = NSPopUpButton()
    private let mcpCredentialStorePopup = NSPopUpButton()
    private var featureChecks: [String: NSButton] = [:]
    private let knownFeatures = [
        ("fast_mode", "Fast mode"),
        ("goals", "Persisted goals"),
        ("multi_agent", "Multi-agent"),
        ("apps", "Apps & connectors"),
        ("hooks", "Lifecycle hooks"),
        ("memories", "Memories (experimental)"),
        ("js_repl", "JavaScript REPL"),
        ("unified_exec", "Unified exec"),
        ("shell_snapshot", "Shell snapshot"),
        ("shell_tool", "Shell tool"),
        ("skill_mcp_dependency_install", "Skill MCP installs"),
        ("remote_plugin", "Remote plugin catalog"),
        ("personality", "Personality controls"),
        ("prevent_idle_sleep", "Prevent idle sleep"),
        ("network_proxy", "Network proxy (experimental)"),
    ]

    init(
        root: String,
        bundleRoot: String,
        environment: [String: String],
        onApplied: @escaping () -> Void,
        onClose: @escaping () -> Void
    ) {
        self.root = root
        self.bundleRoot = bundleRoot
        self.environment = environment
        self.onApplied = onApplied
        self.onClose = onClose
        super.init()
    }

    func showWindow() {
        if window == nil {
            buildWindow()
        }
        guard let window else { return }
        let shouldCenterBeforeOpening = !window.isVisible
        beginSettingsWindowPresentation(window)
        // A menu-bar app has no document-window cascade.  An unopened window
        // otherwise retains the (0, 0) origin supplied to contentRect, which
        // is a screen corner rather than the expected dialog position.
        if shouldCenterBeforeOpening {
            centerWindowInVisibleFrame(window)
        }
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        loadFromDisk()
    }

    private func centerWindowInVisibleFrame(_ window: NSWindow) {
        let mouseLocation = NSEvent.mouseLocation
        guard let screen = NSScreen.screens.first(where: { $0.frame.contains(mouseLocation) }) ?? NSScreen.main else {
            window.center()
            return
        }
        let visibleFrame = screen.visibleFrame
        let frame = window.frame
        window.setFrameOrigin(NSPoint(
            x: visibleFrame.midX - frame.width / 2,
            y: visibleFrame.midY - frame.height / 2
        ))
    }

    func windowWillClose(_ notification: Notification) {
        deferredSync?.cancel()
        onClose()
        if let closedWindow = notification.object as? NSWindow {
            endSettingsWindowPresentation(closedWindow)
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        requestClose()
        return false
    }

    private func buildWindow() {
        let nextWindow = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1120, height: 680),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        nextWindow.title = "Codex Settings"
        nextWindow.minSize = NSSize(width: 1020, height: 620)
        nextWindow.delegate = self
        nextWindow.isReleasedWhenClosed = false
        window = nextWindow

        let content = NSView()
        nextWindow.contentView = content

        let title = NSTextField(labelWithString: "Codex Settings")
        title.font = NSFont.systemFont(ofSize: 17, weight: .semibold)
        let subtitle = NSTextField(wrappingLabelWithString: "Structured controls and raw files share one draft. Codex uses config.toml (LiteLLM itself uses config.yaml). Nothing is written until Apply.")
        subtitle.textColor = .secondaryLabelColor
        subtitle.font = NSFont.systemFont(ofSize: 12)

        validationLabel.textColor = .systemRed
        validationLabel.font = NSFont.systemFont(ofSize: 12)
        validationLabel.maximumNumberOfLines = 2
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.font = NSFont.systemFont(ofSize: 12)
        statusLabel.maximumNumberOfLines = 2

        let split = NSSplitView()
        split.isVertical = true
        split.dividerStyle = .thin
        split.translatesAutoresizingMaskIntoConstraints = false
        let structuredPane = structuredPaneView()
        let rawPane = rawPaneView()
        split.addArrangedSubview(structuredPane)
        split.addArrangedSubview(rawPane)
        structuredPane.widthAnchor.constraint(greaterThanOrEqualToConstant: 420).isActive = true
        rawPane.widthAnchor.constraint(greaterThanOrEqualToConstant: 500).isActive = true
        let preferredLeft = structuredPane.widthAnchor.constraint(equalToConstant: 470)
        preferredLeft.priority = .defaultHigh
        preferredLeft.isActive = true

        reloadButton.target = self
        reloadButton.action = #selector(reloadAction(_:))
        reloadButton.bezelStyle = .rounded
        closeButton.target = self
        closeButton.action = #selector(closeAction(_:))
        closeButton.bezelStyle = .rounded
        closeButton.keyEquivalent = "\u{1b}"
        applyButton.target = self
        applyButton.action = #selector(applyAction(_:))
        applyButton.bezelStyle = .rounded
        applyButton.keyEquivalent = "\r"
        applyButton.isEnabled = false

        let buttons = NSStackView(views: [closeButton, applyButton])
        buttons.orientation = .horizontal
        buttons.spacing = 8
        let footer = NSStackView(views: [statusLabel, spacer(), reloadButton, buttons])
        footer.orientation = .horizontal
        footer.alignment = .centerY
        footer.spacing = 8

        for view in [title, subtitle, validationLabel, split, footer] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        NSLayoutConstraint.activate([
            title.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
            title.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -18),
            title.topAnchor.constraint(equalTo: content.topAnchor, constant: 16),
            subtitle.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            subtitle.trailingAnchor.constraint(equalTo: title.trailingAnchor),
            subtitle.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 4),
            validationLabel.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            validationLabel.trailingAnchor.constraint(equalTo: title.trailingAnchor),
            validationLabel.topAnchor.constraint(equalTo: subtitle.bottomAnchor, constant: 5),
            split.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            split.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            split.topAnchor.constraint(equalTo: validationLabel.bottomAnchor, constant: 8),
            split.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -10),
            footer.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            footer.trailingAnchor.constraint(equalTo: title.trailingAnchor),
            footer.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
            footer.heightAnchor.constraint(greaterThanOrEqualToConstant: 30),
            applyButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 86),
            closeButton.widthAnchor.constraint(greaterThanOrEqualToConstant: 86),
        ])
    }

    private func structuredPaneView() -> NSView {
        let pane = NSView()
        let heading = NSTextField(labelWithString: "Structured settings")
        heading.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false
        scroll.borderType = .bezelBorder
        let document = FlippedDocumentView()
        let form = NSStackView()
        form.orientation = .vertical
        form.alignment = .leading
        form.spacing = 16
        form.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(form)
        scroll.documentView = document
        document.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            document.heightAnchor.constraint(greaterThanOrEqualTo: scroll.contentView.heightAnchor),
            form.leadingAnchor.constraint(equalTo: document.leadingAnchor, constant: 16),
            form.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -16),
            form.topAnchor.constraint(equalTo: document.topAnchor, constant: 14),
            form.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -16),
        ])

        let sections = [
            liteLLMConnectionSection(),
            directConnectionSection(),
            behaviorSection(),
            featuresSection(),
            permissionsSection(),
            providersSection(),
            mcpPluginsSection(),
            advancedSection(),
        ]
        for section in sections {
            section.translatesAutoresizingMaskIntoConstraints = false
            form.addArrangedSubview(section)
            section.widthAnchor.constraint(equalTo: form.widthAnchor).isActive = true
        }

        for view in [heading, scroll] {
            view.translatesAutoresizingMaskIntoConstraints = false
            pane.addSubview(view)
        }
        NSLayoutConstraint.activate([
            heading.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 8),
            heading.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -8),
            heading.topAnchor.constraint(equalTo: pane.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 8),
            scroll.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -8),
            scroll.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: 7),
            scroll.bottomAnchor.constraint(equalTo: pane.bottomAnchor),
        ])
        return pane
    }

    private func rawPaneView() -> NSView {
        let pane = NSView()
        let heading = NSTextField(labelWithString: "Raw files — live draft")
        heading.font = NSFont.systemFont(ofSize: 14, weight: .semibold)
        let note = NSTextField(wrappingLabelWithString: "Edit either file directly. Valid TOML/JSON synchronizes these controls; invalid content stays visible and blocks Apply.")
        note.textColor = .secondaryLabelColor
        note.font = NSFont.systemFont(ofSize: 12)
        let configEditor = sourceEditor(title: "config.toml", textView: configTextView)
        let authEditor = sourceEditor(title: "auth.json", textView: authTextView)
        let editors = NSStackView(views: [configEditor, authEditor])
        editors.orientation = .vertical
        editors.alignment = .width
        editors.spacing = 10
        configEditor.heightAnchor.constraint(greaterThanOrEqualToConstant: 230).isActive = true
        authEditor.heightAnchor.constraint(greaterThanOrEqualToConstant: 180).isActive = true

        for view in [heading, note, editors] {
            view.translatesAutoresizingMaskIntoConstraints = false
            pane.addSubview(view)
        }
        NSLayoutConstraint.activate([
            heading.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 8),
            heading.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -8),
            heading.topAnchor.constraint(equalTo: pane.topAnchor),
            note.leadingAnchor.constraint(equalTo: heading.leadingAnchor),
            note.trailingAnchor.constraint(equalTo: heading.trailingAnchor),
            note.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: 4),
            editors.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 8),
            editors.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -8),
            editors.topAnchor.constraint(equalTo: note.bottomAnchor, constant: 8),
            editors.bottomAnchor.constraint(equalTo: pane.bottomAnchor),
        ])
        return pane
    }

    private func sourceEditor(title: String, textView: NSTextView) -> NSView {
        let container = NSView()
        let label = NSTextField(labelWithString: title)
        label.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .semibold)
        let scroll = NSScrollView()
        scroll.borderType = .bezelBorder
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        textView.isEditable = true
        textView.isSelectable = true
        textView.isRichText = false
        textView.allowsUndo = true
        textView.usesFindBar = true
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.textColor = .textColor
        textView.backgroundColor = .textBackgroundColor
        textView.textContainerInset = NSSize(width: 12, height: 10)
        textView.isHorizontallyResizable = true
        textView.isVerticallyResizable = true
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = false
        textView.textContainer?.containerSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.delegate = self
        textView.identifier = NSUserInterfaceItemIdentifier(
            title == "config.toml" ? "CodexRawConfigText" : "CodexRawAuthText"
        )
        scroll.documentView = textView
        for view in [label, scroll] {
            view.translatesAutoresizingMaskIntoConstraints = false
            container.addSubview(view)
        }
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            label.topAnchor.constraint(equalTo: container.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 5),
            scroll.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        return container
    }

    private func liteLLMConnectionSection() -> NSView {
        configurePopup(deploymentPopup, items: ["(Empty)"])
        deploymentPopup.target = self
        deploymentPopup.action = #selector(liteLLMDeploymentChanged(_:))
        return section(
            "LiteLLM deployment",
            help: "Optional shortcut for a model routed through this LiteLLM Menu. Selecting it stages the local endpoint and key; it does not write until Apply.",
            rows: [formRow("Deployment", deploymentPopup)]
        )
    }

    private func directConnectionSection() -> NSView {
        configureTextFields([modelField, reviewModelField, baseURLField, apiKeyField])
        configurePopup(providerPopup, items: ["openai", "amazon-bedrock", "ollama", "lmstudio"])
        configurePopup(authStorePopup, items: ["(Empty)", "file", "keyring", "auto", "ephemeral"])
        configurePopup(forcedLoginPopup, items: ["(Empty)", "chatgpt", "api"])
        directConnectionHint.textColor = .secondaryLabelColor
        directConnectionHint.font = NSFont.systemFont(ofSize: 11)
        directConnectionHint.maximumNumberOfLines = 2
        updateDirectConnectionControls()
        return section(
            "Direct model connection",
            help: "Use this only when Codex should bypass LiteLLM. It sets Codex's current default model; changes are staged until Apply.",
            rows: [
                formRow("Model", modelField),
                formRow("Review model", reviewModelField),
                formRow("Model provider", providerPopup),
                directConnectionHint,
                formRow("Endpoint URL", baseURLField),
                formRow("API key", apiKeyField),
                formRow("Credential store", authStorePopup),
                formRow("Forced login", forcedLoginPopup),
            ]
        )
    }

    private func behaviorSection() -> NSView {
        configurePopup(reasoningPopup, items: ["(Empty)", "minimal", "low", "medium", "high", "xhigh"])
        configurePopup(planReasoningPopup, items: ["(Empty)", "none", "minimal", "low", "medium", "high", "xhigh"])
        configurePopup(reasoningSummaryPopup, items: ["(Empty)", "auto", "concise", "detailed", "none"])
        configurePopup(verbosityPopup, items: ["(Empty)", "low", "medium", "high"])
        configurePopup(personalityPopup, items: ["(Empty)", "none", "friendly", "pragmatic"])
        configurePopup(tierPopup, items: ["(Empty)", "fast", "flex"])
        configurePopup(webSearchPopup, items: ["(Empty)", "disabled", "cached", "indexed", "live"])
        configureTextFields([contextWindowField, autoCompactField, toolOutputField])
        return section(
            "Behavior",
            help: "Use documented Responses-compatible model controls. Unrecognized compatibility keys remain in the raw file.",
            rows: [
                formRow("Reasoning effort", reasoningPopup),
                formRow("Plan reasoning", planReasoningPopup),
                formRow("Reasoning summary", reasoningSummaryPopup),
                formRow("Text verbosity", verbosityPopup),
                formRow("Personality", personalityPopup),
                formRow("Service tier", tierPopup),
                formRow("Web search", webSearchPopup),
                formRow("Context window", contextWindowField),
                formRow("Auto compact limit", autoCompactField),
                formRow("Tool output limit", toolOutputField),
            ]
        )
    }

    private func featuresSection() -> NSView {
        let grid = NSGridView()
        grid.rowSpacing = 5
        grid.columnSpacing = 18
        var rows: [[NSView]] = []
        var current: [NSView] = []
        for (key, label) in knownFeatures {
            let check = NSButton(checkboxWithTitle: label, target: self, action: #selector(structuredControlChanged(_:)))
            check.identifier = NSUserInterfaceItemIdentifier("feature.\(key)")
            featureChecks[key] = check
            current.append(check)
            if current.count == 2 {
                rows.append(current)
                current = []
            }
        }
        if !current.isEmpty {
            current.append(NSView())
            rows.append(current)
        }
        for row in rows { grid.addRow(with: row) }
        return section(
            "Features",
            help: "Only explicit choices are written. Omitted feature keys retain Codex defaults; deprecated web-search flags are left untouched in raw TOML.",
            rows: [grid]
        )
    }

    private func permissionsSection() -> NSView {
        permissionMode.target = self
        permissionMode.action = #selector(permissionModeChanged(_:))
        permissionMode.selectedSegment = 0
        configurePopup(sandboxPopup, items: ["read-only", "workspace-write", "danger-full-access"])
        configurePopup(approvalPopup, items: ["untrusted", "on-request", "never"])
        configurePopup(reviewerPopup, items: ["(Empty)", "user", "auto_review"])
        configurePopup(permissionProfilePopup, items: [":read-only", ":workspace", ":danger-full-access"])
        configureTextFields([writableRootsField])
        networkAccessCheckbox.target = self
        networkAccessCheckbox.action = #selector(structuredControlChanged(_:))
        let warning = NSTextField(wrappingLabelWithString: "Choose one execution-policy system: legacy sandbox or a permission profile. Approval settings work with either.")
        warning.textColor = .secondaryLabelColor
        warning.font = NSFont.systemFont(ofSize: 11)
        return section(
            "Permissions",
            help: "Apply replaces the other system. :workspace permits workspace writes; :danger-full-access removes Codex's local sandbox.",
            rows: [
                permissionMode,
                warning,
                formRow("Sandbox", sandboxPopup),
                formRow("Approval policy", approvalPopup),
                formRow("Approval reviewer", reviewerPopup),
                networkAccessCheckbox,
                formRow("Writable roots", writableRootsField),
                formRow("Permission profile", permissionProfilePopup),
            ]
        )
    }

    private func providersSection() -> NSView {
        configureListTable(providerTable, kind: .providers, columns: [("ID", 116), ("Base URL", 230), ("Auth", 84)])
        let list = listEditor(
            table: providerTable,
            add: #selector(addProvider(_:)),
            remove: #selector(removeProvider(_:)),
            addTip: "Add provider",
            removeTip: "Remove provider"
        )
        configureTextFields([providerIDField, providerNameField, providerBaseURLField, providerEnvKeyField, providerAuthCommandField])
        for field in [providerIDField, providerNameField, providerBaseURLField, providerEnvKeyField, providerAuthCommandField] {
            field.action = #selector(providerControlChanged(_:))
        }
        configurePopup(providerWireAPIPopup, items: ["responses"])
        providerRequiresOpenAIAuth.target = self
        providerRequiresOpenAIAuth.action = #selector(providerControlChanged(_:))
        return section(
            "Custom providers",
            help: "Custom providers bypass LiteLLM. Select one above to edit its endpoint. For API-key auth, enter the environment-variable name below; Codex reads the secret at launch.",
            rows: [
                list,
                formRow("Provider id", providerIDField),
                formRow("Display name", providerNameField),
                formRow("Base URL", providerBaseURLField),
                formRow("Wire API", providerWireAPIPopup),
                formRow("API-key environment variable", providerEnvKeyField),
                providerRequiresOpenAIAuth,
                formRow("Auth command", providerAuthCommandField),
            ]
        )
    }

    private func mcpPluginsSection() -> NSView {
        configureListTable(mcpTable, kind: .mcp, columns: [("MCP server", 138), ("Transport", 90), ("State", 70)])
        configureListTable(pluginTable, kind: .plugins, columns: [("Plugin", 180), ("State", 90)])
        let mcpList = listEditor(table: mcpTable, add: #selector(addMCP(_:)), remove: #selector(removeMCP(_:)), addTip: "Add MCP server", removeTip: "Remove MCP server")
        let pluginList = listEditor(table: pluginTable, add: #selector(addPlugin(_:)), remove: #selector(removePlugin(_:)), addTip: "Add plugin", removeTip: "Remove plugin")
        configureTextFields([mcpIDField, mcpCommandField, mcpURLField, pluginIDField])
        for field in [mcpIDField, mcpCommandField, mcpURLField, pluginIDField] {
            field.action = #selector(integrationControlChanged(_:))
        }
        // Codex supports stdio command servers and HTTP URL servers.  Do not
        // offer SSE here: the editor protocol deliberately rejects it rather
        // than generating a configuration Codex cannot load.
        configurePopup(mcpTransportPopup, items: ["stdio", "http"])
        for check in [mcpEnabledCheckbox, mcpRequiredCheckbox, pluginEnabledCheckbox] {
            check.target = self
            check.action = #selector(integrationControlChanged(_:))
        }
        let mcpFields = NSStackView(views: [
            formRow("Server id", mcpIDField),
            formRow("Transport", mcpTransportPopup),
            formRow("Command", mcpCommandField),
            formRow("URL", mcpURLField),
            mcpEnabledCheckbox,
            mcpRequiredCheckbox,
        ])
        mcpFields.orientation = .vertical
        mcpFields.alignment = .width
        mcpFields.spacing = 5
        let pluginFields = NSStackView(views: [formRow("Plugin id", pluginIDField), pluginEnabledCheckbox])
        pluginFields.orientation = .vertical
        pluginFields.alignment = .width
        pluginFields.spacing = 5
        return section(
            "MCP & plugins",
            help: "Basic server and plugin state is editable here. OAuth, tool policies, headers, environments, connector overlays, and other detailed configuration stay intact in raw TOML.",
            rows: [mcpList, mcpFields, separator(), pluginList, pluginFields]
        )
    }

    private func advancedSection() -> NSView {
        configurePopup(shellEnvironmentPopup, items: ["(Empty)", "all", "core", "none"])
        configurePopup(historyPopup, items: ["(Empty)", "save-all", "none"])
        configurePopup(fileOpenerPopup, items: ["(Empty)", "vscode", "vscode-insiders", "windsurf", "cursor", "none"])
        configurePopup(mcpCredentialStorePopup, items: ["(Empty)", "auto", "file", "keyring"])
        configureTextFields([agentsThreadsField, agentsDepthField])
        let note = NSTextField(wrappingLabelWithString: "Projects, marketplaces, desktop/notice state, telemetry, skills, app settings, detailed permissions, hooks, and unknown future keys remain fully preserved and editable in the raw files at right.")
        note.textColor = .secondaryLabelColor
        note.font = NSFont.systemFont(ofSize: 11)
        return section(
            "Advanced",
            help: "These common advanced settings have controls; the raw files remain the lossless view for every other Codex setting.",
            rows: [
                formRow("Shell environment", shellEnvironmentPopup),
                formRow("History", historyPopup),
                formRow("Agent threads", agentsThreadsField),
                formRow("Agent depth", agentsDepthField),
                formRow("File opener", fileOpenerPopup),
                formRow("MCP credential store", mcpCredentialStorePopup),
                note,
            ]
        )
    }

    private func section(_ title: String, help: String, rows: [NSView]) -> NSView {
        let container = NSView()
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 7
        let heading = NSTextField(labelWithString: title)
        heading.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        heading.alignment = .left
        let hint = NSTextField(wrappingLabelWithString: help)
        hint.textColor = .secondaryLabelColor
        hint.font = NSFont.systemFont(ofSize: 11)
        hint.alignment = .left
        let line = separator()
        for view in [heading, hint, line] + rows {
            view.translatesAutoresizingMaskIntoConstraints = false
            stack.addArrangedSubview(view)
            view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            stack.topAnchor.constraint(equalTo: container.topAnchor),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        return container
    }

    private func formRow(_ title: String, _ control: NSView) -> NSView {
        let row = NSView()
        let label = NSTextField(labelWithString: title)
        label.alignment = .right
        label.lineBreakMode = .byTruncatingTail
        label.setContentHuggingPriority(.required, for: .horizontal)
        label.setContentCompressionResistancePriority(.required, for: .horizontal)
        control.setContentHuggingPriority(.defaultLow, for: .horizontal)
        control.setContentCompressionResistancePriority(.defaultHigh, for: .horizontal)
        for view in [label, control] {
            view.translatesAutoresizingMaskIntoConstraints = false
            row.addSubview(view)
        }
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: row.leadingAnchor),
            label.widthAnchor.constraint(equalToConstant: 128),
            label.centerYAnchor.constraint(equalTo: row.centerYAnchor),
            control.leadingAnchor.constraint(equalTo: label.trailingAnchor, constant: 12),
            control.trailingAnchor.constraint(equalTo: row.trailingAnchor),
            control.topAnchor.constraint(equalTo: row.topAnchor),
            control.bottomAnchor.constraint(equalTo: row.bottomAnchor),
            control.widthAnchor.constraint(greaterThanOrEqualToConstant: 180),
            row.heightAnchor.constraint(greaterThanOrEqualToConstant: 26),
        ])
        return row
    }

    private func separator() -> NSView {
        let line = NSBox()
        line.boxType = .separator
        return line
    }

    private func spacer() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    private func configureTextFields(_ fields: [NSTextField]) {
        for field in fields {
            field.delegate = self
            field.target = self
            field.action = #selector(structuredControlChanged(_:))
            field.usesSingleLineMode = true
            field.lineBreakMode = .byTruncatingMiddle
            field.placeholderString = "(Empty)"
        }
    }

    private func configurePopup(_ popup: NSPopUpButton, items: [String]) {
        popup.removeAllItems()
        popup.addItems(withTitles: items)
        popup.target = self
        popup.action = #selector(structuredControlChanged(_:))
    }

    private func configureListTable(_ table: NSTableView, kind: ListTable, columns: [(String, CGFloat)]) {
        table.delegate = self
        table.dataSource = self
        table.allowsMultipleSelection = false
        table.allowsEmptySelection = true
        table.usesAlternatingRowBackgroundColors = false
        table.rowSizeStyle = .medium
        table.intercellSpacing = .zero
        table.focusRingType = .none
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        table.identifier = NSUserInterfaceItemIdentifier("CodexList.\(String(describing: kind))")
        for (offset, column) in columns.enumerated() {
            let tableColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("\(table.identifier!.rawValue).\(offset)"))
            tableColumn.title = column.0
            tableColumn.width = column.1
            table.addTableColumn(tableColumn)
        }
    }

    private func listEditor(table: NSTableView, add: Selector, remove: Selector, addTip: String, removeTip: String) -> NSView {
        let scroll = NSScrollView()
        scroll.borderType = .bezelBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.documentView = table
        scroll.heightAnchor.constraint(equalToConstant: 110).isActive = true
        let addButton = NSButton(title: "+", target: self, action: add)
        addButton.toolTip = addTip
        addButton.setAccessibilityLabel(addTip)
        let removeButton = NSButton(title: "−", target: self, action: remove)
        removeButton.toolTip = removeTip
        removeButton.setAccessibilityLabel(removeTip)
        let buttons = NSStackView(views: [addButton, removeButton])
        buttons.orientation = .vertical
        buttons.spacing = 5
        let row = NSStackView(views: [scroll, buttons])
        row.orientation = .horizontal
        row.alignment = .top
        row.spacing = 8
        return row
    }

    // MARK: Data flow

    private func loadFromDisk() {
        setBusy(true, status: "Loading current user-level Codex files…")
        runHelper(arguments: ["codex-config-editor-load"], input: nil) { [weak self] result in
            guard let self else { return }
            self.setBusy(false)
            switch result {
            case .success(let response):
                self.diskConfigText = response.configText
                self.diskAuthText = response.authText
                self.applyPayload(response, replaceRawText: true)
                self.statusLabel.stringValue = "Loaded config.toml and auth.json. Changes are staged until Apply."
            case .failure(let error):
                self.rawDraftIsValid = false
                self.validationLabel.stringValue = "Could not load Codex settings: \(error.message)"
                self.statusLabel.stringValue = "No files were changed."
                self.refreshApplyState()
            }
        }
    }

    func textDidChange(_ notification: Notification) {
        guard !isSynchronizing else { return }
        if let textView = notification.object as? NSTextView,
           textView === configTextView || textView === authTextView {
            // Raw files are the source of truth.  Do not later replay a stale
            // structured edit over text the user has just typed.
            pendingStructuredPatch = nil
            scheduleRawSync()
            return
        }
        if let field = notification.object as? NSTextField {
            scheduleStructuredSync(for: field)
        }
    }

    private func scheduleRawSync() {
        deferredSync?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.syncRawDraft(patch: nil, replaceRawText: false)
        }
        deferredSync = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35, execute: work)
    }

    private func scheduleStructuredSync(for control: NSView) {
        deferredSync?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.syncStructuredControl(control)
        }
        deferredSync = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: work)
    }

    private func syncStructuredControl(_ control: NSView) {
        guard !isSynchronizing, let patch = patch(for: control), !patch.isEmpty else { return }
        syncRawDraft(patch: patch, replaceRawText: true)
    }

    @objc private func structuredControlChanged(_ sender: Any?) {
        guard !isSynchronizing else { return }
        guard let patch = patch(for: sender as? NSView), !patch.isEmpty else { return }
        syncRawDraft(patch: patch, replaceRawText: true)
    }

    @objc private func liteLLMDeploymentChanged(_ sender: Any?) {
        guard !isSynchronizing,
              let payload,
              deploymentPopup.indexOfSelectedItem > 0
        else { return }
        let index = deploymentPopup.indexOfSelectedItem - 1
        guard payload.models.indices.contains(index) else { return }
        let selection = payload.models[index]
        let patch = CodexSettingsPatch(structured: [
            "litellm_model": CodexSettingsAnyValue(CodexSettingsLiteLLMSelection(
                model: selection.model,
                provider: selection.provider,
                deploymentID: selection.deploymentID
            )),
        ])
        syncRawDraft(patch: patch, replaceRawText: true)
    }

    @objc private func permissionModeChanged(_ sender: Any?) {
        guard !isSynchronizing else { return }
        let nextMode = permissionMode.selectedSegment == 1 ? "profile" : "legacy"
        if nextMode == "legacy", selectedValue(sandboxPopup).isEmpty {
            select(sandboxPopup, value: lastLegacySandboxMode)
        }
        if nextMode == "profile", selectedValue(permissionProfilePopup).isEmpty {
            select(permissionProfilePopup, value: lastPermissionProfile)
        }
        updatePermissionControlState()
        syncRawDraft(patch: permissionsPatch(), replaceRawText: true)
    }

    private func syncRawDraft(patch: CodexSettingsPatch?, replaceRawText: Bool) {
        guard !syncInFlight else {
            if let patch {
                pendingStructuredPatch = pendingStructuredPatch?.merging(patch) ?? patch
            } else {
                scheduleRawSync()
            }
            return
        }
        let request = CodexSettingsSyncRequest(
            configText: configTextView.string,
            authText: authTextView.string,
            patch: patch
        )
        guard let data = try? JSONEncoder().encode(request) else {
            showValidationError("Could not prepare the staged configuration.")
            return
        }
        syncInFlight = true
        statusLabel.stringValue = "Validating staged files…"
        runHelper(arguments: ["codex-config-editor-sync"], input: data) { [weak self] result in
            guard let self else { return }
            self.syncInFlight = false
            switch result {
            case .success(let response):
                self.applyPayload(response, replaceRawText: replaceRawText)
                self.statusLabel.stringValue = "Draft is valid. Apply writes both files."
            case .failure(let error):
                self.showValidationError(error.message)
            }
            if let pending = self.pendingStructuredPatch {
                self.pendingStructuredPatch = nil
                self.syncRawDraft(patch: pending, replaceRawText: true)
            }
        }
    }

    private func applyPayload(_ next: CodexSettingsPayload, replaceRawText: Bool) {
        isSynchronizing = true
        let hadPayload = payload != nil
        payload = next
        if replaceRawText {
            configTextView.string = next.configText
            authTextView.string = next.authText
        }
        rawDraftIsValid = next.validationError?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true
        // A parser failure returns an intentionally empty structured payload so
        // that no invalid source is echoed through a separate channel.  Keep
        // the left-side draft visible in that case: raw text remains the source
        // of truth, Apply is disabled, and no previously staged control is lost.
        if rawDraftIsValid || !hadPayload {
            providers = next.structured.providers ?? []
            mcpServers = next.structured.mcpServers ?? []
            plugins = next.structured.plugins ?? []
            populateDeploymentPopup(next.models, structured: next.structured, localBaseURL: next.localBaseURL)
            populateControls(next.structured)
            providerTable.reloadData()
            mcpTable.reloadData()
            pluginTable.reloadData()
            if selectedProvider >= providers.count { selectedProvider = providers.isEmpty ? -1 : 0 }
            if selectedMCPServer >= mcpServers.count { selectedMCPServer = mcpServers.isEmpty ? -1 : 0 }
            if selectedPlugin >= plugins.count { selectedPlugin = plugins.isEmpty ? -1 : 0 }
            selectListRows()
            renderSelectedProvider()
            renderSelectedMCP()
            renderSelectedPlugin()
        }
        let warnings = next.warnings?.filter { !$0.isEmpty } ?? []
        if let error = next.validationError, !error.isEmpty {
            validationLabel.stringValue = error
        } else if !warnings.isEmpty {
            validationLabel.textColor = .systemOrange
            validationLabel.stringValue = warnings.joined(separator: " · ")
        } else {
            validationLabel.textColor = .systemGreen
            validationLabel.stringValue = "Valid TOML and JSON — left controls and raw draft are synchronized."
        }
        isSynchronizing = false
        refreshApplyState()
    }

    private func showValidationError(_ message: String) {
        rawDraftIsValid = false
        validationLabel.textColor = .systemRed
        validationLabel.stringValue = "Invalid draft: \(singleLineDisplayText(message))"
        statusLabel.stringValue = "Fix the highlighted raw file content; nothing has been written."
        refreshApplyState()
    }

    private func refreshApplyState() {
        let dirty = configTextView.string != diskConfigText || authTextView.string != diskAuthText
        applyButton.isEnabled = rawDraftIsValid && dirty && !syncInFlight
        window?.isDocumentEdited = dirty
    }

    private func setBusy(_ busy: Bool, status: String? = nil) {
        if let status { statusLabel.stringValue = status }
        let dirty = configTextView.string != diskConfigText || authTextView.string != diskAuthText
        applyButton.isEnabled = !busy && rawDraftIsValid && dirty && !syncInFlight
        reloadButton.isEnabled = !busy
        window?.isDocumentEdited = dirty
    }

    private func populateDeploymentPopup(
        _ models: [CodexSettingsModel],
        structured: CodexSettingsStructured,
        localBaseURL: String?
    ) {
        deploymentPopup.removeAllItems()
        deploymentPopup.addItem(withTitle: "(Empty)")
        for model in models { deploymentPopup.addItem(withTitle: model.displayTitle) }
        let isUsingLocalLiteLLM = structured.modelProvider == "openai"
            && structured.openAIBaseURL == localBaseURL
        if isUsingLocalLiteLLM,
           let selectedModel = structured.model,
           let index = models.firstIndex(where: { $0.model == selectedModel }) {
            deploymentPopup.selectItem(at: index + 1)
        } else {
            deploymentPopup.selectItem(at: 0)
        }
    }

    private func populateControls(_ values: CodexSettingsStructured) {
        modelField.stringValue = values.model ?? ""
        reviewModelField.stringValue = values.reviewModel ?? ""
        populateModelProviderPopup(values.modelProvider)
        let selectedProvider = directProviderID()
        if selectedProvider == "openai" {
            baseURLField.stringValue = values.openAIBaseURL ?? ""
        } else if let provider = (values.providers ?? []).first(where: { $0.id == selectedProvider }) {
            baseURLField.stringValue = provider.baseURL ?? ""
        } else {
            baseURLField.stringValue = ""
        }
        apiKeyField.stringValue = values.apiKey ?? ""
        select(authStorePopup, value: values.cliAuthCredentialsStore)
        select(forcedLoginPopup, value: values.forcedLoginMethod)
        select(reasoningPopup, value: values.modelReasoningEffort)
        select(planReasoningPopup, value: values.planModeReasoningEffort)
        select(reasoningSummaryPopup, value: values.modelReasoningSummary)
        select(verbosityPopup, value: values.modelVerbosity)
        select(personalityPopup, value: values.personality)
        select(tierPopup, value: values.serviceTier)
        select(webSearchPopup, value: values.webSearch)
        contextWindowField.stringValue = values.modelContextWindow ?? ""
        autoCompactField.stringValue = values.modelAutoCompactTokenLimit ?? ""
        toolOutputField.stringValue = values.toolOutputTokenLimit ?? ""
        for (key, checkbox) in featureChecks {
            checkbox.state = (values.features?[key] ?? nil) == true ? .on : .off
        }
        featureValues = values.features ?? [:]
        let permissions = values.permissions ?? CodexSettingsPermissions()
        permissionMode.selectedSegment = permissions.mode == "profile" ? 1 : 0
        if let sandboxMode = permissions.sandboxMode, !sandboxMode.isEmpty {
            lastLegacySandboxMode = sandboxMode
        }
        if let profile = permissions.defaultPermissions, !profile.isEmpty {
            lastPermissionProfile = profile
        }
        select(sandboxPopup, value: permissions.sandboxMode)
        select(approvalPopup, value: permissions.approvalPolicy)
        select(reviewerPopup, value: permissions.approvalsReviewer)
        populatePermissionProfiles(values.permissionProfiles, selected: permissions.defaultPermissions)
        networkAccessCheckbox.state = permissions.networkAccess == true ? .on : .off
        writableRootsField.stringValue = (permissions.writableRoots ?? []).joined(separator: ", ")
        let advanced = values.advanced ?? CodexSettingsAdvanced()
        select(shellEnvironmentPopup, value: advanced.shellEnvironmentInherit)
        select(historyPopup, value: advanced.historyPersistence)
        agentsThreadsField.stringValue = advanced.agentsMaxThreads ?? ""
        agentsDepthField.stringValue = advanced.agentsMaxDepth ?? ""
        select(fileOpenerPopup, value: advanced.fileOpener)
        select(mcpCredentialStorePopup, value: advanced.mcpCredentialsStore)
        updatePermissionControlState()
        updateDirectConnectionControls()
    }

    private func populateModelProviderPopup(_ selected: String?) {
        let builtIns = ["openai", "amazon-bedrock", "ollama", "lmstudio"]
        let customProviders = providers.map(\.id).filter { !builtIns.contains($0) }.sorted()
        configurePopup(providerPopup, items: builtIns + customProviders)
        select(providerPopup, value: selected ?? "openai")
    }

    private func directProviderID() -> String {
        let selected = selectedValue(providerPopup)
        return selected.isEmpty ? "openai" : selected
    }

    private func endpointURL(for provider: String, openAIBaseURL: String? = nil) -> String {
        if provider == "openai" {
            return openAIBaseURL ?? payload?.structured.openAIBaseURL ?? ""
        }
        return providers.first(where: { $0.id == provider })?.baseURL ?? ""
    }

    private func populatePermissionProfiles(_ profiles: [String]?, selected: String?) {
        let builtIns = [":read-only", ":workspace", ":danger-full-access"]
        let values = builtIns + (profiles ?? []).filter { !builtIns.contains($0) }
        configurePopup(permissionProfilePopup, items: values)
        select(permissionProfilePopup, value: selected ?? lastPermissionProfile)
    }

    private func updateDirectConnectionControls() {
        let provider = directProviderID()
        if provider == "openai" {
            directConnectionHint.stringValue = "Built-in OpenAI-compatible endpoint. URL uses openai_base_url; API key uses Codex's selected credential store."
            baseURLField.isEnabled = true
            apiKeyField.isEnabled = true
            authStorePopup.isEnabled = true
            return
        }
        if let configured = providers.first(where: { $0.id == provider }) {
            if configured.requiresOpenAIAuth == true {
                directConnectionHint.stringValue = "Custom provider endpoint. URL writes [model_providers.\(configured.id)].base_url; it reuses the OpenAI-compatible key above."
            } else {
                directConnectionHint.stringValue = "Custom provider endpoint. URL writes [model_providers.\(configured.id)].base_url. Set its API-key environment variable below; Codex reads the secret at launch."
            }
            baseURLField.isEnabled = true
            apiKeyField.isEnabled = configured.requiresOpenAIAuth == true
            authStorePopup.isEnabled = configured.requiresOpenAIAuth == true
            return
        }
        directConnectionHint.stringValue = "This built-in provider does not use an editable OpenAI-compatible endpoint. Configure it in raw TOML if needed."
        baseURLField.isEnabled = false
        apiKeyField.isEnabled = false
        authStorePopup.isEnabled = false
    }

    private func select(_ popup: NSPopUpButton, value: String?) {
        let candidate = value ?? "(Empty)"
        if popup.itemTitles.contains(candidate) {
            popup.selectItem(withTitle: candidate)
        } else {
            popup.addItem(withTitle: candidate)
            popup.selectItem(withTitle: candidate)
        }
    }

    private func valuePatch(_ value: String?) -> CodexSettingsAnyValue {
        value.map(CodexSettingsAnyValue.init) ?? .null
    }

    private func corePatch(key: String, value: String?) -> CodexSettingsPatch {
        CodexSettingsPatch(structured: [key: valuePatch(value)])
    }

    private func permissionsPatch() -> CodexSettingsPatch {
        let permissionIsProfile = permissionMode.selectedSegment == 1
        let roots = writableRootsField.stringValue.split(separator: ",")
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        let permissions = CodexSettingsPermissions(
            mode: permissionIsProfile ? "profile" : "legacy",
            sandboxMode: permissionIsProfile ? nil : optionalText(selectedValue(sandboxPopup)),
            approvalPolicy: optionalText(selectedValue(approvalPopup)),
            approvalsReviewer: optionalText(selectedValue(reviewerPopup)),
            defaultPermissions: permissionIsProfile ? optionalText(selectedValue(permissionProfilePopup)) : nil,
            networkAccess: permissionIsProfile ? nil : networkAccessCheckbox.state == .on,
            writableRoots: permissionIsProfile ? nil : roots
        )
        return CodexSettingsPatch(structured: ["permissions": CodexSettingsAnyValue(permissions)])
    }

    private func listPatch() -> CodexSettingsPatch {
        CodexSettingsPatch(structured: [
            "providers": CodexSettingsAnyValue(providers),
            "mcp_servers": CodexSettingsAnyValue(mcpServers),
            "plugins": CodexSettingsAnyValue(plugins),
        ])
    }

    private func patch(for control: NSView?) -> CodexSettingsPatch? {
        guard let control else { return nil }
        if control === modelField { return corePatch(key: "model", value: optionalText(modelField.stringValue)) }
        if control === reviewModelField { return corePatch(key: "review_model", value: optionalText(reviewModelField.stringValue)) }
        if control === providerPopup {
            baseURLField.stringValue = endpointURL(for: directProviderID())
            updateDirectConnectionControls()
            return corePatch(key: "model_provider", value: directProviderID())
        }
        if control === baseURLField {
            return CodexSettingsPatch(structured: ["direct_connection": CodexSettingsAnyValue(
                CodexSettingsDirectConnection(
                    provider: directProviderID(),
                    baseURL: optionalText(baseURLField.stringValue)
                )
            )])
        }
        if control === apiKeyField { return corePatch(key: "api_key", value: optionalText(apiKeyField.stringValue)) }
        if control === authStorePopup { return corePatch(key: "cli_auth_credentials_store", value: optionalText(selectedValue(authStorePopup))) }
        if control === forcedLoginPopup { return corePatch(key: "forced_login_method", value: optionalText(selectedValue(forcedLoginPopup))) }
        if control === reasoningPopup { return corePatch(key: "model_reasoning_effort", value: optionalText(selectedValue(reasoningPopup))) }
        if control === planReasoningPopup { return corePatch(key: "plan_mode_reasoning_effort", value: optionalText(selectedValue(planReasoningPopup))) }
        if control === reasoningSummaryPopup { return corePatch(key: "model_reasoning_summary", value: optionalText(selectedValue(reasoningSummaryPopup))) }
        if control === verbosityPopup { return corePatch(key: "model_verbosity", value: optionalText(selectedValue(verbosityPopup))) }
        if control === personalityPopup { return corePatch(key: "personality", value: optionalText(selectedValue(personalityPopup))) }
        if control === tierPopup { return corePatch(key: "service_tier", value: optionalText(selectedValue(tierPopup))) }
        if control === webSearchPopup { return corePatch(key: "web_search", value: optionalText(selectedValue(webSearchPopup))) }
        if control === contextWindowField { return corePatch(key: "model_context_window", value: optionalText(contextWindowField.stringValue)) }
        if control === autoCompactField { return corePatch(key: "model_auto_compact_token_limit", value: optionalText(autoCompactField.stringValue)) }
        if control === toolOutputField { return corePatch(key: "tool_output_token_limit", value: optionalText(toolOutputField.stringValue)) }
        if control === sandboxPopup {
            return CodexSettingsPatch(structured: ["permissions": CodexSettingsAnyValue(CodexSettingsPermissions(sandboxMode: selectedValue(sandboxPopup)))])
        }
        if control === approvalPopup {
            return CodexSettingsPatch(structured: ["permissions": CodexSettingsAnyValue(CodexSettingsPermissions(approvalPolicy: selectedValue(approvalPopup)))])
        }
        if control === reviewerPopup {
            return CodexSettingsPatch(structured: ["permissions": CodexSettingsAnyValue(CodexSettingsPermissions(approvalsReviewer: optionalText(selectedValue(reviewerPopup))) )])
        }
        if control === permissionProfilePopup {
            return CodexSettingsPatch(structured: ["permissions": CodexSettingsAnyValue(CodexSettingsPermissions(defaultPermissions: selectedValue(permissionProfilePopup)))])
        }
        if control === networkAccessCheckbox {
            return CodexSettingsPatch(structured: ["permissions": CodexSettingsAnyValue(CodexSettingsPermissions(networkAccess: networkAccessCheckbox.state == .on))])
        }
        if control === writableRootsField {
            let roots = writableRootsField.stringValue.split(separator: ",")
                .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
            return CodexSettingsPatch(structured: ["permissions": CodexSettingsAnyValue(CodexSettingsPermissions(writableRoots: roots))])
        }
        if control === shellEnvironmentPopup {
            return CodexSettingsPatch(structured: ["advanced": CodexSettingsAnyValue(CodexSettingsAdvanced(shellEnvironmentInherit: optionalText(selectedValue(shellEnvironmentPopup))) )])
        }
        if control === historyPopup {
            return CodexSettingsPatch(structured: ["advanced": CodexSettingsAnyValue(CodexSettingsAdvanced(historyPersistence: optionalText(selectedValue(historyPopup))) )])
        }
        if control === agentsThreadsField {
            return CodexSettingsPatch(structured: ["advanced": CodexSettingsAnyValue(CodexSettingsAdvanced(agentsMaxThreads: optionalText(agentsThreadsField.stringValue)))])
        }
        if control === agentsDepthField {
            return CodexSettingsPatch(structured: ["advanced": CodexSettingsAnyValue(CodexSettingsAdvanced(agentsMaxDepth: optionalText(agentsDepthField.stringValue)))])
        }
        if control === fileOpenerPopup {
            return CodexSettingsPatch(structured: ["advanced": CodexSettingsAnyValue(CodexSettingsAdvanced(fileOpener: optionalText(selectedValue(fileOpenerPopup))) )])
        }
        if control === mcpCredentialStorePopup {
            return CodexSettingsPatch(structured: ["advanced": CodexSettingsAnyValue(CodexSettingsAdvanced(mcpCredentialsStore: optionalText(selectedValue(mcpCredentialStorePopup))) )])
        }
        if let button = control as? NSButton,
           let identifier = button.identifier?.rawValue,
           identifier.hasPrefix("feature.") {
            let key = String(identifier.dropFirst("feature.".count))
            featureValues[key] = button.state == .on
            return CodexSettingsPatch(structured: ["features": CodexSettingsAnyValue([key: featureValues[key] ?? nil])])
        }
        if control === providerWireAPIPopup || control === providerRequiresOpenAIAuth || control === providerIDField || control === providerNameField || control === providerBaseURLField || control === providerEnvKeyField || control === providerAuthCommandField {
            commitProviderFields()
            return listPatch()
        }
        if control === mcpTransportPopup || control === mcpIDField || control === mcpCommandField || control === mcpURLField || control === mcpEnabledCheckbox || control === mcpRequiredCheckbox || control === pluginIDField || control === pluginEnabledCheckbox {
            commitMCPFields()
            commitPluginFields()
            return listPatch()
        }
        return nil
    }

    private func updatePermissionControlState() {
        let useProfiles = permissionMode.selectedSegment == 1
        if useProfiles {
            let selected = selectedValue(permissionProfilePopup)
            if !selected.isEmpty { lastPermissionProfile = selected }
        } else if let selected = optionalText(selectedValue(sandboxPopup)) {
            lastLegacySandboxMode = selected
        }
        for control in [sandboxPopup, networkAccessCheckbox, writableRootsField] {
            control.isEnabled = !useProfiles
        }
        approvalPopup.isEnabled = true
        reviewerPopup.isEnabled = true
        permissionProfilePopup.isEnabled = useProfiles
    }

    private func selectedValue(_ popup: NSPopUpButton) -> String {
        let selected = popup.titleOfSelectedItem ?? ""
        return selected == "(Empty)" ? "" : selected
    }

    private func optionalText(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    // MARK: Provider / integration list editing

    func numberOfRows(in tableView: NSTableView) -> Int {
        if tableView === providerTable { return providers.count }
        if tableView === mcpTable { return mcpServers.count }
        if tableView === pluginTable { return plugins.count }
        return 0
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let column = tableColumn?.identifier.rawValue.components(separatedBy: ".").last ?? "0"
        let text: String
        if tableView === providerTable, providers.indices.contains(row) {
            let provider = providers[row]
            switch column {
            case "1": text = provider.baseURL ?? "—"
            case "2": text = provider.authCommand?.isEmpty == false ? "Command" : (provider.envKey?.isEmpty == false ? "Environment" : (provider.requiresOpenAIAuth == true ? "OpenAI" : "—"))
            default: text = provider.id
            }
        } else if tableView === mcpTable, mcpServers.indices.contains(row) {
            let server = mcpServers[row]
            switch column {
            case "1": text = server.transport ?? "stdio"
            case "2": text = server.enabled == false ? "Off" : "On"
            default: text = server.id
            }
        } else if tableView === pluginTable, plugins.indices.contains(row) {
            let plugin = plugins[row]
            text = column == "1" ? (plugin.enabled == false ? "Off" : "On") : plugin.id
        } else {
            text = ""
        }
        let label = NSTextField(labelWithString: text)
        label.lineBreakMode = .byTruncatingMiddle
        label.toolTip = text
        let cell = NSTableCellView()
        cell.textField = label
        label.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 6),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -6),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !isSynchronizing, let table = notification.object as? NSTableView else { return }
        if table === providerTable {
            selectedProvider = table.selectedRow
            renderSelectedProvider()
        } else if table === mcpTable {
            selectedMCPServer = table.selectedRow
            renderSelectedMCP()
        } else if table === pluginTable {
            selectedPlugin = table.selectedRow
            renderSelectedPlugin()
        }
    }

    private func selectListRows() {
        if selectedProvider >= 0 { providerTable.selectRowIndexes(IndexSet(integer: selectedProvider), byExtendingSelection: false) }
        if selectedMCPServer >= 0 { mcpTable.selectRowIndexes(IndexSet(integer: selectedMCPServer), byExtendingSelection: false) }
        if selectedPlugin >= 0 { pluginTable.selectRowIndexes(IndexSet(integer: selectedPlugin), byExtendingSelection: false) }
    }

    private func renderSelectedProvider() {
        let current = providers.indices.contains(selectedProvider) ? providers[selectedProvider] : nil
        providerIDField.stringValue = current?.id ?? ""
        providerNameField.stringValue = current?.name ?? ""
        providerBaseURLField.stringValue = current?.baseURL ?? ""
        select(providerWireAPIPopup, value: current?.wireAPI ?? "responses")
        providerEnvKeyField.stringValue = current?.envKey ?? ""
        providerRequiresOpenAIAuth.state = current?.requiresOpenAIAuth == true ? .on : .off
        providerAuthCommandField.stringValue = current?.authCommand ?? ""
        let enabled = current != nil
        for field in [providerIDField, providerNameField, providerBaseURLField, providerWireAPIPopup, providerEnvKeyField, providerRequiresOpenAIAuth, providerAuthCommandField] {
            field.isEnabled = enabled
        }
    }

    private func renderSelectedMCP() {
        let current = mcpServers.indices.contains(selectedMCPServer) ? mcpServers[selectedMCPServer] : nil
        mcpIDField.stringValue = current?.id ?? ""
        select(mcpTransportPopup, value: current?.transport ?? "stdio")
        mcpCommandField.stringValue = current?.command ?? ""
        mcpURLField.stringValue = current?.url ?? ""
        mcpEnabledCheckbox.state = current?.enabled == false ? .off : .on
        mcpRequiredCheckbox.state = current?.required == true ? .on : .off
        let enabled = current != nil
        for field in [mcpIDField, mcpTransportPopup, mcpCommandField, mcpURLField, mcpEnabledCheckbox, mcpRequiredCheckbox] {
            field.isEnabled = enabled
        }
    }

    private func renderSelectedPlugin() {
        let current = plugins.indices.contains(selectedPlugin) ? plugins[selectedPlugin] : nil
        pluginIDField.stringValue = current?.id ?? ""
        pluginEnabledCheckbox.state = current?.enabled == false ? .off : .on
        pluginIDField.isEnabled = current != nil
        pluginEnabledCheckbox.isEnabled = current != nil
    }

    @objc private func addProvider(_ sender: Any?) {
        providers.append(.blank())
        selectedProvider = providers.count - 1
        providerTable.reloadData()
        selectListRows()
        renderSelectedProvider()
        stageListChanges()
    }

    @objc private func removeProvider(_ sender: Any?) {
        guard providers.indices.contains(selectedProvider) else { return }
        providers.remove(at: selectedProvider)
        selectedProvider = providers.isEmpty ? -1 : min(selectedProvider, providers.count - 1)
        providerTable.reloadData()
        selectListRows()
        renderSelectedProvider()
        stageListChanges()
    }

    @objc private func providerControlChanged(_ sender: Any?) {
        commitProviderFields()
        stageListChanges()
    }

    @objc private func addMCP(_ sender: Any?) {
        mcpServers.append(.blank())
        selectedMCPServer = mcpServers.count - 1
        mcpTable.reloadData()
        selectListRows()
        renderSelectedMCP()
        stageListChanges()
    }

    @objc private func removeMCP(_ sender: Any?) {
        guard mcpServers.indices.contains(selectedMCPServer) else { return }
        mcpServers.remove(at: selectedMCPServer)
        selectedMCPServer = mcpServers.isEmpty ? -1 : min(selectedMCPServer, mcpServers.count - 1)
        mcpTable.reloadData()
        selectListRows()
        renderSelectedMCP()
        stageListChanges()
    }

    @objc private func addPlugin(_ sender: Any?) {
        plugins.append(.blank())
        selectedPlugin = plugins.count - 1
        pluginTable.reloadData()
        selectListRows()
        renderSelectedPlugin()
        stageListChanges()
    }

    @objc private func removePlugin(_ sender: Any?) {
        guard plugins.indices.contains(selectedPlugin) else { return }
        plugins.remove(at: selectedPlugin)
        selectedPlugin = plugins.isEmpty ? -1 : min(selectedPlugin, plugins.count - 1)
        pluginTable.reloadData()
        selectListRows()
        renderSelectedPlugin()
        stageListChanges()
    }

    @objc private func integrationControlChanged(_ sender: Any?) {
        commitMCPFields()
        commitPluginFields()
        stageListChanges()
    }

    func controlTextDidEndEditing(_ obj: Notification) {
        guard !isSynchronizing else { return }
        guard let control = obj.object as? NSView else { return }
        if control === providerIDField || control === providerNameField || control === providerBaseURLField || control === providerEnvKeyField || control === providerAuthCommandField {
            commitProviderFields()
            stageListChanges()
        } else if control === mcpIDField || control === mcpCommandField || control === mcpURLField || control === pluginIDField {
            commitMCPFields()
            commitPluginFields()
            stageListChanges()
        }
    }

    private func commitProviderFields() {
        guard providers.indices.contains(selectedProvider) else { return }
        providers[selectedProvider] = CodexSettingsProvider(
            id: providerIDField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            name: optionalText(providerNameField.stringValue),
            baseURL: optionalText(providerBaseURLField.stringValue),
            wireAPI: optionalText(selectedValue(providerWireAPIPopup)),
            envKey: optionalText(providerEnvKeyField.stringValue),
            requiresOpenAIAuth: providerRequiresOpenAIAuth.state == .on,
            authCommand: optionalText(providerAuthCommandField.stringValue),
            authMode: providers[selectedProvider].authMode
        )
        providerTable.reloadData()
    }

    private func commitMCPFields() {
        guard mcpServers.indices.contains(selectedMCPServer) else { return }
        mcpServers[selectedMCPServer] = CodexSettingsMCPServer(
            id: mcpIDField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            enabled: mcpEnabledCheckbox.state == .on,
            required: mcpRequiredCheckbox.state == .on,
            transport: optionalText(selectedValue(mcpTransportPopup)),
            command: optionalText(mcpCommandField.stringValue),
            url: optionalText(mcpURLField.stringValue)
        )
        mcpTable.reloadData()
    }

    private func commitPluginFields() {
        guard plugins.indices.contains(selectedPlugin) else { return }
        plugins[selectedPlugin] = CodexSettingsPlugin(
            id: pluginIDField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines),
            enabled: pluginEnabledCheckbox.state == .on
        )
        pluginTable.reloadData()
    }

    private func stageListChanges() {
        guard !isSynchronizing else { return }
        syncRawDraft(patch: listPatch(), replaceRawText: true)
    }

    // MARK: Apply / close

    @objc private func reloadAction(_ sender: Any?) {
        guard configTextView.string == diskConfigText, authTextView.string == diskAuthText else {
            let alert = NSAlert()
            alert.messageText = "Discard this Codex settings draft?"
            alert.informativeText = "Reloading reads config.toml and auth.json from disk and discards the unapplied draft."
            alert.alertStyle = .warning
            alert.addButton(withTitle: "Reload & Discard")
            alert.addButton(withTitle: "Keep Editing")
            alert.beginSheetModal(for: window!) { [weak self] result in
                if result == .alertFirstButtonReturn { self?.loadFromDisk() }
            }
            return
        }
        loadFromDisk()
    }

    @objc private func applyAction(_ sender: Any?) {
        guard rawDraftIsValid, !syncInFlight else { return }
        let request = CodexSettingsSyncRequest(configText: configTextView.string, authText: authTextView.string, patch: nil)
        guard let input = try? JSONEncoder().encode(request) else {
            showValidationError("Could not prepare files for Apply.")
            return
        }
        setBusy(true, status: "Revalidating and applying config.toml and auth.json…")
        runHelper(arguments: ["codex-config-editor-apply"], input: input) { [weak self] result in
            guard let self else { return }
            self.setBusy(false)
            switch result {
            case .success(let response):
                self.diskConfigText = response.configText
                self.diskAuthText = response.authText
                self.applyPayload(response, replaceRawText: true)
                self.statusLabel.stringValue = "Applied user-level Codex settings."
                self.onApplied()
            case .failure(let error):
                self.showValidationError(error.message)
            }
        }
    }

    @objc private func closeAction(_ sender: Any?) {
        requestClose()
    }

    private func requestClose() {
        guard let window else { return }
        guard configTextView.string != diskConfigText || authTextView.string != diskAuthText else {
            window.close()
            return
        }
        let alert = NSAlert()
        alert.messageText = "Discard unapplied Codex settings?"
        alert.informativeText = "Close does not write config.toml or auth.json. Choose Apply to save the draft first."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Discard Changes")
        alert.addButton(withTitle: "Keep Editing")
        alert.beginSheetModal(for: window) { response in
            if response == .alertFirstButtonReturn { window.close() }
        }
    }

    // MARK: Helper process

    private func runHelper(arguments: [String], input: Data?, completion: @escaping (Result<CodexSettingsPayload, CodexSettingsHelperError>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [bundleRoot, environment] in
            let process = Process()
            let output = Pipe()
            let errors = Pipe()
            let stdin = input == nil ? nil : Pipe()
            process.executableURL = URL(fileURLWithPath: "/bin/bash")
            process.arguments = ["\(bundleRoot)/service.sh"] + arguments
            process.environment = environment
            process.standardOutput = output
            process.standardError = errors
            process.standardInput = stdin
            do {
                try process.run()
                if let input, let stdin {
                    stdin.fileHandleForWriting.write(input)
                    stdin.fileHandleForWriting.closeFile()
                }
                process.waitUntilExit()
                let outputData = output.fileHandleForReading.readDataToEndOfFile()
                let errorData = errors.fileHandleForReading.readDataToEndOfFile()
                let response: Result<CodexSettingsPayload, CodexSettingsHelperError>
                if process.terminationStatus == 0 {
                    do {
                        response = .success(try JSONDecoder().decode(CodexSettingsPayload.self, from: outputData))
                    } catch {
                        response = .failure(CodexSettingsHelperError(message: "The settings helper returned an invalid response."))
                    }
                } else {
                    // Do not echo helper output here: it can refer to secret-bearing source files.
                    _ = errorData
                    response = .failure(CodexSettingsHelperError(message: "Codex settings validation failed. Correct the staged file syntax or configuration conflict and try again."))
                }
                DispatchQueue.main.async { completion(response) }
            } catch {
                DispatchQueue.main.async { completion(.failure(CodexSettingsHelperError(message: "The Codex settings helper could not start."))) }
            }
        }
    }
}
