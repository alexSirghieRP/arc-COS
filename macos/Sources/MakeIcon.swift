// Generates the app icon (1024×1024 PNG): dark rounded square, violet
// gradient, activity-pulse glyph — matching the board's brand mark.
// Run at build time: swift MakeIcon.swift <out.png>

import Cocoa

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"

let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()

// Rounded-square background (macOS icon grid: ~100px margin, ~185px radius)
let rect = NSRect(x: 100, y: 100, width: size - 200, height: size - 200)
let path = NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185)
let bg = NSGradient(colors: [
    NSColor(calibratedRed: 0.08, green: 0.07, blue: 0.16, alpha: 1),
    NSColor(calibratedRed: 0.03, green: 0.03, blue: 0.07, alpha: 1),
])!
bg.draw(in: path, angle: -90)

// Subtle violet glow ring
path.lineWidth = 6
NSColor(calibratedRed: 0.42, green: 0.36, blue: 0.91, alpha: 0.55).setStroke()
path.stroke()

// Activity pulse line
let pulse = NSBezierPath()
pulse.lineWidth = 46
pulse.lineCapStyle = .round
pulse.lineJoinStyle = .round
let midY = size / 2
pulse.move(to: NSPoint(x: 210, y: midY))
pulse.line(to: NSPoint(x: 380, y: midY))
pulse.line(to: NSPoint(x: 450, y: midY + 190))
pulse.line(to: NSPoint(x: 560, y: midY - 210))
pulse.line(to: NSPoint(x: 630, y: midY + 60))
pulse.line(to: NSPoint(x: 690, y: midY))
pulse.line(to: NSPoint(x: 814, y: midY))
NSColor(calibratedRed: 0.65, green: 0.58, blue: 1.0, alpha: 1).setStroke()
pulse.stroke()

// Bright core over the same path
let core = pulse.copy() as! NSBezierPath
core.lineWidth = 18
NSColor.white.withAlphaComponent(0.9).setStroke()
core.stroke()

img.unlockFocus()

guard let tiff = img.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else {
    fputs("icon render failed\n", stderr); exit(1)
}
try! png.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")
