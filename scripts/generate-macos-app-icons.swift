import AppKit
import CoreGraphics
import Foundation

private let canvasSize = 1024
private let iconSizes = [16, 32, 64, 128, 256, 512, 1024]
private let outputDirectory = URL(
    fileURLWithPath: "rn/apps/macos/macos/LiteLLMMenu-macOS/Assets.xcassets/AppIcon.appiconset",
    isDirectory: true
)

private func bitmapContext(width: Int, height: Int) -> CGContext {
    CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpace(name: CGColorSpace.sRGB)!,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    )!
}

private func makeMasterIcon() -> CGImage {
    let context = bitmapContext(width: canvasSize, height: canvasSize)
    let iconRect = CGRect(x: 108, y: 108, width: 808, height: 808)
    let iconPath = CGPath(
        roundedRect: iconRect,
        cornerWidth: 184,
        cornerHeight: 184,
        transform: nil
    )

    context.saveGState()
    context.setShadow(
        offset: CGSize(width: 0, height: -22),
        blur: 42,
        color: CGColor(gray: 0.12, alpha: 0.32)
    )
    context.addPath(iconPath)
    context.setFillColor(CGColor(gray: 0.89, alpha: 1))
    context.fillPath()
    context.restoreGState()

    context.addPath(iconPath)
    context.setFillColor(CGColor(gray: 0.89, alpha: 1))
    context.fillPath()

    context.addPath(iconPath)
    context.setStrokeColor(CGColor(gray: 0.58, alpha: 0.62))
    context.setLineWidth(7)
    context.strokePath()

    let leftGlyph = CGPath(rect: CGRect(x: 322, y: 358, width: 28, height: 310), transform: nil)
        .mutableCopy()!
    leftGlyph.addRect(CGRect(x: 322, y: 358, width: 186, height: 28))

    let rightGlyph = CGPath(rect: CGRect(x: 557, y: 358, width: 25, height: 236), transform: nil)
        .mutableCopy()!
    rightGlyph.addRect(CGRect(x: 557, y: 358, width: 137, height: 25))

    let glyphs = CGMutablePath()
    glyphs.addPath(leftGlyph)
    glyphs.addPath(rightGlyph)

    context.saveGState()
    context.setShadow(
        offset: CGSize(width: 4, height: -4),
        blur: 7,
        color: CGColor(gray: 0, alpha: 0.28)
    )
    context.addPath(glyphs)
    context.setFillColor(CGColor(gray: 0.08, alpha: 1))
    context.fillPath()
    context.restoreGState()

    return context.makeImage()!
}

private func resize(_ image: CGImage, to size: Int) -> CGImage {
    let context = bitmapContext(width: size, height: size)
    context.interpolationQuality = .high
    context.draw(image, in: CGRect(x: 0, y: 0, width: size, height: size))
    return context.makeImage()!
}

private func pngData(for image: CGImage) -> Data {
    NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])!
}

try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
let masterIcon = makeMasterIcon()
for size in iconSizes {
    let image = size == canvasSize ? masterIcon : resize(masterIcon, to: size)
    let output = outputDirectory.appendingPathComponent("icon_\(size).png")
    try pngData(for: image).write(to: output, options: .atomic)
}
