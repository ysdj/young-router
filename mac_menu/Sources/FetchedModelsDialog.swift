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
        self.rowHeight = rowHeight
        let height = max(rowHeight, CGFloat(models.count) * rowHeight)
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height))
        autoresizingMask = [.width]
        preparedContentRect = bounds
        updateTooltipTracking()
    }

    required init?(coder: NSCoder) {
        rows = []
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

        let firstRow = max(0, Int(floor(dirtyRect.minY / rowHeight)))
        let lastRow = min(rows.count - 1, Int(floor((dirtyRect.maxY - 0.01) / rowHeight)))
        guard firstRow <= lastRow else { return }

        for rowIndex in firstRow...lastRow {
            let rowY = CGFloat(rowIndex) * rowHeight
            let rowRect = NSRect(x: 0, y: rowY, width: bounds.width, height: rowHeight)
            if rowIndex % 2 == 1 {
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

    override func prepareContent(in rect: NSRect) {
        super.prepareContent(in: rect)
        preparedContentRect = bounds
        updateTooltipTracking()
    }

    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        let rowIndex = Int(floor(point.y / rowHeight))
        guard rowIndex >= 0, rowIndex < rows.count else { return }
        rows[rowIndex].selected.toggle()
        setNeedsDisplay(NSRect(x: 0, y: CGFloat(rowIndex) * rowHeight, width: bounds.width, height: rowHeight))
    }

    func selectAll() {
        for index in rows.indices {
            rows[index].selected = true
        }
        needsDisplay = true
    }

    func invertSelection() {
        for index in rows.indices {
            rows[index].selected.toggle()
        }
        needsDisplay = true
    }

    var selectedModels: [String] {
        rows.filter { $0.selected }.map { $0.title }
    }

    func view(
        _ view: NSView,
        stringForToolTip tag: NSView.ToolTipTag,
        point: NSPoint,
        userData data: UnsafeMutableRawPointer?
    ) -> String {
        let rowIndex = Int(floor(point.y / rowHeight))
        guard rowIndex >= 0, rowIndex < rows.count else { return "" }
        return rows[rowIndex].title
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

final class FetchedModelChooserController: NSObject, NSWindowDelegate {
    var didStopModal = false
    weak var modalWindow: NSWindow?
    let listView: FetchedModelListView

    init(models: [String], width: CGFloat) {
        listView = FetchedModelListView(models: models, rowHeight: 28, width: width)
        super.init()
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
