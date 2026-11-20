// pdf_probe.swift -- local, offline structural probe for mixed PDFs.
//
// For each page emits JSON with:
//   page            one-based page number
//   media_box       [x0, y0, x1, y1] in PDF points
//   text            full extractable text layer (PDFKit), may be empty
//   images          placed raster images: resource name, pixel size,
//                   bounding box [x0, y0, x1, y1] of the unit square
//                   transformed by the CTM active at the Do operator
//
// This mirrors the two signals beangulp.pdf.classify_pdf() combines:
// image coverage (union of image bboxes over the page box) and text
// characters. It does not render or OCR anything: an amount printed
// only inside a full-page image is invisible here, by construction.
//
// Build:  swiftc -module-cache-path <dir> pdf_probe.swift -o pdf_probe
// Run:    pdf_probe document.pdf > probe.json

import Foundation
import CoreGraphics
import PDFKit

final class ScanState {
    var ctmStack: [CGAffineTransform] = [.identity]
    var xobjects: [String: CGPDFDictionaryRef] = [:]
    var images: [[String: Any]] = []
}

func currentState(_ info: UnsafeMutableRawPointer?) -> ScanState {
    return Unmanaged<ScanState>.fromOpaque(info!).takeUnretainedValue()
}

let opSave: CGPDFOperatorCallback = { _, info in
    let state = currentState(info)
    state.ctmStack.append(state.ctmStack.last!)
}

let opRestore: CGPDFOperatorCallback = { _, info in
    let state = currentState(info)
    if state.ctmStack.count > 1 {
        state.ctmStack.removeLast()
    }
}

let opConcat: CGPDFOperatorCallback = { scanner, info in
    let state = currentState(info)
    var f = CGFloat(0), e = CGFloat(0), d = CGFloat(0)
    var c = CGFloat(0), b = CGFloat(0), a = CGFloat(0)
    CGPDFScannerPopNumber(scanner, &f)
    CGPDFScannerPopNumber(scanner, &e)
    CGPDFScannerPopNumber(scanner, &d)
    CGPDFScannerPopNumber(scanner, &c)
    CGPDFScannerPopNumber(scanner, &b)
    CGPDFScannerPopNumber(scanner, &a)
    let matrix = CGAffineTransform(a: a, b: b, c: c, d: d, tx: e, ty: f)
    let top = state.ctmStack.removeLast()
    state.ctmStack.append(top.concatenating(matrix))
}

let opDrawObject: CGPDFOperatorCallback = { scanner, info in
    let state = currentState(info)
    var rawName: UnsafePointer<CChar>?
    guard CGPDFScannerPopName(scanner, &rawName), let rawName else { return }
    let name = String(cString: rawName)
    guard let imageDict = state.xobjects[name] else { return }
    var subtype: UnsafePointer<CChar>?
    guard CGPDFDictionaryGetName(imageDict, "Subtype", &subtype),
          let subtype, String(cString: subtype) == "Image" else { return }
    var width = CGPDFInteger(0)
    var height = CGPDFInteger(0)
    CGPDFDictionaryGetInteger(imageDict, "Width", &width)
    CGPDFDictionaryGetInteger(imageDict, "Height", &height)
    let ctm = state.ctmStack.last!
    let corners = [
        CGPoint(x: 0, y: 0), CGPoint(x: 1, y: 0),
        CGPoint(x: 0, y: 1), CGPoint(x: 1, y: 1),
    ].map { $0.applying(ctm) }
    let xs = corners.map(\.x), ys = corners.map(\.y)
    state.images.append([
        "name": name,
        "width_px": Int(width),
        "height_px": Int(height),
        "bbox": [xs.min()!, ys.min()!, xs.max()!, ys.max()!],
    ])
}

func collectXObjects(_ page: CGPDFPage) -> [String: CGPDFDictionaryRef] {
    var result: [String: CGPDFDictionaryRef] = [:]
    guard let pageDict = page.dictionary else { return result }
    var resources: CGPDFDictionaryRef?
    guard CGPDFDictionaryGetDictionary(pageDict, "Resources", &resources),
          let resources else { return result }
    var xobjectDict: CGPDFDictionaryRef?
    guard CGPDFDictionaryGetDictionary(resources, "XObject", &xobjectDict),
          let xobjectDict else { return result }

    // Image XObjects are streams (object type 9); the image metadata
    // lives in the dictionary attached to the stream.
    let applier: CGPDFDictionaryApplierFunction = { key, object, target in
        var stream: CGPDFStreamRef?
        guard CGPDFObjectGetType(object) == .stream,
              CGPDFObjectGetValue(object, .stream, &stream), let stream,
              let dict = CGPDFStreamGetDictionary(stream) else { return }
        let map = Unmanaged<NSMutableDictionary>.fromOpaque(target!).takeUnretainedValue()
        map[String(cString: key)] = dict
    }
    let bridge = NSMutableDictionary()
    CGPDFDictionaryApplyFunction(
        xobjectDict, applier,
        Unmanaged.passUnretained(bridge).toOpaque())
    for (key, value) in bridge {
        result[key as! String] = (value as! CGPDFDictionaryRef)
    }
    return result
}

let arguments = CommandLine.arguments
guard arguments.count > 1 else {
    FileHandle.standardError.write(Data("usage: pdf_probe document.pdf\n".utf8))
    exit(2)
}
let url = URL(fileURLWithPath: arguments[1])
guard let cgDocument = CGPDFDocument(url as CFURL),
      let pdfKitDocument = PDFDocument(url: url) else {
    FileHandle.standardError.write(Data("cannot open \(arguments[1])\n".utf8))
    exit(1)
}

let table = CGPDFOperatorTableCreate()!
CGPDFOperatorTableSetCallback(table, "q", opSave)
CGPDFOperatorTableSetCallback(table, "Q", opRestore)
CGPDFOperatorTableSetCallback(table, "cm", opConcat)
CGPDFOperatorTableSetCallback(table, "Do", opDrawObject)

var pagesOut: [[String: Any]] = []
for index in 1...cgDocument.numberOfPages {
    guard let page = cgDocument.page(at: index) else { continue }
    let mediaBox = page.getBoxRect(.mediaBox)
    let state = ScanState()
    state.xobjects = collectXObjects(page)
    let contentStream = CGPDFContentStreamCreateWithPage(page)
    let scanner = CGPDFScannerCreate(contentStream, table,
                                    Unmanaged.passUnretained(state).toOpaque())
    CGPDFScannerScan(scanner)
    CGPDFScannerRelease(scanner)
    CGPDFContentStreamRelease(contentStream)
    let text = pdfKitDocument.page(at: index - 1)?.string ?? ""
    pagesOut.append([
        "page": index,
        "media_box": [
            mediaBox.origin.x, mediaBox.origin.y,
            mediaBox.maxX, mediaBox.maxY,
        ],
        "text": text,
        "images": state.images,
    ])
}

let output: [String: Any] = [
    "file": arguments[1],
    "page_count": cgDocument.numberOfPages,
    "pages": pagesOut,
]
let data = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
