import AppKit
import CoreGraphics
import Foundation

// Generate a two-page statement PDF for diagnostics:
//   page 1: native text with an unparseable date format and a transaction
//           row whose amount column is blank (field-rule nulls);
//   page 2: a page with no text layer at all (conversion null).
//
// Usage: make_sample_pdf.swift output.pdf

let outPath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "sample_statement.pdf"
let url = URL(fileURLWithPath: outPath)

var mediaBox = CGRect(x: 0, y: 0, width: 612, height: 792)
guard let context = CGContext(url as CFURL, mediaBox: &mediaBox, nil) else {
    fatalError("cannot create PDF context at \(outPath)")
}
NSGraphicsContext.current = NSGraphicsContext(cgContext: context, flipped: false)

let font = NSFont(name: "Courier", size: 11) ?? NSFont.systemFont(ofSize: 11)
let attributes: [NSAttributedString.Key: Any] = [.font: font]

func drawLines(_ lines: [String]) {
    var y: CGFloat = 760
    for line in lines {
        NSAttributedString(string: line, attributes: attributes)
            .draw(at: NSPoint(x: 40, y: y))
        y -= 18
    }
}

context.beginPDFPage(nil)
drawLines([
    "ACME Bank",
    "Diagnostic sample statement",
    "Date: 11/20/2026",
    "Statement: November Transactions",
    "Balance: $2,109.93",
    "",
    "11/18/2026 Coffee -$4.50",
    "11/19/2026 Lunch",
    "11/99/2026 Bad date row $9.00",
    "11/20/2026 Payroll $3,200.00",
])
context.endPDFPage()

// Second page deliberately contains no text, simulating content the PDF
// converter cannot extract (image-only or rasterized page).
context.beginPDFPage(nil)
context.endPDFPage()
context.closePDF()

print("wrote \(outPath)")
