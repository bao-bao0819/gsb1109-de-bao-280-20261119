// render_pdf.swift -- render PDF pages to PNGs for visual evidence.
//
// Offline and dependency-free. Used to show, as pixels, the amounts that
// the text layer cannot provide on scanned pages.
//
// Build:  swiftc -module-cache-path <dir> render_pdf.swift -o render_pdf
// Run:    render_pdf document.pdf output_dir [scale]

import Foundation
import CoreGraphics
import ImageIO

let arguments = CommandLine.arguments
guard arguments.count > 2 else {
    FileHandle.standardError.write(
        Data("usage: render_pdf document.pdf output_dir [scale]\n".utf8))
    exit(2)
}
let url = URL(fileURLWithPath: arguments[1])
let outputDir = URL(fileURLWithPath: arguments[2], isDirectory: true)
let scale = arguments.count > 3 ? CGFloat(Double(arguments[3]) ?? 2.0) : 2.0

guard let document = CGPDFDocument(url as CFURL) else {
    FileHandle.standardError.write(Data("cannot open \(arguments[1])\n".utf8))
    exit(1)
}
try? FileManager.default.createDirectory(at: outputDir,
                                         withIntermediateDirectories: true)

for index in 1...document.numberOfPages {
    guard let page = document.page(at: index) else { continue }
    let box = page.getBoxRect(.mediaBox)
    let width = Int(box.width * scale)
    let height = Int(box.height * scale)
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    guard let context = CGContext(
        data: nil, width: width, height: height, bitsPerComponent: 8,
        bytesPerRow: 0, space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        fatalError("cannot create bitmap context")
    }
    context.setFillColor(red: 1, green: 1, blue: 1, alpha: 1)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    context.scaleBy(x: scale, y: scale)
    context.drawPDFPage(page)
    guard let image = context.makeImage() else { fatalError("cannot render page") }
    let destination = outputDir.appendingPathComponent("page\(index).png")
    guard let sink = CGImageDestinationCreateWithURL(
        destination as CFURL, "public.png" as CFString, 1, nil) else {
        fatalError("cannot create PNG destination")
    }
    CGImageDestinationAddImage(sink, image, nil)
    CGImageDestinationFinalize(sink)
    print("wrote \(destination.path)")
}
