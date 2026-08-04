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
    context.setFillColor(CGColor(gray: 0.86, alpha: 1))
    context.fillPath()
    context.restoreGState()

    context.saveGState()
    context.addPath(iconPath)
    context.clip()
    let gradient = CGGradient(
        colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
        colors: [
            CGColor(red: 0.985, green: 0.985, blue: 0.985, alpha: 1),
            CGColor(red: 0.79, green: 0.79, blue: 0.79, alpha: 1),
        ] as CFArray,
        locations: [0, 1]
    )!
    context.drawLinearGradient(
        gradient,
        start: CGPoint(x: iconRect.midX, y: iconRect.maxY),
        end: CGPoint(x: iconRect.midX, y: iconRect.minY),
        options: []
    )
    context.restoreGState()

    context.addPath(iconPath)
    context.setStrokeColor(CGColor(gray: 0.58, alpha: 0.62))
    context.setLineWidth(7)
    context.strokePath()

    let highlightPath = CGPath(
        roundedRect: iconRect.insetBy(dx: 5, dy: 5),
        cornerWidth: 179,
        cornerHeight: 179,
        transform: nil
    )
    context.addPath(highlightPath)
    context.setStrokeColor(CGColor(gray: 1, alpha: 0.72))
    context.setLineWidth(4)
    context.strokePath()

    context.setFillColor(CGColor(gray: 0.015, alpha: 1))
    context.fill(CGRect(x: 324, y: 365, width: 29, height: 318))
    context.fill(CGRect(x: 324, y: 365, width: 190, height: 29))
    context.fill(CGRect(x: 563, y: 365, width: 25, height: 239))
    context.fill(CGRect(x: 563, y: 365, width: 138, height: 25))

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

let masterIcon = makeMasterIcon()
for size in iconSizes {
    let image = size == canvasSize ? masterIcon : resize(masterIcon, to: size)
    let output = outputDirectory.appendingPathComponent("icon_\(size).png")
    try pngData(for: image).write(to: output, options: .atomic)
}
