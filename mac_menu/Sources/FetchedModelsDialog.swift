import Cocoa

struct ModelCandidate: Codable {
    var id: String
}

struct ModelListResponse: Codable {
    var data: [ModelCandidate]
}

final class FetchedModelListView: NSView {
    struct Row {
        var title: String
        var selected: Bool
    }

    var rows: [Row]
    private(set) var visibleRowIndexes: [Int]
    private(set) var searchQuery = ""
    var minimumDocumentHeight: CGFloat = 0
    var stateDidChange: (() -> Void)?
    let rowHeight: CGFloat
    let checkboxSize: CGFloat = 18
    let checkboxX: CGFloat = 14
    let textX: CGFloat = 44
    var tooltipTag: NSView.ToolTipTag?

    override class var isCompatibleWithResponsiveScrolling: Bool { true }
    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }

    init(models: [String], rowHeight: CGFloat, width: CGFloat) {
        self.rows = models.map { Row(title: $0, selected: false) }
        self.visibleRowIndexes = Array(models.indices)
        self.rowHeight = rowHeight
        let height = max(rowHeight, CGFloat(models.count) * rowHeight)
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height))
        autoresizingMask = [.width]
        preparedContentRect = bounds
        updateTooltipTracking()
    }

    required init?(coder: NSCoder) {
        rows = []
        visibleRowIndexes = []
        rowHeight = 28
        super.init(coder: coder)
        preparedContentRect = bounds
        updateTooltipTracking()
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        updateTooltipTracking()
    }

    func updateTooltipTracking() {
        if let tooltipTag {
            removeToolTip(tooltipTag)
        }
        tooltipTag = addToolTip(bounds, owner: self, userData: nil)
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.textBackgroundColor.setFill()
        dirtyRect.fill()
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingMiddle
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 14),
            .foregroundColor: NSColor.labelColor,
            .paragraphStyle: paragraph,
        ]

        guard !visibleRowIndexes.isEmpty else {
            drawEmptyState()
            return
        }

        let firstRow = max(0, Int(floor(dirtyRect.minY / rowHeight)))
        let lastRow = min(visibleRowIndexes.count - 1, Int(floor((dirtyRect.maxY - 0.01) / rowHeight)))
        guard firstRow <= lastRow else { return }

        for visibleRowIndex in firstRow...lastRow {
            let rowIndex = visibleRowIndexes[visibleRowIndex]
            let rowY = CGFloat(visibleRowIndex) * rowHeight
            let rowRect = NSRect(x: 0, y: rowY, width: bounds.width, height: rowHeight)
            if visibleRowIndex % 2 == 1 {
                NSColor.alternatingContentBackgroundColors[1].setFill()
                rowRect.fill()
            }

            let checkboxRect = NSRect(
                x: checkboxX,
                y: rowY + floor((rowHeight - checkboxSize) / 2),
                width: checkboxSize,
                height: checkboxSize
            )
            let checkboxPath = NSBezierPath(roundedRect: checkboxRect, xRadius: 5, yRadius: 5)
            (rows[rowIndex].selected ? NSColor.controlAccentColor : NSColor.systemGray.withAlphaComponent(0.22)).setFill()
            checkboxPath.fill()

            if rows[rowIndex].selected {
                NSColor.white.setStroke()
                let check = NSBezierPath()
                check.lineWidth = 2
                check.lineCapStyle = .round
                check.lineJoinStyle = .round
                check.move(to: NSPoint(x: checkboxRect.minX + 4.5, y: checkboxRect.midY))
                check.line(to: NSPoint(x: checkboxRect.minX + 8, y: checkboxRect.maxY - 5))
                check.line(to: NSPoint(x: checkboxRect.maxX - 4, y: checkboxRect.minY + 5))
                check.stroke()
            }

            let textRect = NSRect(
                x: textX,
                y: rowY + floor((rowHeight - 18) / 2) - 1,
                width: max(0, bounds.width - textX - 8),
                height: 22
            )
            rows[rowIndex].title.draw(in: textRect, withAttributes: attributes)
        }
    }

    func drawEmptyState() {
        let message = searchQuery.isEmpty ? "No models available" : "No matching models"
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 14),
            .foregroundColor: NSColor.secondaryLabelColor,
            .paragraphStyle: paragraph,
        ]
        let messageHeight: CGFloat = 20
        let messageRect = NSRect(
            x: 16,
            y: max(20, floor((bounds.height - messageHeight) / 2)),
            width: max(0, bounds.width - 32),
            height: messageHeight
        )
        message.draw(in: messageRect, withAttributes: attributes)
    }

    override func prepareContent(in rect: NSRect) {
        super.prepareContent(in: rect)
        preparedContentRect = bounds
        updateTooltipTracking()
    }

    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        let visibleRowIndex = Int(floor(point.y / rowHeight))
        guard visibleRowIndex >= 0, visibleRowIndex < visibleRowIndexes.count else { return }
        let rowIndex = visibleRowIndexes[visibleRowIndex]
        rows[rowIndex].selected.toggle()
        setNeedsDisplay(NSRect(x: 0, y: CGFloat(visibleRowIndex) * rowHeight, width: bounds.width, height: rowHeight))
        stateDidChange?()
    }

    func selectAll() {
        var changed = false
        for index in visibleRowIndexes where !rows[index].selected {
            rows[index].selected = true
            changed = true
        }
        guard changed else { return }
        needsDisplay = true
        stateDidChange?()
    }

    func setSearchQuery(_ query: String) {
        searchQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let terms = searchQuery
            .split(whereSeparator: { $0.isWhitespace })
            .map { normalizedSearchText(String($0)) }

        if terms.isEmpty {
            visibleRowIndexes = Array(rows.indices)
        } else {
            visibleRowIndexes = rows.indices.filter { index in
                let title = normalizedSearchText(rows[index].title)
                return terms.allSatisfy { title.contains($0) }
            }
        }

        updateDocumentHeight()
        needsDisplay = true
        stateDidChange?()
    }

    func normalizedSearchText(_ value: String) -> String {
        value.folding(
            options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive],
            locale: .current
        )
    }

    func setMinimumDocumentHeight(_ height: CGFloat) {
        minimumDocumentHeight = max(0, height)
        updateDocumentHeight()
    }

    func updateDocumentHeight() {
        let rowsHeight = CGFloat(max(1, visibleRowIndexes.count)) * rowHeight
        setFrameSize(NSSize(width: frame.width, height: max(minimumDocumentHeight, rowsHeight)))
    }

    func invertSelection() {
        guard !visibleRowIndexes.isEmpty else { return }
        for index in visibleRowIndexes {
            rows[index].selected.toggle()
        }
        needsDisplay = true
        stateDidChange?()
    }

    var selectedModels: [String] {
        rows.filter { $0.selected }.map { $0.title }
    }

    var totalCount: Int {
        rows.count
    }

    var visibleCount: Int {
        visibleRowIndexes.count
    }

    var selectedCount: Int {
        rows.reduce(into: 0) { count, row in
            if row.selected {
                count += 1
            }
        }
    }

    var hasActiveSearch: Bool {
        !searchQuery.isEmpty
    }

    func view(
        _ view: NSView,
        stringForToolTip tag: NSView.ToolTipTag,
        point: NSPoint,
        userData data: UnsafeMutableRawPointer?
    ) -> String {
        let visibleRowIndex = Int(floor(point.y / rowHeight))
        guard visibleRowIndex >= 0, visibleRowIndex < visibleRowIndexes.count else { return "" }
        return rows[visibleRowIndexes[visibleRowIndex]].title
    }
}

final class FetchedModelScrollView: NSScrollView {
    let preciseScrollMultiplier: CGFloat = 4

    override func scrollWheel(with event: NSEvent) {
        guard event.hasPreciseScrollingDeltas,
              let documentView else {
            super.scrollWheel(with: event)
            return
        }

        let clipView = contentView
        var origin = clipView.bounds.origin
        let maxY = max(0, documentView.frame.height - clipView.bounds.height)
        origin.y = min(maxY, max(0, origin.y - event.scrollingDeltaY * preciseScrollMultiplier))
        origin.x = 0
        clipView.scroll(to: origin)
        reflectScrolledClipView(clipView)
    }
}

final class FetchedModelChooserController: NSObject, NSWindowDelegate, NSSearchFieldDelegate {
    var didStopModal = false
    weak var modalWindow: NSWindow?
    weak var searchField: NSSearchField?
    weak var scrollView: NSScrollView?
    weak var resultCountLabel: NSTextField?
    weak var selectAllButton: NSButton?
    weak var invertSelectionButton: NSButton?
    weak var addButton: NSButton?
    let listView: FetchedModelListView

    init(models: [String], width: CGFloat) {
        listView = FetchedModelListView(models: models, rowHeight: 28, width: width)
        super.init()
        listView.stateDidChange = { [weak self] in
            self?.refreshControls()
        }
    }

    func configureControls(
        searchField: NSSearchField,
        scrollView: NSScrollView,
        resultCountLabel: NSTextField,
        selectAllButton: NSButton,
        invertSelectionButton: NSButton,
        addButton: NSButton,
        minimumListHeight: CGFloat
    ) {
        self.searchField = searchField
        self.scrollView = scrollView
        self.resultCountLabel = resultCountLabel
        self.selectAllButton = selectAllButton
        self.invertSelectionButton = invertSelectionButton
        self.addButton = addButton
        searchField.delegate = self
        listView.setMinimumDocumentHeight(minimumListHeight)
        refreshControls()
    }

    func selectAll() {
        listView.selectAll()
    }

    func invertSelection() {
        listView.invertSelection()
    }

    @objc func selectAllAction(_ sender: Any?) {
        selectAll()
    }

    @objc func invertSelectionAction(_ sender: Any?) {
        invertSelection()
    }

    func controlTextDidChange(_ obj: Notification) {
        guard let field = obj.object as? NSSearchField, field === searchField else { return }
        listView.setSearchQuery(field.stringValue)
        scrollToTop()
    }

    func scrollToTop() {
        guard let scrollView else { return }
        let clipView = scrollView.contentView
        clipView.scroll(to: NSPoint(x: 0, y: 0))
        scrollView.reflectScrolledClipView(clipView)
    }

    func refreshControls() {
        let visibleCount = listView.visibleCount
        let totalCount = listView.totalCount
        let selectedCount = listView.selectedCount

        var summary = listView.hasActiveSearch
            ? "\(visibleCount) of \(totalCount) models"
            : "\(totalCount) models"
        if selectedCount > 0 {
            summary += "  |  \(selectedCount) selected"
        }
        resultCountLabel?.stringValue = summary
        selectAllButton?.isEnabled = visibleCount > 0
        invertSelectionButton?.isEnabled = visibleCount > 0
        addButton?.isEnabled = selectedCount > 0
    }

    @objc func addSelectedAction(_ sender: Any?) {
        stopModal(with: .OK)
    }

    @objc func cancelAction(_ sender: Any?) {
        stopModal(with: .cancel)
    }

    func windowWillClose(_ notification: Notification) {
        stopModal(with: .cancel)
    }

    func stopModal(with response: NSApplication.ModalResponse) {
        guard !didStopModal else { return }
        didStopModal = true
        NSApp.stopModal(withCode: response)
        modalWindow?.orderOut(nil)
    }

    var selectedModels: [String] {
        listView.selectedModels
    }
}
