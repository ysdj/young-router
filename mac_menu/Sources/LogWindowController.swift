import Cocoa

enum LogWindowTab: String, CaseIterable {
    case requests
    case service
    case menu
    case configWatch
    case routeTrace
    case recovery
    case remoteUsage

    var title: String {
        switch self {
        case .requests:
            return "Requests"
        case .service:
            return "Service"
        case .menu:
            return "Menu"
        case .configWatch:
            return "Config Watch"
        case .routeTrace:
            return "Route Trace"
        case .recovery:
            return "Recovery"
        case .remoteUsage:
            return "Online Usage"
        }
    }

    var emptyMessage: String {
        switch self {
        case .requests:
            return "No request summaries yet."
        case .service:
            return "No service log entries yet."
        case .menu:
            return "No menu action entries yet."
        case .configWatch:
            return "No configuration-watch entries yet."
        case .routeTrace:
            return "No route-trace records yet."
        case .recovery:
            return "No active recovery or cooldown records."
        case .remoteUsage:
            return "No online usage records yet."
        }
    }
}

/// Native, bounded log reader for the menu-owned runtime.  It intentionally reads
/// only local files (or the bundled, sanitized online-usage command), so the viewer
/// remains live without opening a browser or retaining a second copy of any log.
final class LogWindowController: NSWindowController, NSWindowDelegate, NSTabViewDelegate, NSTextFieldDelegate {
    private struct LogSnapshot {
        let text: String
        let lineCount: Int
        let wasTrimmed: Bool
        let sourceExists: Bool
    }

    private struct TabView {
        let item: NSTabViewItem
        let scrollView: NSScrollView
        let textView: NSTextView
    }

    /// Every tab renders rows through the same four-column grid.  Individual
    /// producers naturally expose different fields, but a fixed visual shape
    /// keeps the viewer scannable and makes timestamps comparable between tabs.
    private struct LogRow {
        let timestamp: String
        let source: String
        let status: String
        let detail: String
    }

    private let runtimeRoot: String
    private let bundleRoot: String
    private let maximumLines: Int
    private let maximumReadBytes: UInt64
    private let refreshInterval: TimeInterval = 0.8
    private let remoteRefreshInterval: TimeInterval = 8

    private let tabView = NSTabView()
    private let filterField = NSTextField()
    private let pauseButton = NSButton(title: "Pause", target: nil, action: nil)
    private let clearButton = NSButton(title: "Clear View", target: nil, action: nil)
    private let refreshButton = NSButton(title: "Refresh", target: nil, action: nil)
    private let infoLabel = NSTextField(labelWithString: "Ready")

    private var tabViews: [LogWindowTab: TabView] = [:]
    private var latestSnapshots: [LogWindowTab: LogSnapshot] = [:]
    private var renderedText: [LogWindowTab: String] = [:]
    private var filters: [LogWindowTab: String] = [:]
    private var clearedTabs: Set<LogWindowTab> = []
    private var refreshInFlight: Set<LogWindowTab> = []
    private var refreshTimer: Timer?
    private var lastRemoteRefresh: Date?
    private var isPaused = false
    private var isSynchronizingFilter = false
    private var hasPresented = false

    private static let timestampFormatterLock = NSLock()
    private static let localTimestampFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()
    private static let absoluteTimestampFormatters: [DateFormatter] = [
        "yyyy-MM-dd HH:mm:ss.SSSSSS ZZZZ",
        "yyyy-MM-dd HH:mm:ss.SSS ZZZZ",
        "yyyy-MM-dd HH:mm:ss ZZZZ",
        "yyyy-MM-dd HH:mm:ss.SSSSSS ZZZZZ",
        "yyyy-MM-dd HH:mm:ss.SSS ZZZZZ",
        "yyyy-MM-dd HH:mm:ss ZZZZZ",
    ].map { format in
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = format
        return formatter
    }

    init(runtimeRoot: String, bundleRoot: String, maximumLines: Int = 1_200) {
        self.runtimeRoot = (runtimeRoot as NSString).expandingTildeInPath
        self.bundleRoot = (bundleRoot as NSString).expandingTildeInPath
        self.maximumLines = min(max(maximumLines, 100), 5_000)
        // A bounded tail keeps refreshes cheap even if a user raises the local
        // on-disk cap.  Formatting happens off the main thread below.
        self.maximumReadBytes = 1_500_000
        super.init(window: nil)
        buildWindow()
    }

    required init?(coder: NSCoder) {
        nil
    }

    deinit {
        refreshTimer?.invalidate()
    }

    /// Presents the reusable window and starts from the requested live tab.
    func show(initialTab: LogWindowTab = .requests) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in
                self?.show(initialTab: initialTab)
            }
            return
        }

        select(initialTab)
        refresh(initialTab, force: true)
        startRefreshing()
        guard let window else { return }
        beginSettingsWindowPresentation(window)
        if !hasPresented {
            window.center()
            hasPresented = true
        }
        window.makeKeyAndOrderFront(nil)
    }

    /// String convenience for callers that only have a menu label at hand.
    func show(initialTab: String) {
        let normalized = initialTab.trimmingCharacters(in: .whitespacesAndNewlines)
        let tab = LogWindowTab.allCases.first {
            $0.rawValue.caseInsensitiveCompare(normalized) == .orderedSame
                || $0.title.caseInsensitiveCompare(normalized) == .orderedSame
        } ?? .requests
        show(initialTab: tab)
    }

    func windowWillClose(_ notification: Notification) {
        refreshTimer?.invalidate()
        refreshTimer = nil
        if let closedWindow = notification.object as? NSWindow {
            endSettingsWindowPresentation(closedWindow)
        }
    }

    func windowDidBecomeKey(_ notification: Notification) {
        refresh(selectedTab)
    }

    func tabView(_ tabView: NSTabView, didSelect tabViewItem: NSTabViewItem?) {
        synchronizeFilterField()
        redraw(selectedTab)
        refresh(selectedTab)
    }

    func controlTextDidChange(_ obj: Notification) {
        guard obj.object as? NSTextField === filterField, !isSynchronizingFilter else { return }
        filters[selectedTab] = filterField.stringValue
        redraw(selectedTab)
    }

    @objc private func refreshCurrentTab(_ sender: Any?) {
        refresh(selectedTab, force: true)
    }

    @objc private func togglePause(_ sender: Any?) {
        isPaused.toggle()
        pauseButton.title = isPaused ? "Resume" : "Pause"
        if isPaused {
            updateInfoBar(for: selectedTab)
        } else {
            refresh(selectedTab)
        }
    }

    @objc private func clearCurrentView(_ sender: Any?) {
        let tab = selectedTab
        clearedTabs.insert(tab)
        renderedText.removeValue(forKey: tab)
        guard let view = tabViews[tab] else { return }
        replaceText(
            Self.renderedRows([
                Self.noticeRow("Display cleared. Refresh to show the latest retained records.", source: tab.title),
            ]).joined(separator: "\n"),
            in: view
        )
        updateInfoBar(for: tab)
    }

    private func buildWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 580),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "LiteLLM Menu Logs"
        window.minSize = NSSize(width: 640, height: 420)
        window.delegate = self
        self.window = window

        let content = NSView()
        content.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = content

        let toolbar = NSStackView()
        toolbar.orientation = .horizontal
        toolbar.alignment = .centerY
        toolbar.spacing = 8
        toolbar.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(toolbar)

        let filterLabel = NSTextField(labelWithString: "Filter")
        filterLabel.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        toolbar.addArrangedSubview(filterLabel)

        filterField.placeholderString = "Filter current tab"
        filterField.delegate = self
        filterField.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        filterField.translatesAutoresizingMaskIntoConstraints = false
        filterField.widthAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true
        filterField.widthAnchor.constraint(lessThanOrEqualToConstant: 360).isActive = true
        toolbar.addArrangedSubview(filterField)

        let toolbarSpacer = NSView()
        toolbarSpacer.translatesAutoresizingMaskIntoConstraints = false
        toolbarSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        toolbar.addArrangedSubview(toolbarSpacer)

        for button in [pauseButton, clearButton, refreshButton] {
            button.target = self
            button.bezelStyle = .rounded
            button.setButtonType(.momentaryPushIn)
            toolbar.addArrangedSubview(button)
        }
        pauseButton.action = #selector(togglePause(_:))
        clearButton.action = #selector(clearCurrentView(_:))
        refreshButton.action = #selector(refreshCurrentTab(_:))

        tabView.translatesAutoresizingMaskIntoConstraints = false
        tabView.tabViewType = .topTabsBezelBorder
        tabView.delegate = self
        for tab in LogWindowTab.allCases {
            let tabContent = makeTabView(for: tab)
            tabViews[tab] = tabContent
            tabView.addTabViewItem(tabContent.item)
        }
        content.addSubview(tabView)

        let divider = NSBox()
        divider.boxType = .separator
        divider.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(divider)

        infoLabel.font = NSFont.systemFont(ofSize: NSFont.smallSystemFontSize)
        infoLabel.lineBreakMode = .byTruncatingTail
        infoLabel.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(infoLabel)

        NSLayoutConstraint.activate([
            toolbar.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 10),
            toolbar.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -10),
            toolbar.topAnchor.constraint(equalTo: content.topAnchor, constant: 8),
            toolbar.heightAnchor.constraint(equalToConstant: 26),

            tabView.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 8),
            tabView.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -8),
            tabView.topAnchor.constraint(equalTo: toolbar.bottomAnchor, constant: 4),
            tabView.bottomAnchor.constraint(equalTo: divider.topAnchor, constant: -4),

            divider.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 8),
            divider.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -8),
            divider.bottomAnchor.constraint(equalTo: infoLabel.topAnchor, constant: -4),

            infoLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 12),
            infoLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),
            infoLabel.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -6),
            infoLabel.heightAnchor.constraint(equalToConstant: 17),
        ])
    }

    private func makeTabView(for tab: LogWindowTab) -> TabView {
        let item = NSTabViewItem(identifier: tab.rawValue)
        item.label = tab.title

        // NSTabView sizes an item's root view with its autoresizing mask rather
        // than constraints owned by the enclosing window.  Disabling autoresize
        // translation here leaves the content at a zero-sized frame: data still
        // loads and is counted, but the scroll/text view has no visible area.
        let content = NSView(frame: .zero)
        content.translatesAutoresizingMaskIntoConstraints = true
        content.autoresizingMask = [.width, .height]
        item.view = content

        let scrollView = NSScrollView()
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        scrollView.hasVerticalScroller = true
        // Logs are deliberately single-line rows.  Preserve their full content
        // and let the scroll view expose it horizontally instead of wrapping or
        // eliding a long route/error field.
        scrollView.hasHorizontalScroller = true
        scrollView.autohidesScrollers = true
        scrollView.borderType = .noBorder

        let textView = NSTextView(frame: NSRect(x: 0, y: 0, width: 760, height: 420))
        textView.isEditable = false
        textView.isSelectable = true
        textView.allowsUndo = false
        textView.usesFindBar = true
        textView.isRichText = false
        textView.importsGraphics = false
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        // `NSTextView` can inherit an inactive/transparent semantic color when
        // this window is created from an accessory menu-bar app.  That makes a
        // populated log appear blank even though the status bar correctly
        // reports records. Use an explicit dynamic label color and always make
        // the document view opaque so each tab remains readable on both system
        // appearances.
        textView.textColor = NSColor.labelColor
        textView.backgroundColor = .textBackgroundColor
        textView.drawsBackground = true
        textView.textContainerInset = NSSize(width: 8, height: 7)
        textView.isHorizontallyResizable = true
        textView.isVerticallyResizable = true
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.autoresizingMask = [.height]
        textView.textContainer?.widthTracksTextView = false
        textView.textContainer?.containerSize = NSSize(
            width: CGFloat.greatestFiniteMagnitude,
            height: CGFloat.greatestFiniteMagnitude
        )
        textView.string = Self.renderedRows([
            Self.noticeRow("Loading latest records…", source: tab.title),
        ]).joined(separator: "\n")
        scrollView.documentView = textView
        content.addSubview(scrollView)

        NSLayoutConstraint.activate([
            scrollView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scrollView.topAnchor.constraint(equalTo: content.topAnchor),
            scrollView.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])

        return TabView(item: item, scrollView: scrollView, textView: textView)
    }

    private var selectedTab: LogWindowTab {
        guard let identifier = tabView.selectedTabViewItem?.identifier as? String,
              let tab = LogWindowTab(rawValue: identifier)
        else {
            return .requests
        }
        return tab
    }

    private func select(_ tab: LogWindowTab) {
        guard let item = tabViews[tab]?.item else { return }
        tabView.selectTabViewItem(item)
        synchronizeFilterField()
    }

    private func synchronizeFilterField() {
        isSynchronizingFilter = true
        filterField.stringValue = filters[selectedTab] ?? ""
        isSynchronizingFilter = false
    }

    private func startRefreshing() {
        guard refreshTimer == nil else { return }
        let timer = Timer(timeInterval: refreshInterval, repeats: true) { [weak self] _ in
            guard let self, !self.isPaused else { return }
            let tab = self.selectedTab
            if tab == .remoteUsage,
               let previous = self.lastRemoteRefresh,
               Date().timeIntervalSince(previous) < self.remoteRefreshInterval {
                return
            }
            self.refresh(tab)
        }
        refreshTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func refresh(_ tab: LogWindowTab, force: Bool = false) {
        guard force || !isPaused else { return }
        guard force || !clearedTabs.contains(tab) else { return }
        guard !refreshInFlight.contains(tab) else { return }
        if force {
            clearedTabs.remove(tab)
        }
        if tab == .remoteUsage {
            refreshRemoteUsage(force: force)
            return
        }

        refreshInFlight.insert(tab)
        let path = sourcePath(for: tab)
        let recoveryStatePath = (runtimeRoot as NSString).appendingPathComponent(
            ".litellm-runtime/route-recovery-state.json"
        )
        let cooldownStatePath = (runtimeRoot as NSString).appendingPathComponent(
            ".litellm-runtime/deployment-cooldowns.json"
        )
        let maximumLines = maximumLines
        let maximumReadBytes = maximumReadBytes

        DispatchQueue.global(qos: .utility).async { [weak self] in
            let snapshot: LogSnapshot
            if tab == .recovery {
                snapshot = Self.readRecoverySnapshot(
                    recoveryStatePath: recoveryStatePath,
                    cooldownStatePath: cooldownStatePath
                )
            } else {
                snapshot = Self.readSnapshot(
                    at: path,
                    tab: tab,
                    maximumLines: maximumLines,
                    maximumReadBytes: maximumReadBytes
                )
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.refreshInFlight.remove(tab)
                self.apply(snapshot, to: tab)
            }
        }
    }

    private func refreshRemoteUsage(force: Bool) {
        refreshInFlight.insert(.remoteUsage)
        let service = (bundleRoot as NSString).appendingPathComponent("service.sh")
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = Self.readRemoteUsage(service: service)
            let formatted = Self.formattedRemoteUsageText(result)
            DispatchQueue.main.async {
                guard let self else { return }
                self.refreshInFlight.remove(.remoteUsage)
                self.lastRemoteRefresh = Date()
                self.apply(
                    LogSnapshot(
                        text: formatted,
                        lineCount: Self.onlineUsageRecordCount(in: formatted),
                        wasTrimmed: false,
                        sourceExists: true
                    ),
                    to: .remoteUsage
                )
            }
        }
    }

    private func sourcePath(for tab: LogWindowTab) -> String {
        let name: String
        switch tab {
        case .requests:
            name = "recent-requests.jsonl"
        case .service, .routeTrace:
            name = "menu-server.log"
        case .menu:
            name = "menu-actions.log"
        case .configWatch:
            name = "config-watch.log"
        case .recovery, .remoteUsage:
            name = ""
        }
        return (runtimeRoot as NSString).appendingPathComponent(name)
    }

    private func apply(_ snapshot: LogSnapshot, to tab: LogWindowTab) {
        latestSnapshots[tab] = snapshot
        guard !clearedTabs.contains(tab) else {
            if tab == selectedTab { updateInfoBar(for: tab) }
            return
        }
        redraw(tab)
    }

    private func redraw(_ tab: LogWindowTab) {
        guard let view = tabViews[tab] else { return }
        guard !clearedTabs.contains(tab) else {
            if tab == selectedTab { updateInfoBar(for: tab) }
            return
        }
        guard let snapshot = latestSnapshots[tab] else {
            if tab == selectedTab {
                infoLabel.stringValue = "\(tab.title) · Loading latest records…"
            }
            return
        }

        let text = filteredText(snapshot.text, query: filters[tab] ?? "", tab: tab)
        if renderedText[tab] != text {
            replaceText(text, in: view)
            renderedText[tab] = text
        }
        if tab == selectedTab { updateInfoBar(for: tab) }
    }

    private func filteredText(_ text: String, query: String, tab: LogWindowTab) -> String {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return text }
        let lines = text.components(separatedBy: .newlines)
        let matchingIndexes = lines.indices.filter {
            lines[$0].range(of: needle, options: .caseInsensitive) != nil
        }
        guard !matchingIndexes.isEmpty else {
            return "No displayed records match \"\(String(needle.prefix(80)))\"."
        }
        var includedIndexes = Set(matchingIndexes)
        for index in matchingIndexes where index >= 2 && Self.isTableDivider(lines[index - 1]) {
            // Keep the table labels with a filtered row.  The optional section
            // heading makes Recovery and Online Usage results understandable
            // without bringing back the unfiltered raw record pile.
            includedIndexes.insert(index - 2)
            includedIndexes.insert(index - 1)
            if index >= 3, !lines[index - 3].isEmpty, !Self.isTableDivider(lines[index - 3]) {
                includedIndexes.insert(index - 3)
            }
        }
        return lines.indices.filter { includedIndexes.contains($0) }
            .map { lines[$0] }
            .joined(separator: "\n")
    }

    private func updateInfoBar(for tab: LogWindowTab) {
        if clearedTabs.contains(tab) {
            infoLabel.stringValue = "\(tab.title) · Display cleared · Refresh to load the latest retained records"
            return
        }
        guard let snapshot = latestSnapshots[tab] else {
            infoLabel.stringValue = "\(tab.title) · Loading latest records…"
            return
        }

        var parts = [tab.title]
        parts.append(snapshot.sourceExists ? "\(snapshot.lineCount) records" : "Waiting for log file")
        if snapshot.wasTrimmed {
            parts.append("showing latest \(maximumLines)")
        }
        if let filter = filters[tab]?.trimmingCharacters(in: .whitespacesAndNewlines), !filter.isEmpty {
            parts.append("filter: \(String(filter.prefix(80)))")
        }
        if isPaused { parts.append("paused") }
        parts.append("updated \(Self.updateTimeFormatter.string(from: Date()))")
        infoLabel.stringValue = parts.joined(separator: " · ")
    }

    private func replaceText(_ text: String, in view: TabView) {
        let shouldFollow = isAtBottom(view.scrollView)
        let priorOrigin = view.scrollView.contentView.bounds.origin

        // Do not rely on NSTextView.textColor here: it controls typing/default
        // attributes, but an existing text storage can retain an inherited
        // inactive/transparent foreground color.  That was the source of the
        // "records count, blank body" failure in a menu-bar-launched window.
        // Replacing the complete attributed storage gives every rendered line
        // an explicit dynamic foreground color and also invalidates the glyph
        // range when an inactive tab is first laid out.
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
            .foregroundColor: NSColor.labelColor,
        ]
        let rendered = NSAttributedString(string: text, attributes: attributes)
        if let storage = view.textView.textStorage {
            storage.setAttributedString(rendered)
        } else {
            view.textView.string = text
        }
        view.textView.typingAttributes = attributes
        view.textView.textColor = NSColor.labelColor
        view.textView.backgroundColor = .textBackgroundColor
        view.textView.needsDisplay = true
        view.scrollView.contentView.needsDisplay = true
        view.textView.layoutManager?.ensureLayout(for: view.textView.textContainer!)
        view.textView.needsLayout = true
        view.textView.layoutSubtreeIfNeeded()

        if shouldFollow {
            scrollToBottom(view.scrollView, horizontalOrigin: priorOrigin.x)
        } else {
            let maximumOrigin = maximumScrollOrigin(for: view.scrollView)
            let restoredOrigin = NSPoint(
                x: min(max(0, priorOrigin.x), maximumHorizontalScrollOrigin(for: view.scrollView)),
                y: min(max(0, priorOrigin.y), maximumOrigin)
            )
            view.scrollView.contentView.scroll(to: restoredOrigin)
            view.scrollView.reflectScrolledClipView(view.scrollView.contentView)
        }
    }

    private func isAtBottom(_ scrollView: NSScrollView) -> Bool {
        scrollView.contentView.bounds.origin.y >= maximumScrollOrigin(for: scrollView) - 4
    }

    private func scrollToBottom(_ scrollView: NSScrollView, horizontalOrigin: CGFloat = 0) {
        scrollView.contentView.scroll(to: NSPoint(
            x: min(max(0, horizontalOrigin), maximumHorizontalScrollOrigin(for: scrollView)),
            y: maximumScrollOrigin(for: scrollView)
        ))
        scrollView.reflectScrolledClipView(scrollView.contentView)
    }

    private func maximumScrollOrigin(for scrollView: NSScrollView) -> CGFloat {
        guard let documentView = scrollView.documentView else { return 0 }
        return max(0, documentView.bounds.height - scrollView.contentView.bounds.height)
    }

    private func maximumHorizontalScrollOrigin(for scrollView: NSScrollView) -> CGFloat {
        guard let documentView = scrollView.documentView else { return 0 }
        return max(0, documentView.bounds.width - scrollView.contentView.bounds.width)
    }

    private static let updateTimeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }()

    private static func readSnapshot(
        at path: String,
        tab: LogWindowTab,
        maximumLines: Int,
        maximumReadBytes: UInt64
    ) -> LogSnapshot {
        let url = URL(fileURLWithPath: path)
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: path),
              let size = attributes[.size] as? NSNumber
        else {
            return LogSnapshot(
                text: renderedRows([noticeRow(tab.emptyMessage, source: tab.title)]).joined(separator: "\n"),
                lineCount: 0,
                wasTrimmed: false,
                sourceExists: false
            )
        }

        let sourceModificationDate = attributes[.modificationDate] as? Date
        let fileSize = size.uint64Value
        let startOffset = fileSize > maximumReadBytes ? fileSize - maximumReadBytes : 0
        let data: Data
        do {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            try handle.seek(toOffset: startOffset)
            data = try handle.readToEnd() ?? Data()
        } catch {
            return LogSnapshot(
                text: renderedRows([
                    noticeRow("The local log file could not be read.", source: tab.title),
                ]).joined(separator: "\n"),
                lineCount: 0,
                wasTrimmed: false,
                sourceExists: true
            )
        }

        var usableData = data
        if startOffset > 0, let firstNewline = usableData.firstIndex(of: 0x0A) {
            usableData.removeSubrange(...firstNewline)
        }
        let sourceLines = String(decoding: usableData, as: UTF8.self)
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { String($0).trimmingCharacters(in: .newlines) }
            .filter { !$0.isEmpty }
        let relevantLines: [String]
        if tab == .routeTrace {
            relevantLines = sourceLines.filter { $0.contains("litellm_route_trace") }
        } else if tab == .service {
            // Route Trace owns the structured routing events.  Keeping them out
            // of Service avoids duplicated rows and leaves this tab focused on
            // the proxy's own startup, health, and HTTP output.
            relevantLines = sourceLines.filter { !$0.contains("litellm_route_trace") }
        } else {
            relevantLines = sourceLines
        }
        let wasTrimmed = startOffset > 0 || relevantLines.count > maximumLines
        let displayedLines = Array(relevantLines.suffix(maximumLines))
        guard !displayedLines.isEmpty else {
            return LogSnapshot(
                text: renderedRows([noticeRow(tab.emptyMessage, source: tab.title)]).joined(separator: "\n"),
                lineCount: 0,
                wasTrimmed: wasTrimmed,
                sourceExists: true
            )
        }
        return LogSnapshot(
            text: formattedLocalLogText(
                displayedLines,
                tab: tab,
                referenceDate: sourceModificationDate
            ),
            lineCount: displayedLines.count,
            wasTrimmed: wasTrimmed,
            sourceExists: true
        )
    }

    private static func formattedLocalLogText(
        _ lines: [String],
        tab: LogWindowTab,
        referenceDate: Date?
    ) -> String {
        switch tab {
        case .requests:
            return formattedRequestRows(lines, referenceDate: referenceDate)
        case .routeTrace:
            return formattedRouteTraceRows(lines, referenceDate: referenceDate)
        case .service, .menu, .configWatch:
            return formattedPlainLogRows(lines, tab: tab, referenceDate: referenceDate)
        case .recovery, .remoteUsage:
            return lines.joined(separator: "\n")
        }
    }

    private static func formattedRequestRows(_ lines: [String], referenceDate: Date?) -> String {
        var rows: [LogRow] = []
        var unreadable = 0
        for line in lines {
            guard let record = jsonDictionary(in: line) else {
                unreadable += 1
                continue
            }
            let route: String
            if let explicit = firstText(record, keys: ["route_key"]), !explicit.isEmpty {
                route = explicit
            } else {
                route = [
                    firstText(record, keys: ["provider"]),
                    firstText(record, keys: ["upstream_model"]),
                ].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " / ")
            }
            let usage = record["usage"] as? [String: Any] ?? [:]
            let tokens = firstNumber(usage, keys: ["total_tokens", "total", "input_tokens", "prompt_tokens"])
            let status = (firstText(record, keys: ["status"]) ?? "-").uppercased()
            let duration = firstNumber(record, keys: ["duration_ms"]).map { "\($0)ms" } ?? "-"
            let detailParts = [
                "duration=\(duration)",
                "route=\(route.isEmpty ? "-" : route)",
                "tokens=\(tokens ?? "-")",
                compactError(record["error"]),
            ].filter { !$0.isEmpty }
            rows.append(LogRow(
                timestamp: localTimestamp(
                    firstText(record, keys: ["ts"]) ?? "",
                    referenceDate: referenceDate
                ),
                source: firstText(record, keys: ["model_group"]) ?? "-",
                status: status,
                detail: detailParts.joined(separator: " · ")
            ))
        }

        guard !rows.isEmpty else {
            let message = unreadable > 0
                ? "No readable request records. \(unreadable) malformed entries were skipped."
                : LogWindowTab.requests.emptyMessage
            return renderedRows([noticeRow(message, source: LogWindowTab.requests.title)]).joined(separator: "\n")
        }

        if unreadable > 0 {
            rows.append(noticeRow("\(unreadable) malformed request entries were skipped.", source: LogWindowTab.requests.title))
        }
        return renderedRows(rows).joined(separator: "\n")
    }

    private static func formattedRouteTraceRows(_ lines: [String], referenceDate: Date?) -> String {
        var rows: [LogRow] = []
        for line in lines {
            guard let record = jsonDictionary(in: line) else { continue }
            let route = [
                firstText(record, keys: ["provider"]),
                firstText(record, keys: ["upstream_model"]),
            ].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " / ")
            let detail = traceDetail(record)
            rows.append(LogRow(
                timestamp: localTimestamp(
                    firstText(record, keys: ["timestamp", "ts", "time"]) ?? "",
                    referenceDate: referenceDate
                ),
                source: firstText(record, keys: ["model_group", "model", "requested_model"]) ?? "-",
                status: firstText(record, keys: ["event"]) ?? "event",
                detail: ["route=\(route.isEmpty ? "-" : route)", detail.isEmpty ? "" : detail]
                    .filter { !$0.isEmpty }
                    .joined(separator: " · ")
            ))
        }
        guard !rows.isEmpty else {
            return renderedRows([
                noticeRow(LogWindowTab.routeTrace.emptyMessage, source: LogWindowTab.routeTrace.title),
            ]).joined(separator: "\n")
        }
        return renderedRows(rows).joined(separator: "\n")
    }

    private static func formattedPlainLogRows(
        _ lines: [String],
        tab: LogWindowTab = .service,
        referenceDate: Date? = nil
    ) -> String {
        let rows = lines.compactMap { line -> LogRow? in
            if let record = jsonDictionary(in: line) {
                return structuredLogRow(record, tab: tab, referenceDate: referenceDate)
            }
            return plainLogRow(line, tab: tab, referenceDate: referenceDate)
        }
        return renderedRows(rows.isEmpty ? [noticeRow(tab.emptyMessage, source: tab.title)] : rows).joined(separator: "\n")
    }

    /// Terminal-coloured LiteLLM output is useful in a shell but should not
    /// show raw escape sequences in the native text view.  Keep ordinary
    /// bracketed text intact and remove only CSI/OSC control sequences.
    private static func strippingTerminalControlSequences(_ value: String) -> String {
        let csiPattern = "\u{001B}\\[[0-?]*[ -/]*[@-~]"
        let oscPattern = "\u{001B}\\][^\u{0007}\u{001B}]*(?:\u{0007}|\u{001B}\\\\)"
        let withoutOSC = replacingTerminalPattern(oscPattern, in: value)
        return replacingTerminalPattern(csiPattern, in: withoutOSC)
    }

    private static func replacingTerminalPattern(_ pattern: String, in value: String) -> String {
        guard let expression = try? NSRegularExpression(pattern: pattern) else { return value }
        return expression.stringByReplacingMatches(
            in: value,
            options: [],
            range: NSRange(value.startIndex..., in: value),
            withTemplate: ""
        )
    }

    private static func structuredLogRow(
        _ record: [String: Any],
        tab: LogWindowTab,
        referenceDate: Date?
    ) -> LogRow {
        let timestamp = localTimestamp(
            firstText(record, keys: ["timestamp", "ts", "time", "created_at", "createdAt"]) ?? "",
            referenceDate: referenceDate
        )
        let status = (firstText(record, keys: ["level", "severity", "status"]) ?? "event").uppercased()
        let source = firstText(record, keys: ["event", "action", "model_group", "model", "provider"]) ?? tab.title
        let detail = [
            firstText(record, keys: ["model_group", "model"]),
            firstText(record, keys: ["provider"]),
            firstText(record, keys: ["reason", "detail", "error"]),
        ].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        return LogRow(
            timestamp: timestamp,
            source: source,
            status: status,
            detail: detail.isEmpty ? "Structured log event" : detail
        )
    }

    private static func plainLogRow(_ line: String, tab: LogWindowTab, referenceDate: Date?) -> LogRow? {
        let clean = strippingTerminalControlSequences(line)
        let extracted = extractingLeadingTimestamp(from: clean)
        let detail = singleLine(extracted.detail)
        guard !detail.isEmpty else { return nil }
        let status = logLevel(in: detail)
        return LogRow(
            timestamp: localTimestamp(extracted.timestamp, referenceDate: referenceDate),
            source: tab.title,
            status: status,
            detail: detail
        )
    }

    private static func renderedRows(_ rows: [LogRow]) -> [String] {
        table(
            headers: [localTimeHeader(), "SOURCE", "STATUS", "DETAIL"],
            widths: [19, 16, 12, 52],
            rows: rows.map { [$0.timestamp, $0.source, $0.status, $0.detail] }
        )
    }

    private static func localTimeHeader() -> String {
        let seconds = TimeZone.autoupdatingCurrent.secondsFromGMT(for: Date())
        let sign = seconds >= 0 ? "+" : "-"
        let absoluteSeconds = abs(seconds)
        return String(format: "LOCAL TIME (%@%02d%02d)", sign, absoluteSeconds / 3_600, (absoluteSeconds / 60) % 60)
    }

    private static func noticeRow(_ message: String, source: String, observedAt: Date = Date()) -> LogRow {
        LogRow(
            timestamp: formattedLocalTimestamp(observedAt),
            source: source,
            status: "NOTICE",
            detail: message
        )
    }

    private static func logLevel(in detail: String) -> String {
        let lowercased = detail.lowercased()
        if lowercased.contains("error") || lowercased.contains("failed") || lowercased.contains("exception") {
            return "ERROR"
        }
        if lowercased.contains("warning") || lowercased.contains("warn") {
            return "WARN"
        }
        if lowercased.contains("success") || lowercased.contains("complete") || lowercased.contains("started") {
            return "INFO"
        }
        return "LOG"
    }

    private static func extractingLeadingTimestamp(from value: String) -> (timestamp: String, detail: String) {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return ("", "") }

        let patterns = [
            "^\\[([^\\]]+)\\]\\s*",
            "^((?:\\d{4}-\\d{2}-\\d{2}[ T])?\\d{2}:\\d{2}:\\d{2}(?:[.,]\\d+)?)\\s+",
        ]
        for pattern in patterns {
            guard let expression = try? NSRegularExpression(pattern: pattern),
                  let match = expression.firstMatch(
                      in: trimmed,
                      options: [],
                      range: NSRange(trimmed.startIndex..., in: trimmed)
                  ),
                  let timestampRange = Range(match.range(at: 1), in: trimmed),
                  let fullRange = Range(match.range(at: 0), in: trimmed)
            else {
                continue
            }
            return (String(trimmed[timestampRange]), String(trimmed[fullRange.upperBound...]))
        }
        return ("", trimmed)
    }

    /// Every parsed value is shown as local `yyyy-MM-dd HH:mm:ss`. A time-only
    /// or legacy timestamp-less source is anchored to its log file's modification
    /// date, or to the current observation time when the source has no file.
    private static func localTimestamp(_ value: String, referenceDate: Date? = nil) -> String {
        let compact = singleLine(value)
        let fallback = formattedLocalTimestamp(referenceDate ?? Date())
        guard !compact.isEmpty else { return fallback }

        let normalized = compact
            .replacingOccurrences(of: ",", with: ".")
            .replacingOccurrences(of: " UTC", with: " +0000")
        let hasOffset = normalized.hasSuffix("Z")
            || normalized.range(of: #"[+-]\d{2}:?\d{2}$"#, options: .regularExpression) != nil
            || normalized.range(of: #"\s[+-]\d{4}$"#, options: .regularExpression) != nil
        let absoluteDate: Date?
        if hasOffset {
            timestampFormatterLock.lock()
            defer { timestampFormatterLock.unlock() }
            let fractionalFormatter = ISO8601DateFormatter()
            fractionalFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            let wholeSecondsFormatter = ISO8601DateFormatter()
            wholeSecondsFormatter.formatOptions = [.withInternetDateTime]
            absoluteDate = fractionalFormatter.date(from: normalized)
                ?? wholeSecondsFormatter.date(from: normalized)
                ?? absoluteTimestampFormatters.lazy.compactMap { $0.date(from: normalized) }.first
        } else if let date = localDate(in: normalized) {
            return formattedLocalTimestamp(date)
        } else if let time = timeOnly(in: normalized), let referenceDate {
            return formattedLocalTimestamp(date(for: time, relativeTo: referenceDate))
        } else {
            return fallback
        }
        guard let absoluteDate else {
            return fallback
        }
        return formattedLocalTimestamp(absoluteDate)
    }

    private static func localDate(in value: String) -> Date? {
        let formats = [
            "yyyy-MM-dd HH:mm:ss.SSSSSS",
            "yyyy-MM-dd HH:mm:ss.SSS",
            "yyyy-MM-dd HH:mm:ss",
            "yyyy-MM-dd'T'HH:mm:ss.SSSSSS",
            "yyyy-MM-dd'T'HH:mm:ss.SSS",
            "yyyy-MM-dd'T'HH:mm:ss",
        ]
        timestampFormatterLock.lock()
        defer { timestampFormatterLock.unlock() }
        localTimestampFormatter.timeZone = TimeZone.autoupdatingCurrent
        localTimestampFormatter.isLenient = false
        for format in formats {
            localTimestampFormatter.dateFormat = format
            if let date = localTimestampFormatter.date(from: value) { return date }
        }
        localTimestampFormatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return nil
    }

    private static func timeOnly(in value: String) -> DateComponents? {
        guard let expression = try? NSRegularExpression(
            pattern: #"^(\d{2}):(\d{2}):(\d{2})(?:[.,]\d+)?$"#
        ),
        let match = expression.firstMatch(
            in: value,
            options: [],
            range: NSRange(value.startIndex..., in: value)
        ),
        let hourRange = Range(match.range(at: 1), in: value),
        let minuteRange = Range(match.range(at: 2), in: value),
        let secondRange = Range(match.range(at: 3), in: value),
        let hour = Int(value[hourRange]),
        let minute = Int(value[minuteRange]),
        let second = Int(value[secondRange]),
        (0..<24).contains(hour),
        (0..<60).contains(minute),
        (0..<60).contains(second)
        else {
            return nil
        }
        return DateComponents(hour: hour, minute: minute, second: second)
    }

    private static func date(for time: DateComponents, relativeTo referenceDate: Date) -> Date {
        var calendar = Calendar.autoupdatingCurrent
        calendar.timeZone = TimeZone.autoupdatingCurrent
        let candidate = calendar.date(
            bySettingHour: time.hour ?? 0,
            minute: time.minute ?? 0,
            second: time.second ?? 0,
            of: referenceDate
        ) ?? referenceDate
        // A log cannot contain a timestamp materially later than its own mtime.
        // Shift back one day for the common post-midnight rollover case.
        if candidate.timeIntervalSince(referenceDate) > 120 {
            return calendar.date(byAdding: .day, value: -1, to: candidate) ?? candidate
        }
        return candidate
    }

    private static func formattedLocalTimestamp(_ date: Date) -> String {
        timestampFormatterLock.lock()
        defer { timestampFormatterLock.unlock() }
        localTimestampFormatter.timeZone = TimeZone.autoupdatingCurrent
        localTimestampFormatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return localTimestampFormatter.string(from: date)
    }

    private static func readRecoverySnapshot(
        recoveryStatePath: String,
        cooldownStatePath: String
    ) -> LogSnapshot {
        let observedAt = Date()
        let state = boundedRecoveryJSON(at: recoveryStatePath)
        let cooldown = boundedRecoveryJSON(at: cooldownStatePath)
        let recoveryRows = state["recoveries"] as? [String: Any] ?? [:]
        let cooldownRows = cooldown["cooldowns"] as? [String: Any] ?? [:]
        var rows: [LogRow] = []

        let recoveries = recoveryRows.sorted(by: { $0.key > $1.key }).prefix(100).compactMap { _, raw -> LogRow? in
            guard let row = raw as? [String: Any] else { return nil }
            let diagnostic = row["diagnostic"] as? [String: Any] ?? [:]
            let detail = [
                boundedRecoveryText(diagnostic["title"]),
                boundedRecoveryText(diagnostic["detail"]),
            ].filter { !$0.isEmpty }.joined(separator: " — ")
            return LogRow(
                timestamp: localTimestamp(
                    boundedRecoveryText(
                        row["updated_at"],
                        fallback: boundedRecoveryText(row["heartbeat_at"], fallback: boundedRecoveryText(row["started_at"]))
                    ),
                    referenceDate: observedAt
                ),
                source: boundedRecoveryText(row["model_group"], fallback: "recovery"),
                status: boundedRecoveryText(row["status"], fallback: "RECOVERING").uppercased(),
                detail: [
                    boundedRecoveryText(row["activity"], fallback: "recovering"),
                    detail,
                ].filter { !$0.isEmpty }.joined(separator: " · ")
            )
        }
        rows.append(contentsOf: recoveries)

        let cooldowns = cooldownRows.sorted(by: { $0.key > $1.key }).prefix(100).compactMap { _, raw -> LogRow? in
            guard let row = raw as? [String: Any] else { return nil }
            let route = [
                boundedRecoveryText(row["provider"]),
                boundedRecoveryText(row["upstream_model"]),
            ].filter { !$0.isEmpty }.joined(separator: " / ")
            let failures = boundedRecoveryNumber(row["failures"])
            let remaining = boundedCooldownRemaining(row["cooldown_until"])
            return LogRow(
                timestamp: localTimestamp(epochTimestamp(row["last_failure_at"]), referenceDate: observedAt),
                source: boundedRecoveryText(row["model_group"], fallback: "cooldown"),
                status: "COOLDOWN",
                detail: [
                    route.isEmpty ? "" : "route=\(route)",
                    failures.isEmpty ? "" : "failures=\(failures)",
                    remaining.isEmpty ? "" : "available in \(remaining)",
                ].filter { !$0.isEmpty }.joined(separator: " · ")
            )
        }
        rows.append(contentsOf: cooldowns)

        let text = renderedRows(
            rows.isEmpty
                ? [noticeRow(LogWindowTab.recovery.emptyMessage, source: LogWindowTab.recovery.title, observedAt: observedAt)]
                : rows
        ).joined(separator: "\n")
        return LogSnapshot(
            text: text,
            lineCount: recoveries.count + cooldowns.count,
            wasTrimmed: recoveryRows.count + cooldownRows.count > 200,
            sourceExists: FileManager.default.fileExists(atPath: recoveryStatePath)
                || FileManager.default.fileExists(atPath: cooldownStatePath)
        )
    }

    private static func formattedRemoteUsageText(_ raw: String) -> String {
        let observedAt = Date()
        let lines = raw.components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        let nonempty = lines.filter { !$0.isEmpty }
        guard !nonempty.isEmpty else {
            return renderedRows([
                noticeRow(LogWindowTab.remoteUsage.emptyMessage, source: LogWindowTab.remoteUsage.title, observedAt: observedAt),
            ])
                .joined(separator: "\n")
        }
        if nonempty.count == 1,
           nonempty[0].hasPrefix("No ") || nonempty[0].hasPrefix("Configured ") || nonempty[0].hasPrefix("Online usage") {
            return renderedRows([
                noticeRow(singleLine(nonempty[0]), source: LogWindowTab.remoteUsage.title, observedAt: observedAt),
            ])
                .joined(separator: "\n")
        }

        var currentSource = "Online Usage"
        var rows: [LogRow] = []

        for line in lines {
            guard !line.isEmpty else { continue }
            if line.hasPrefix("Updated ") {
                continue
            }
            if let opening = line.lastIndex(of: "("), line.hasSuffix(")") {
                let provider = line[..<opening].trimmingCharacters(in: .whitespacesAndNewlines)
                let source = line[line.index(after: opening)..<line.index(before: line.endIndex)]
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                if !provider.isEmpty, !source.isEmpty {
                    currentSource = "\(singleLine(provider)) · \(singleLine(source.capitalized))"
                    continue
                }
            }
            let fields = line.components(separatedBy: "  ")
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
            let tokens = fields.first { $0.hasPrefix("tokens=") }
                .map { String($0.dropFirst("tokens=".count)) } ?? "-"
            let values = fields.filter { !$0.hasPrefix("tokens=") }
            let timestamp: String
            let model: String
            let status: String
            switch values.count {
            case 3...:
                timestamp = localTimestamp(values[0], referenceDate: observedAt)
                model = values[1]
                status = values[2]
            case 2 where looksLikeUsageTimestamp(values[0]):
                timestamp = localTimestamp(values[0], referenceDate: observedAt)
                model = values[1]
                status = "usage"
            case 2:
                timestamp = formattedLocalTimestamp(observedAt)
                model = values[0]
                status = values[1]
            case 1:
                timestamp = formattedLocalTimestamp(observedAt)
                model = values[0]
                status = "usage"
            default:
                timestamp = formattedLocalTimestamp(observedAt)
                model = "-"
                status = "usage"
            }
            rows.append(LogRow(
                timestamp: timestamp,
                source: currentSource,
                status: status.uppercased(),
                detail: "model=\(model) · tokens=\(tokens)"
            ))
        }
        if rows.isEmpty {
            return renderedRows([
                noticeRow("Online usage did not include displayable records.", source: LogWindowTab.remoteUsage.title, observedAt: observedAt),
            ])
                .joined(separator: "\n")
        }
        return renderedRows(rows).joined(separator: "\n")
    }

    private static func onlineUsageRecordCount(in text: String) -> Int {
        var inUsageTable = false
        var sawDivider = false
        var count = 0
        for line in text.components(separatedBy: .newlines) {
            if line.hasPrefix("LOCAL TIME") && line.contains("DETAIL") {
                inUsageTable = true
                sawDivider = false
                continue
            }
            guard inUsageTable else { continue }
            if !sawDivider, isTableDivider(line) {
                sawDivider = true
                continue
            }
            guard sawDivider else { continue }
            if line.isEmpty {
                inUsageTable = false
                continue
            }
            count += 1
        }
        return count
    }

    private static func table(headers: [String], widths: [Int], rows: [[String]]) -> [String] {
        let header = tableLine(headers, widths: widths)
        let divider = widths.map { String(repeating: "-", count: $0) }.joined(separator: "  ")
        return [header, divider] + rows.map { tableLine($0, widths: widths) }
    }

    private static func tableLine(_ values: [String], widths: [Int]) -> String {
        zip(values, widths).map { fixedColumn($0.0, width: $0.1) }.joined(separator: "  ")
    }

    private static func fixedColumn(_ value: String, width: Int) -> String {
        let text = singleLine(value)
        // The fixed width establishes the starting position of the next column,
        // not a maximum.  A wide value expands the text view and is reachable
        // through its horizontal scroller.
        return text + String(repeating: " ", count: max(0, width - text.count))
    }

    private static func jsonDictionary(in line: String) -> [String: Any]? {
        guard let opening = line.firstIndex(of: "{") else { return nil }
        let candidate = String(line[opening...])
        guard let data = candidate.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let dictionary = object as? [String: Any]
        else {
            return nil
        }
        return dictionary
    }

    private static func firstText(_ dictionary: [String: Any], keys: [String]) -> String? {
        for key in keys {
            if let text = compactValue(dictionary[key]), !text.isEmpty { return text }
        }
        return nil
    }

    private static func firstNumber(_ dictionary: [String: Any], keys: [String]) -> String? {
        for key in keys {
            if let number = compactNumber(dictionary[key]), !number.isEmpty { return number }
        }
        return nil
    }

    private static func compactValue(_ value: Any?, limit: Int = 180) -> String? {
        if let text = value as? String {
            let compact = singleLine(text)
            return compact.isEmpty ? nil : compact
        }
        if let number = compactNumber(value), !number.isEmpty { return number }
        return nil
    }

    private static func compactNumber(_ value: Any?) -> String? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID()
        else {
            return nil
        }
        let decimal = number.doubleValue
        guard decimal.isFinite else { return nil }
        return decimal.rounded() == decimal ? String(number.int64Value) : String(format: "%.4g", decimal)
    }

    /// The reader itself retains a bounded tail of each source file.  Do not
    /// shorten individual displayed values: a horizontal scroller is less
    /// surprising than silently hiding the tail of a route or error message.
    private static func singleLine(_ value: String) -> String {
        value
            .replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "\r", with: " ")
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")
    }

    private static func looksLikeUsageTimestamp(_ value: String) -> Bool {
        value.contains(":") && (value.contains("-") || value.contains("/"))
    }

    private static func isTableDivider(_ value: String) -> Bool {
        let allowed = CharacterSet(charactersIn: "- ")
        let scalars = value.unicodeScalars
        return !scalars.isEmpty && scalars.allSatisfy { allowed.contains($0) }
    }

    private static func compactError(_ value: Any?) -> String {
        if let text = compactValue(value, limit: 140), !text.isEmpty {
            return "Error: \(text)"
        }
        guard let error = value as? [String: Any] else { return "" }
        let parts = ["type", "status_code", "reason", "code"].compactMap { key -> String? in
            guard let value = compactValue(error[key], limit: 70), !value.isEmpty else { return nil }
            return "\(key)=\(value)"
        }
        return parts.isEmpty ? "Error recorded" : "Error: \(parts.joined(separator: " · "))"
    }

    private static func traceDetail(_ record: [String: Any]) -> String {
        let parts = ["status", "activity", "reason", "detail", "attempt", "recovery_kind", "error_type", "service_tier", "codex_fast_default_injected"].compactMap { key -> String? in
            guard let value = compactValue(record[key], limit: 90), !value.isEmpty else { return nil }
            return key == "detail" ? value : "\(key)=\(value)"
        }
        return parts.joined(separator: " · ")
    }

    private static func boundedRecoveryJSON(at path: String) -> [String: Any] {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              data.count <= 1_500_000,
              let value = try? JSONSerialization.jsonObject(with: data),
              let dictionary = value as? [String: Any]
        else {
            return [:]
        }
        return dictionary
    }

    private static func boundedRecoveryText(_ value: Any?, fallback: String = "") -> String {
        guard let value = value as? String else { return fallback }
        let compact = singleLine(value)
        return compact.isEmpty ? fallback : compact
    }

    private static func boundedRecoveryNumber(_ value: Any?) -> String {
        compactNumber(value) ?? ""
    }

    private static func epochTimestamp(_ value: Any?) -> String {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite,
              number.doubleValue > 0
        else {
            return ""
        }
        return ISO8601DateFormatter.string(
            from: Date(timeIntervalSince1970: number.doubleValue),
            timeZone: TimeZone(secondsFromGMT: 0)!,
            formatOptions: [.withInternetDateTime, .withFractionalSeconds]
        )
    }

    private static func boundedCooldownRemaining(_ value: Any?) -> String {
        guard let value = value as? NSNumber else { return "" }
        let seconds = max(0, value.doubleValue - Date().timeIntervalSince1970)
        if seconds < 60 { return "\(Int(seconds.rounded(.up)))s" }
        let minutes = Int(seconds / 60)
        return "\(minutes)m \(Int(seconds) % 60)s"
    }

    private static func readRemoteUsage(service: String) -> String {
        guard FileManager.default.isExecutableFile(atPath: service) else {
            return "Online usage reader is unavailable in this app bundle."
        }
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [service, "remote-usage-logs"]
        process.standardOutput = output
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            let text = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if process.terminationStatus == 0, !text.isEmpty {
                return text
            }
        } catch {
            return "Online usage reader could not start."
        }
        return "Online usage logs are unavailable right now."
    }
}
