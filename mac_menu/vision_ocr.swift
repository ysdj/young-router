import Foundation
import Vision
import ImageIO

enum OutputFormat: String {
    case compact
    case detailed
}

struct OCRLine {
    let text: String
    let minX: Double
    let midX: Double
    let maxX: Double
    let minY: Double
    let midY: Double
    let maxY: Double
    let width: Double
    let height: Double
}

struct RectangleBox {
    let minX: Double
    let midX: Double
    let maxX: Double
    let minY: Double
    let midY: Double
    let maxY: Double
    let width: Double
    let height: Double
}

struct LayoutSnapshot {
    let imageWidth: Int
    let imageHeight: Int
    let archetype: String
    let regions: [String]
    let contentFlow: String
    let textConcentration: [String]
    let title: String?
    let navigationItems: [String]
    let fieldLabels: [String]
    let actionLabels: [String]
    let tableHeaders: [String]
    let buttonLabels: [String]
    let inputLabels: [String]
    let salientTexts: [String]
    let topPreview: [String]
    let textLineCount: Int
    let largePanelCount: Int
    let cardLikePanelCount: Int
    let rectangleCount: Int
    let looksLikeTable: Bool
    let looksLikeForm: Bool
    let looksLikeDocument: Bool
    let looksLikeModal: Bool
    let looksLikeSidebar: Bool
    let looksLikeRightRail: Bool
    let looksLikeHeader: Bool
    let looksLikeFooter: Bool
    let twoColumn: Bool
    let probableInputCount: Int
    let probableButtonCount: Int
}

func loadImage(_ path: String) throws -> CGImage {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
        throw NSError(domain: "VisionOCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "Could not open image source."])
    }
    guard let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "VisionOCR", code: 2, userInfo: [NSLocalizedDescriptionKey: "Could not decode image."])
    }
    return image
}

func recognizeText(from image: CGImage) throws -> [OCRLine] {
    var recognized: [OCRLine] = []
    let request = VNRecognizeTextRequest { request, error in
        if let error {
            fputs("Vision OCR failed: \(error.localizedDescription)\n", stderr)
            return
        }
        guard let observations = request.results as? [VNRecognizedTextObservation] else {
            return
        }
        for observation in observations {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let box = observation.boundingBox
            recognized.append(
                OCRLine(
                    text: text,
                    minX: Double(box.minX),
                    midX: Double(box.midX),
                    maxX: Double(box.maxX),
                    minY: Double(box.minY),
                    midY: Double(box.midY),
                    maxY: Double(box.maxY),
                    width: Double(box.width),
                    height: Double(box.height)
                )
            )
        }
    }
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    if #available(macOS 13.0, *) {
        request.automaticallyDetectsLanguage = true
    }

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])
    return recognized.sorted {
        if abs($0.midY - $1.midY) > 0.02 {
            return $0.midY > $1.midY
        }
        return $0.minX < $1.minX
    }
}

func detectRectangles(in image: CGImage) throws -> [RectangleBox] {
    var boxes: [RectangleBox] = []
    let request = VNDetectRectanglesRequest { request, error in
        if let error {
            fputs("Rectangle detection failed: \(error.localizedDescription)\n", stderr)
            return
        }
        guard let observations = request.results as? [VNRectangleObservation] else {
            return
        }
        for observation in observations.prefix(16) {
            let box = observation.boundingBox
            boxes.append(
                RectangleBox(
                    minX: Double(box.minX),
                    midX: Double(box.midX),
                    maxX: Double(box.maxX),
                    minY: Double(box.minY),
                    midY: Double(box.midY),
                    maxY: Double(box.maxY),
                    width: Double(box.width),
                    height: Double(box.height)
                )
            )
        }
    }
    request.maximumObservations = 16
    request.minimumAspectRatio = 0.15
    request.maximumAspectRatio = 1.0
    request.minimumConfidence = 0.4
    request.minimumSize = 0.04

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])
    return boxes.sorted { ($0.width * $0.height) > ($1.width * $1.height) }
}

func uniquePreservingOrder(_ values: [String]) -> [String] {
    var seen = Set<String>()
    var result: [String] = []
    for value in values {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { continue }
        if seen.insert(trimmed).inserted {
            result.append(trimmed)
        }
    }
    return result
}

func classifyHorizontalRegion(minX: Double, maxX: Double) -> String {
    if maxX <= 0.22 { return "left edge" }
    if minX >= 0.78 { return "right edge" }
    if minX >= 0.22 && maxX <= 0.78 { return "center" }
    if minX < 0.25 && maxX < 0.6 { return "left side" }
    if minX > 0.4 && maxX > 0.75 { return "right side" }
    return "mid-width"
}

func classifyVerticalRegion(minY: Double, maxY: Double) -> String {
    if minY >= 0.72 { return "top" }
    if maxY <= 0.28 { return "bottom" }
    if minY >= 0.42 && maxY <= 0.72 { return "middle" }
    return "upper-middle"
}

func dominantRegions(from lines: [OCRLine]) -> [String] {
    var counts: [String: Int] = [:]
    for line in lines {
        let key = "\(classifyVerticalRegion(minY: line.minY, maxY: line.maxY)) \(classifyHorizontalRegion(minX: line.minX, maxX: line.maxX))"
        counts[key, default: 0] += 1
    }
    return counts.sorted { lhs, rhs in
        if lhs.value != rhs.value { return lhs.value > rhs.value }
        return lhs.key < rhs.key
    }.prefix(4).map(\.key)
}

func detectColumns(from lines: [OCRLine]) -> Int {
    let longLines = lines.filter { $0.width > 0.12 }
    guard longLines.count >= 4 else { return 1 }
    let left = longLines.filter { $0.midX < 0.42 }.count
    let right = longLines.filter { $0.midX > 0.58 }.count
    if left >= 2 && right >= 2 { return 2 }
    return 1
}

func detectSidebar(from lines: [OCRLine]) -> Bool {
    let leftStack = lines.filter { $0.minX < 0.18 && $0.width < 0.24 }
    return leftStack.count >= 4
}

func detectRightRail(from lines: [OCRLine]) -> Bool {
    let rightStack = lines.filter { $0.maxX > 0.82 && $0.width < 0.24 }
    return rightStack.count >= 4
}

func detectHeaderBand(from lines: [OCRLine]) -> Bool {
    let topLines = lines.filter { $0.midY > 0.83 }
    return topLines.count >= 2
}

func detectFooterBand(from lines: [OCRLine]) -> Bool {
    let bottomLines = lines.filter { $0.midY < 0.14 }
    return bottomLines.count >= 2
}

func detectFormLayout(from lines: [OCRLine]) -> Bool {
    let candidates = lines.filter {
        let text = $0.text.lowercased()
        return text.contains(":") || text.contains("email") || text.contains("password") || text.contains("username") || text.contains("phone") || text.contains("search") || text.contains("name") || text.contains("address")
    }
    return candidates.count >= 2
}

func detectTableLayout(from lines: [OCRLine]) -> Bool {
    let shortLines = lines.filter { $0.text.count <= 24 }
    let top = shortLines.filter { $0.midY > 0.62 }
    let middle = shortLines.filter { $0.midY > 0.34 && $0.midY <= 0.62 }
    let bottom = shortLines.filter { $0.midY <= 0.34 }
    return top.count >= 3 && middle.count >= 3 && bottom.count >= 2
}

func detectCardLikePanels(from rectangles: [RectangleBox]) -> Int {
    rectangles.filter { $0.width > 0.18 && $0.height > 0.12 }.count
}

func detectLargePanels(from rectangles: [RectangleBox]) -> Int {
    rectangles.filter { $0.width > 0.28 && $0.height > 0.18 }.count
}

func detectDialogLayout(from rectangles: [RectangleBox], lines: [OCRLine]) -> Bool {
    let centralLarge = rectangles.first {
        $0.width > 0.28 && $0.width < 0.8 && $0.height > 0.18 && $0.height < 0.8 &&
        abs($0.midX - 0.5) < 0.18 && abs($0.midY - 0.5) < 0.18
    }
    guard centralLarge != nil else { return false }
    let centeredText = lines.filter { abs($0.midX - 0.5) < 0.2 && abs($0.midY - 0.5) < 0.25 }
    return centeredText.count >= 3
}

func detectDenseDocument(from lines: [OCRLine]) -> Bool {
    guard lines.count >= 12 else { return false }
    let wideLines = lines.filter { $0.width > 0.32 }
    let distinctBands = Set(lines.map { Int($0.midY * 12.0) }).count
    return wideLines.count >= 6 && distinctBands >= 6
}

func detectNumericDensity(from lines: [OCRLine]) -> Bool {
    let numeric = lines.filter { $0.text.rangeOfCharacter(from: .decimalDigits) != nil }
    return numeric.count >= 4
}

func probableTitle(from lines: [OCRLine]) -> String? {
    let candidates = lines.filter { $0.midY > 0.68 }
    guard !candidates.isEmpty else { return nil }
    let ranked = candidates.sorted {
        if abs($0.height - $1.height) > 0.01 { return $0.height > $1.height }
        if $0.text.count != $1.text.count { return $0.text.count > $1.text.count }
        return $0.midY > $1.midY
    }
    let text = ranked[0].text.trimmingCharacters(in: .whitespacesAndNewlines)
    return text.isEmpty ? nil : text
}

func navigationItems(from lines: [OCRLine]) -> [String] {
    let items = lines.filter { $0.minX < 0.2 && $0.width < 0.25 && $0.text.count <= 28 }
    return uniquePreservingOrder(items.prefix(8).map(\.text))
}

func fieldLabels(from lines: [OCRLine]) -> [String] {
    let labels = lines.filter {
        let text = $0.text.lowercased()
        let hasKeyword = text.contains("email") || text.contains("password") || text.contains("username") || text.contains("search") || text.contains("phone") || text.contains("name") || text.contains("address") || text.contains("title") || text.contains("date") || text.contains("filter")
        let looksLikeLabel = text.contains(":") && $0.text.count <= 32
        return hasKeyword || looksLikeLabel
    }
    return uniquePreservingOrder(labels.prefix(8).map(\.text))
}

func actionLabels(from lines: [OCRLine]) -> [String] {
    let labels = lines.filter {
        let text = $0.text.lowercased()
        return text.contains("sign in") || text.contains("log in") || text.contains("login") || text.contains("submit") || text.contains("save") || text.contains("cancel") || text.contains("continue") || text.contains("next") || text.contains("back") || text.contains("search") || text.contains("send") || text.contains("create") || text.contains("add") || text.contains("delete") || text.contains("run") || text.contains("apply") || text.contains("confirm")
    }
    return uniquePreservingOrder(labels.prefix(8).map(\.text))
}

func tableHeaders(from lines: [OCRLine]) -> [String] {
    let topBand = lines.filter { $0.midY > 0.6 && $0.text.count <= 24 }
    let sorted = topBand.sorted {
        if abs($0.midY - $1.midY) > 0.02 { return $0.midY > $1.midY }
        return $0.minX < $1.minX
    }
    return uniquePreservingOrder(sorted.prefix(6).map(\.text))
}

func buttonLabels(from lines: [OCRLine]) -> [String] {
    let labels = lines.filter {
        let t = $0.text.lowercased()
        return (t.contains("sign in") || t.contains("submit") || t.contains("save") || t.contains("cancel") || t.contains("continue") || t.contains("next") || t.contains("back") || t.contains("apply") || t.contains("confirm") || t.contains("search")) && $0.width < 0.28
    }
    return uniquePreservingOrder(labels.prefix(6).map(\.text))
}

func inputLabels(from lines: [OCRLine]) -> [String] {
    let labels = lines.filter {
        let t = $0.text.lowercased()
        return t.contains("email") || t.contains("password") || t.contains("username") || t.contains("search") || t.contains("phone") || t.contains("name") || t.contains("address") || t.contains("date")
    }
    return uniquePreservingOrder(labels.prefix(6).map(\.text))
}

func longestTexts(_ lines: [OCRLine], limit: Int) -> [String] {
    uniquePreservingOrder(
        lines.sorted {
            if $0.text.count != $1.text.count { return $0.text.count > $1.text.count }
            return $0.midY > $1.midY
        }
        .prefix(limit)
        .map(\.text)
    )
}

func topTextPreview(_ lines: [OCRLine], limit: Int) -> [String] {
    uniquePreservingOrder(lines.prefix(limit).map(\.text))
}

func probableInputCount(lines: [OCRLine], rectangles: [RectangleBox]) -> Int {
    let labels = inputLabels(from: lines).count
    let longRectangles = rectangles.filter { $0.width > 0.18 && $0.width < 0.72 && $0.height > 0.03 && $0.height < 0.16 }
    return max(labels, min(longRectangles.count, 8))
}

func probableButtonCount(lines: [OCRLine], rectangles: [RectangleBox]) -> Int {
    let labels = buttonLabels(from: lines).count
    let buttonRects = rectangles.filter { $0.width > 0.08 && $0.width < 0.32 && $0.height > 0.03 && $0.height < 0.14 }
    return max(labels, min(buttonRects.count, 8))
}

func inferArchetype(
    sidebar: Bool,
    rightRail: Bool,
    headerBand: Bool,
    footerBand: Bool,
    dialog: Bool,
    formLayout: Bool,
    tableLayout: Bool,
    denseDocument: Bool,
    numericDense: Bool,
    columns: Int,
    cardCount: Int,
    actionLabels: [String]
) -> String {
    if dialog { return "dialog or modal surface" }
    if sidebar && formLayout { return "navigation + form page" }
    if sidebar && (tableLayout || numericDense || cardCount >= 2) { return "navigation + dashboard/list page" }
    if denseDocument && columns >= 2 { return "multi-column document page" }
    if denseDocument { return "text-centric document/article page" }
    if tableLayout { return "table/list page" }
    if formLayout { return "form/settings page" }
    if cardCount >= 3 { return "card/dashboard page" }
    if headerBand && rightRail { return "workspace page with utility rail" }
    if !actionLabels.isEmpty && footerBand { return "wizard or confirmation page" }
    if columns >= 2 { return "two-column content page" }
    return "single-content interface page"
}

func inferRegions(
    sidebar: Bool,
    rightRail: Bool,
    headerBand: Bool,
    footerBand: Bool,
    dialog: Bool,
    columns: Int,
    cardCount: Int,
    largePanelCount: Int
) -> [String] {
    var regions: [String] = []
    if dialog { regions.append("centered main panel") }
    if headerBand { regions.append("top header band") }
    if sidebar { regions.append("left navigation rail") }
    if rightRail { regions.append("right-side utility rail") }
    if footerBand { regions.append("bottom footer/action band") }
    if columns >= 2 { regions.append("two reading columns") }
    if largePanelCount > 0 { regions.append("\(largePanelCount) large content panel(s)") }
    if cardCount >= 2 { regions.append("\(cardCount) card-like panel(s)") }
    if regions.isEmpty { regions.append("single central content region") }
    return regions
}

func inferContentFlow(columns: Int, denseDocument: Bool, tableLayout: Bool, formLayout: Bool, lines: [OCRLine]) -> String {
    let distinctBands = Set(lines.map { Int($0.midY * 12.0) }).count
    if tableLayout { return "scan by rows, then compare short entries across columns" }
    if formLayout { return "top-to-bottom field flow with short label/value steps" }
    if denseDocument && columns >= 2 { return "top-to-bottom reading within two text columns" }
    if columns >= 2 { return "top band first, then split attention across two columns" }
    if distinctBands >= 6 { return "top-to-bottom reading through several stacked sections" }
    return "top-to-bottom reading through a single main section"
}

func buildLayoutSnapshot(image: CGImage, lines: [OCRLine], rectangles: [RectangleBox]) -> LayoutSnapshot {
    let columns = detectColumns(from: lines)
    let sidebar = detectSidebar(from: lines)
    let rightRail = detectRightRail(from: lines)
    let headerBand = detectHeaderBand(from: lines)
    let footerBand = detectFooterBand(from: lines)
    let formLayout = detectFormLayout(from: lines)
    let tableLayout = detectTableLayout(from: lines)
    let denseDocument = detectDenseDocument(from: lines)
    let numericDense = detectNumericDensity(from: lines)
    let dialog = detectDialogLayout(from: rectangles, lines: lines)
    let cardCount = detectCardLikePanels(from: rectangles)
    let largePanelCount = detectLargePanels(from: rectangles)
    let title = probableTitle(from: lines)
    let navItems = navigationItems(from: lines)
    let fields = fieldLabels(from: lines)
    let actions = actionLabels(from: lines)
    let headers = tableHeaders(from: lines)
    let buttons = buttonLabels(from: lines)
    let inputs = inputLabels(from: lines)
    return LayoutSnapshot(
        imageWidth: image.width,
        imageHeight: image.height,
        archetype: inferArchetype(
            sidebar: sidebar,
            rightRail: rightRail,
            headerBand: headerBand,
            footerBand: footerBand,
            dialog: dialog,
            formLayout: formLayout,
            tableLayout: tableLayout,
            denseDocument: denseDocument,
            numericDense: numericDense,
            columns: columns,
            cardCount: cardCount,
            actionLabels: actions
        ),
        regions: inferRegions(
            sidebar: sidebar,
            rightRail: rightRail,
            headerBand: headerBand,
            footerBand: footerBand,
            dialog: dialog,
            columns: columns,
            cardCount: cardCount,
            largePanelCount: largePanelCount
        ),
        contentFlow: inferContentFlow(columns: columns, denseDocument: denseDocument, tableLayout: tableLayout, formLayout: formLayout, lines: lines),
        textConcentration: dominantRegions(from: lines),
        title: title,
        navigationItems: navItems,
        fieldLabels: fields,
        actionLabels: actions,
        tableHeaders: headers,
        buttonLabels: buttons,
        inputLabels: inputs,
        salientTexts: longestTexts(lines, limit: 6),
        topPreview: topTextPreview(lines, limit: 8),
        textLineCount: lines.count,
        largePanelCount: largePanelCount,
        cardLikePanelCount: cardCount,
        rectangleCount: rectangles.count,
        looksLikeTable: tableLayout,
        looksLikeForm: formLayout,
        looksLikeDocument: denseDocument,
        looksLikeModal: dialog,
        looksLikeSidebar: sidebar,
        looksLikeRightRail: rightRail,
        looksLikeHeader: headerBand,
        looksLikeFooter: footerBand,
        twoColumn: columns >= 2,
        probableInputCount: probableInputCount(lines: lines, rectangles: rectangles),
        probableButtonCount: probableButtonCount(lines: lines, rectangles: rectangles)
    )
}

func compactSummary(snapshot: LayoutSnapshot) -> [String] {
    var parts: [String] = []
    parts.append("type=\(snapshot.archetype)")
    if !snapshot.regions.isEmpty { parts.append("regions=\(snapshot.regions.joined(separator: ", "))") }
    if let title = snapshot.title { parts.append("title=\(title)") }

    var elements: [String] = []
    if snapshot.looksLikeModal { elements.append("modal") }
    if snapshot.looksLikeSidebar { elements.append("sidebar") }
    if snapshot.looksLikeHeader { elements.append("header") }
    if snapshot.looksLikeFooter { elements.append("footer") }
    if snapshot.twoColumn { elements.append("two-column") }
    if snapshot.looksLikeTable { elements.append("table") }
    if snapshot.looksLikeForm { elements.append("form") }
    if snapshot.probableInputCount > 0 { elements.append("inputs≈\(snapshot.probableInputCount)") }
    if snapshot.probableButtonCount > 0 { elements.append("buttons≈\(snapshot.probableButtonCount)") }
    if !elements.isEmpty { parts.append("elements=\(elements.joined(separator: ", "))") }

    if !snapshot.navigationItems.isEmpty {
        parts.append("nav=\(snapshot.navigationItems.prefix(4).joined(separator: " | "))")
    }
    if !snapshot.inputLabels.isEmpty {
        parts.append("inputs=\(snapshot.inputLabels.prefix(4).joined(separator: " | "))")
    }
    if !snapshot.buttonLabels.isEmpty {
        parts.append("buttons=\(snapshot.buttonLabels.prefix(4).joined(separator: " | "))")
    }
    if !snapshot.tableHeaders.isEmpty {
        parts.append("headers=\(snapshot.tableHeaders.prefix(4).joined(separator: " | "))")
    }
    if !snapshot.textConcentration.isEmpty {
        parts.append("text-zones=\(snapshot.textConcentration.prefix(3).joined(separator: ", "))")
    }
    if !snapshot.topPreview.isEmpty {
        parts.append("preview=\(snapshot.topPreview.prefix(6).joined(separator: " | "))")
    }
    return parts
}

func detailedSummary(snapshot: LayoutSnapshot) -> [String] {
    var summary: [String] = []
    summary.append("Image size: \(snapshot.imageWidth)x\(snapshot.imageHeight).")
    summary.append("Probable page type: \(snapshot.archetype).")
    summary.append("Region structure: \(snapshot.regions.joined(separator: ", ")).")
    summary.append("Content flow: \(snapshot.contentFlow).")

    var detectedElements: [String] = []
    detectedElements.append("\(snapshot.textLineCount) OCR text line(s)")
    detectedElements.append("\(snapshot.rectangleCount) rectangular region candidate(s)")
    if snapshot.largePanelCount > 0 { detectedElements.append("\(snapshot.largePanelCount) large panel(s)") }
    if snapshot.cardLikePanelCount > 0 { detectedElements.append("\(snapshot.cardLikePanelCount) card-like region(s)") }
    if snapshot.looksLikeModal { detectedElements.append("modal-style central surface") }
    if snapshot.looksLikeSidebar { detectedElements.append("left navigation rail") }
    if snapshot.looksLikeRightRail { detectedElements.append("right utility rail") }
    if snapshot.looksLikeHeader { detectedElements.append("top header") }
    if snapshot.looksLikeFooter { detectedElements.append("bottom footer/actions") }
    if snapshot.twoColumn { detectedElements.append("two text columns") }
    if snapshot.looksLikeForm { detectedElements.append("form-style labels") }
    if snapshot.looksLikeTable { detectedElements.append("table/list structure") }
    if snapshot.looksLikeDocument { detectedElements.append("dense document-style text") }
    if snapshot.probableInputCount > 0 { detectedElements.append("about \(snapshot.probableInputCount) input-like region(s)") }
    if snapshot.probableButtonCount > 0 { detectedElements.append("about \(snapshot.probableButtonCount) button-like region(s)") }
    summary.append("Detected elements: \(detectedElements.joined(separator: ", ")).")

    if let title = snapshot.title {
        summary.append("Likely primary heading: \(title).")
    }
    if !snapshot.navigationItems.isEmpty {
        summary.append("Likely navigation/section labels: \(snapshot.navigationItems.joined(separator: " | ")).")
    }
    if !snapshot.fieldLabels.isEmpty {
        summary.append("Likely field/filter labels: \(snapshot.fieldLabels.joined(separator: " | ")).")
    }
    if !snapshot.inputLabels.isEmpty {
        summary.append("Likely input labels: \(snapshot.inputLabels.joined(separator: " | ")).")
    }
    if !snapshot.buttonLabels.isEmpty {
        summary.append("Likely button labels: \(snapshot.buttonLabels.joined(separator: " | ")).")
    }
    if !snapshot.actionLabels.isEmpty {
        summary.append("Likely actions/status labels: \(snapshot.actionLabels.joined(separator: " | ")).")
    }
    if !snapshot.tableHeaders.isEmpty {
        summary.append("Likely table/list headers: \(snapshot.tableHeaders.joined(separator: " | ")).")
    }
    if !snapshot.textConcentration.isEmpty {
        summary.append("Text is concentrated in: \(snapshot.textConcentration.joined(separator: ", ")).")
    }
    if !snapshot.topPreview.isEmpty {
        summary.append("Top-to-bottom visible text preview: \(snapshot.topPreview.joined(separator: " | ")).")
    }
    if !snapshot.salientTexts.isEmpty {
        summary.append("Most salient text strings: \(snapshot.salientTexts.joined(separator: " | ")).")
    }
    if snapshot.textLineCount == 0 && snapshot.cardLikePanelCount > 0 {
        summary.append("Rectangular UI regions were detected, but OCR did not return readable text.")
    }
    return summary
}

func summarizeLayout(snapshot: LayoutSnapshot, format: OutputFormat) -> [String] {
    guard snapshot.textLineCount > 0 || snapshot.rectangleCount > 0 else {
        return ["No readable text or strong rectangular UI regions were detected locally."]
    }
    switch format {
    case .compact:
        return compactSummary(snapshot: snapshot)
    case .detailed:
        return detailedSummary(snapshot: snapshot)
    }
}

func renderOutput(image: CGImage, lines: [OCRLine], rectangles: [RectangleBox], format: OutputFormat) -> String {
    let snapshot = buildLayoutSnapshot(image: image, lines: lines, rectangles: rectangles)
    let layout = summarizeLayout(snapshot: snapshot, format: format)
    if lines.isEmpty {
        return (["Layout summary:"] + layout).joined(separator: "\n")
    }
    let visibleText = lines.map(\.text).joined(separator: "\n")
    return (["Layout summary:"] + layout + ["", "Visible text:", visibleText]).joined(separator: "\n")
}

func parseArguments(_ arguments: [String]) -> (OutputFormat, String)? {
    if arguments.count < 2 { return nil }
    var format: OutputFormat = .compact
    var index = 1
    while index < arguments.count - 1 {
        let arg = arguments[index]
        if arg == "--format" {
            guard index + 1 < arguments.count - 0 else { return nil }
            guard let parsed = OutputFormat(rawValue: arguments[index + 1]) else { return nil }
            format = parsed
            index += 2
            continue
        }
        break
    }
    guard index < arguments.count else { return nil }
    return (format, arguments[index])
}

let arguments = CommandLine.arguments
guard let (format, imagePath) = parseArguments(arguments) else {
    fputs("usage: vision_ocr [--format compact|detailed] <image-path>\n", stderr)
    exit(64)
}

do {
    let image = try loadImage(imagePath)
    let lines = try recognizeText(from: image)
    let rectangles = try detectRectangles(in: image)
    print(renderOutput(image: image, lines: lines, rectangles: rectangles, format: format))
} catch {
    fputs("\(error.localizedDescription)\n", stderr)
    exit(1)
}
