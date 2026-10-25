import Foundation
import PDFKit
// Table rows of a PDF: PDFKit's own text runs (selectionsByLine), runs on one baseline joined left to right.
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let doc = PDFDocument(url: url) else { FileHandle.standardError.write("cannot open\n".data(using: .utf8)!); exit(1) }
var out = ""
for p in 0..<doc.pageCount {
    guard let page = doc.page(at: p), let all = page.selection(for: page.bounds(for: .mediaBox)) else { continue }
    var runs: [(y: CGFloat, x: CGFloat, s: String)] = []
    for line in all.selectionsByLine() {
        let b = line.bounds(for: page)
        let s = (line.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !s.isEmpty { runs.append((b.midY, b.minX, s)) }
    }
    runs.sort { $0.y > $1.y }
    var rows: [[(y: CGFloat, x: CGFloat, s: String)]] = []
    for r in runs {
        if var last = rows.last, abs(last[0].y - r.y) < 3.0 { last.append(r); rows[rows.count - 1] = last }
        else { rows.append([r]) }
    }
    for row in rows { out += row.sorted { $0.x < $1.x }.map { $0.s }.joined(separator: " ") + "\n" }
    out += "\u{0C}\n"
}
print(out)
