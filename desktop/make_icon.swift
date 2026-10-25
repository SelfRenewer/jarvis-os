// Builds the app icon (1024×1024 PNG) from the logo: the emblem on a navy rounded square.
// usage: swift make_icon.swift <logo.png> <out.png>
import AppKit

let args = CommandLine.arguments
guard args.count == 3, let logo = NSImage(contentsOfFile: args[1]) else {
    FileHandle.standardError.write("usage: make_icon.swift <logo.png> <out.png>\n".data(using: .utf8)!)
    exit(2)
}
let side: CGFloat = 1024
let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: Int(side), pixelsHigh: Int(side), bitsPerSample: 8, samplesPerPixel: 4,
    hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let inset: CGFloat = 100  // macOS icon grid: the shape sits inside a 824 pt square
let shape = NSRect(x: inset, y: inset, width: side - 2 * inset, height: side - 2 * inset)
let rounded = NSBezierPath(roundedRect: shape, xRadius: 185, yRadius: 185)
NSGradient(colors: [NSColor(red: 0.04, green: 0.09, blue: 0.19, alpha: 1), NSColor(red: 0.01, green: 0.03, blue: 0.08, alpha: 1)])!
    .draw(in: rounded, angle: -90)
rounded.addClip()
// The emblem with a margin, above the «JARVIS» lettering: x 262…992, y 107…837 of the 1254 px logo (top-left).
let px = logo.representations.first.map { CGFloat($0.pixelsWide) } ?? logo.size.width
let scale = logo.size.width / px
let crop = NSRect(x: 262 * scale, y: (1254 - 837) * scale, width: 730 * scale, height: 730 * scale)
logo.draw(in: shape, from: crop, operation: .sourceOver, fraction: 1)
NSColor(white: 1, alpha: 0.10).setStroke()
rounded.lineWidth = 4
rounded.stroke()
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: args[2]))
