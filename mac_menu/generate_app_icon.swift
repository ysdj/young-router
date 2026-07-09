import AppKit
import Foundation

struct IconSpec {
    let name: String
    let points: Int
    let scale: Int
}

let specs = [
    IconSpec(name: "icon_16x16.png", points: 16, scale: 1),
    IconSpec(name: "icon_16x16@2x.png", points: 16, scale: 2),
    IconSpec(name: "icon_32x32.png", points: 32, scale: 1),
    IconSpec(name: "icon_32x32@2x.png", points: 32, scale: 2),
    IconSpec(name: "icon_128x128.png", points: 128, scale: 1),
    IconSpec(name: "icon_128x128@2x.png", points: 128, scale: 2),
    IconSpec(name: "icon_256x256.png", points: 256, scale: 1),
    IconSpec(name: "icon_256x256@2x.png", points: 256, scale: 2),
    IconSpec(name: "icon_512x512.png", points: 512, scale: 1),
    IconSpec(name: "icon_512x512@2x.png", points: 512, scale: 2),
]

let outputPath = CommandLine.arguments.dropFirst().first ?? "LiteLLMMenu.icns"
let outputURL = URL(fileURLWithPath: outputPath)
let fileManager = FileManager.default
let tempRoot = fileManager.temporaryDirectory
    .appendingPathComponent("LiteLLMMenuIcon-\(UUID().uuidString)", isDirectory: true)
let iconsetURL = tempRoot.appendingPathComponent("LiteLLMMenu.iconset", isDirectory: true)

try fileManager.createDirectory(at: iconsetURL, withIntermediateDirectories: true)
defer {
    try? fileManager.removeItem(at: tempRoot)
}

for spec in specs {
    let pixels = spec.points * spec.scale
    let url = iconsetURL.appendingPathComponent(spec.name)
    try renderIcon(pixels: pixels, to: url)
}

try? fileManager.removeItem(at: outputURL)
let process = Process()
process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
process.arguments = [
    "-c",
    "icns",
    iconsetURL.path,
    "-o",
    outputURL.path,
]
try process.run()
process.waitUntilExit()

if process.terminationStatus != 0 {
    throw NSError(
        domain: "LiteLLMMenuIcon",
        code: Int(process.terminationStatus),
        userInfo: [NSLocalizedDescriptionKey: "iconutil failed with exit \(process.terminationStatus)"]
    )
}

func renderIcon(pixels: Int, to url: URL) throws {
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: pixels,
        pixelsHigh: pixels,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bitmapFormat: .alphaFirst,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        throw NSError(
            domain: "LiteLLMMenuIcon",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: "Could not create bitmap representation"]
        )
    }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    NSGraphicsContext.current?.shouldAntialias = true
    NSColor.clear.setFill()
    NSRect(x: 0, y: 0, width: pixels, height: pixels).fill()
    drawMenuIconMark(in: NSRect(x: 0, y: 0, width: pixels, height: pixels))
    NSGraphicsContext.restoreGraphicsState()

    guard let png = rep.representation(using: .png, properties: [:]) else {
        throw NSError(
            domain: "LiteLLMMenuIcon",
            code: 2,
            userInfo: [NSLocalizedDescriptionKey: "Could not encode PNG"]
        )
    }
    try png.write(to: url)
}

func drawMenuIconMark(in bounds: NSRect) {
    let unit = min(bounds.width * 0.78 / 22.0, bounds.height * 0.70 / 18.0)
    let markWidth = 22.0 * unit
    let markHeight = 18.0 * unit
    let origin = NSPoint(
        x: bounds.midX - markWidth / 2.0,
        y: bounds.midY - markHeight / 2.0
    )
    let attributes: [NSAttributedString.Key: Any] = [
        .foregroundColor: NSColor.black,
    ]

    ("L" as NSString).draw(
        at: NSPoint(x: origin.x + 2.5 * unit, y: origin.y - 1.0 * unit),
        withAttributes: attributes.merging([
            .font: NSFont.systemFont(ofSize: 18.0 * unit, weight: .regular),
        ]) { _, new in new }
    )
    ("L" as NSString).draw(
        at: NSPoint(x: origin.x + 13.0 * unit, y: origin.y + 2.0 * unit),
        withAttributes: attributes.merging([
            .font: NSFont.systemFont(ofSize: 13.0 * unit, weight: .regular),
        ]) { _, new in new }
    )
}
