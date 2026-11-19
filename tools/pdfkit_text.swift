import Foundation
import PDFKit

// Usage: pdfkit_text.swift [--plain] file.pdf [file2.pdf ...]
//
// Default mode prints an annotated, human-readable per-page dump.
// --plain emits only the extracted page texts joined by form-feed
// characters (\f), the same page separator used by poppler's
// pdftotext, and writes per-page statistics to stderr.

let arguments = CommandLine.arguments
let plainMode = arguments.contains("--plain")
let files = arguments.dropFirst().filter { $0 != "--plain" }

guard !files.isEmpty else {
    fputs("usage: pdfkit_text.swift [--plain] file.pdf [file2.pdf ...]\n", stderr)
    exit(2)
}

for path in files {
    let url = URL(fileURLWithPath: path)
    guard let document = PDFDocument(url: url) else {
        fputs("ERROR: cannot open \(path)\n", stderr)
        continue
    }

    var pages: [String] = []
    for index in 0..<document.pageCount {
        let text = document.page(at: index)?.string ?? ""
        pages.append(text)
        if plainMode {
            let alnum = text.unicodeScalars.filter {
                CharacterSet.alphanumerics.contains($0)
            }.count
            let status = text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? "EMPTY"
                : "ok"
            fputs("page \(index + 1)/\(document.pageCount): "
                  + "chars=\(text.count) alnum=\(alnum) [\(status)]\n", stderr)
        }
    }

    if plainMode {
        for (index, text) in pages.enumerated() {
            let alnum = text.unicodeScalars.filter {
                CharacterSet.alphanumerics.contains($0)
            }.count
            let status = text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? "EMPTY"
                : "ok"
            fputs("page \(index + 1)/\(document.pageCount): "
                  + "chars=\(text.count) alnum=\(alnum) [\(status)]\n", stderr)
        }
        fputs("total_pages: \(document.pageCount)\n", stderr)
        // pdftotext ends every page, including the last one, with a form
        // feed. Reproduce that so blank trailing pages survive page counting.
        let formFeed = "\u{000C}"
        let payload = pages.joined(separator: formFeed) + formFeed
        FileHandle.standardOutput.write(Data(payload.utf8))
        continue
    }

    print("===== FILE: \(path)")
    print("===== PAGES: \(document.pageCount)")
    for (index, text) in pages.enumerated() {
        let alnum = text.unicodeScalars.filter {
            CharacterSet.alphanumerics.contains($0)
        }.count
        print("----- PAGE \(index + 1) (chars=\(text.count), alnum=\(alnum)) -----")
        print(text)
        if !text.hasSuffix("\n") {
            print()
        }
    }
}
