// halo-extract: text from a PDF (PDFKit) and from images and scanned pages (Vision OCR), on this Mac only, for StrixFold UI's
// chat. It reads the one file it is given, writes JSON to stdout, opens no network connection and runs nothing from
// the file. Built once from this source by mac/filetext.py (swiftc, Xcode's toolchain).
//   halo-extract pdf FILE MAX_PAGES MAX_OCR_PAGES   {"pages", "read", "ocr", "locked", "text": [page text...]}
//   halo-extract image FILE                         {"width", "height", "text"}
//   halo-extract see FILE [MAX_PIXELS] [QUALITY]    a photo for a vision model: upright (EXIF), at most MAX_PIXELS
//                                                   (1100000), a fresh JPEG with no metadata: {"width", "height", "jpeg"
//                                                   (base64)}
//   FILE "-" (pdf, image, see): the bytes come on stdin and stay in memory, and only JSON leaves (a Private chat's file,
//   mac/private_reader.py: nothing of it is written anywhere)
//   halo-extract make-pdf OUT TEXT [scan]           tests only: a one-page PDF of TEXT (scan: the text as a picture)
//   halo-extract make-png OUT TEXT                  tests only: TEXT drawn into a PNG
//   halo-extract make-photo OUT TEXT                tests only: TEXT in a JPEG turned on its side (EXIF orientation 6)
//                                                   with a location, a camera and a comment in its metadata
//   halo-extract doc -                              the chat's document tools (see "doc" below; mac/docs.py)
//   halo-extract make-form OUT                      tests only: a fillable two-page form
import AppKit
import Foundation
import PDFKit
import Vision

func out(_ obj: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func fail(_ msg: String) -> Never {
    out(["error": msg])
    exit(1)
}

// Recognised text, top to bottom, one observation per line
func ocr(_ image: CGImage) -> String {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    if #available(macOS 13.0, *) { req.automaticallyDetectsLanguage = true }
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do { try handler.perform([req]) } catch { return "" }
    let obs = (req.results ?? []).sorted { a, b in
        abs(a.boundingBox.midY - b.boundingBox.midY) > 0.01 ? a.boundingBox.midY > b.boundingBox.midY : a.boundingBox.minX < b.boundingBox.minX
    }
    return obs.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}

func render(_ page: PDFPage, maxSide: CGFloat = 2400) -> CGImage? {
    let box = page.bounds(for: .mediaBox)
    guard box.width > 0, box.height > 0 else { return nil }
    let scale = min(3.0, maxSide / max(box.width, box.height))
    let w = Int(box.width * scale), h = Int(box.height * scale)
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    return ctx.makeImage()
}

func textImage(_ text: String, width: Int = 1400, height: Int = 500) -> CGImage {
    let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
    NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 48), .foregroundColor: NSColor.black]
    (text as NSString).draw(in: CGRect(x: 40, y: 40, width: width - 80, height: height - 80), withAttributes: attrs)
    NSGraphicsContext.current = nil
    return ctx.makeImage()!
}

// ---------------------------------------------------------------- doc: the chat's document tools (mac/docs.py)
// One request on stdin: a JSON line ({"op", its arguments, "sizes": each file's length}), then the files' bytes one
// after another. One JSON line on stdout; a file it makes comes back inside it as base64 ("pdf", "jpeg"). Nothing is
// written anywhere and nothing is fetched (the HTML it lays out is docs.py's own, its pictures data: URIs).
//   info     {"pages", "locked", "sizes" (points, as shown), "fields", "placeholder" (an XFA form's "Please wait...")}
//   page     page (1-based), side (px), part (whole | top | bottom), marks (each field's number drawn beside it):
//            {"jpeg", "width", "height", "page", "pages", "text"}
//   text     pages ([1-based]): {"pages", "text": [each page's text; a scanned page's by OCR]}
//   fields   {"pages", "fields": [{"n", "name", "type", "value", "options", "page", "rect" (x, y from the top left, w,
//            h in points), "label", "readonly", "maxlen"}]}
//   fill     values ({field number or name: value}), flatten: {"pdf", "pages", "results": [{"key", "n", "name", "ok",
//            "why", "value"}]}
//   write    items ([{"page", "text", "x" and "y" | "after" | "below" (text on the page), "nth", "size"}]), flatten:
//            {"pdf", "pages", "placed": [{"page", "x", "y", "w", "h"} | {"error"}]}
//   combine  parts ([{"file", "pages" (1-based), "rotate"}]): {"pdf", "pages"}
//   html     html, paper ([w, h] points), margin, footer (page numbers), title, fit (at most so many pages: the type
//            shrinks to 75% at most): {"pdf", "pages"}; laid out by TextKit page by page in memory; the text PAGE_BREAK
//            starts a new page
let PAGE_BREAK = "HALOPAGEBREAK7f3a"

func docRequest() -> ([String: Any], [Data]) {
    let all = FileHandle.standardInput.readDataToEndOfFile()
    guard let nl = all.firstIndex(of: 0x0A),
          let req = try? JSONSerialization.jsonObject(with: all.subdata(in: all.startIndex..<nl)) as? [String: Any] else { fail("bad request") }
    var files: [Data] = [], at = nl + 1
    for n in (req["sizes"] as? [Int]) ?? [] {
        guard n >= 0, at + n <= all.endIndex else { fail("bad request") }
        files.append(all.subdata(in: at..<(at + n)))
        at += n
    }
    return (req, files)
}

func openPDF(_ d: Data?) -> PDFDocument {
    guard let d = d, let doc = PDFDocument(data: d) else { fail("unreadable") }
    if doc.isLocked && !doc.unlock(withPassword: "") { fail("locked") }
    return doc
}

func r1(_ v: CGFloat) -> Double { (Double(v) * 10).rounded() / 10 }

func isBool(_ v: Any) -> Bool { CFGetTypeID(v as CFTypeRef) == CFBooleanGetTypeID() }

// a JSON value as a field's text: 3 (not 3.0), true as Yes
func asText(_ v: Any) -> String {
    if isBool(v) { return (v as! Bool) ? "Yes" : "No" }
    if let n = v as? NSNumber { let d = n.doubleValue; return d == d.rounded() && abs(d) < 1e15 ? String(Int64(d)) : n.stringValue }
    if let s = v as? String { return s }
    return ""
}

func truthy(_ v: Any) -> Bool? {
    if isBool(v) { return v as? Bool }
    if let n = v as? NSNumber { return n.doubleValue != 0 }
    let s = asText(v).trimmingCharacters(in: .whitespaces).lowercased()
    if ["yes", "y", "true", "x", "on", "checked", "check", "1", "✓", "✔"].contains(s) { return true }
    if ["no", "n", "false", "off", "unchecked", "0", "", "none"].contains(s) { return false }
    return nil
}

// the size a page is shown at (its rotation applied), and the part of it a picture shows
func shownSize(_ p: PDFPage) -> CGSize {
    let b = p.bounds(for: .cropBox), rot = ((p.rotation % 360) + 360) % 360
    return rot % 180 == 0 ? b.size : CGSize(width: b.height, height: b.width)
}

struct Field {
    var n: Int
    var name: String
    var type: String
    var widgets: [(page: Int, a: PDFAnnotation)]
}

func fieldType(_ a: PDFAnnotation) -> String {
    let t = a.widgetFieldType
    if t == .text { return "text" }
    if t == .choice { return "choice" }
    if t == .signature { return "signature" }
    if t == .button {
        if a.widgetControlType == .radioButtonControl { return "radio" }
        if a.widgetControlType == .checkBoxControl { return "checkbox" }
        return "button"
    }
    return "other"
}

// every form field in page order (a field shown in several places, or a radio group, is one field with its widgets)
func fieldsOf(_ doc: PDFDocument) -> [Field] {
    var order: [String] = [], by: [String: Field] = [:], unnamed = 0
    for i in 0..<doc.pageCount {
        guard let p = doc.page(at: i) else { continue }
        for a in p.annotations where a.type == "Widget" {
            var name = a.fieldName ?? ""
            if name.isEmpty { unnamed += 1; name = "(unnamed \(unnamed))" }
            if by[name] == nil { order.append(name); by[name] = Field(n: order.count, name: name, type: fieldType(a), widgets: []) }
            by[name]!.widgets.append((i, a))
        }
    }
    return order.map { by[$0]! }
}

func fieldValue(_ f: Field) -> Any {
    let a = f.widgets[0].a
    switch f.type {
    case "text", "choice": return a.widgetStringValue ?? ""
    case "checkbox": return f.widgets.contains { $0.a.buttonWidgetState == .onState }
    case "radio": return f.widgets.first { $0.a.buttonWidgetState == .onState }?.a.buttonWidgetStateString ?? ""
    default: return ""
    }
}

// the words printed next to a field: a box's (a checkbox, a radio option) on its right, else on its left; a field's on its
// left on the same line, else the lines just above it. A search stops at the next field on the line, and a line above
// that only passes over the field (it began further left: a word cut where the search starts) isn't its label.
func labelNear(_ page: PDFPage, _ r: CGRect, box: Bool) -> String {
    let cb = page.bounds(for: .cropBox)
    func lines(_ rect: CGRect) -> [(String, CGRect)] {
        guard rect.width > 1, rect.height > 1, let s = page.selection(for: rect) else { return [] }
        return s.selectionsByLine().compactMap { l in
            let t = (l.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: "\n", with: " ")
            return t.isEmpty ? nil : (t, l.bounds(for: page))
        }
    }
    func blank(_ rect: CGRect) -> Bool {
        (page.selection(for: rect)?.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    var best: (String, CGFloat)? = nil
    func alnum(_ t: String) -> Int { t.unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }.count }
    func take(_ t: String, _ score: CGFloat) {  // a lone mark (".", "–", "$") is never a label
        if alnum(t) >= 1 && (best == nil || score < best!.1) { best = (t, score) }
    }
    let tol = max(5, r.height * 0.75)
    let row = page.annotations.filter { $0.type == "Widget" && $0.bounds != r && abs($0.bounds.midY - r.midY) < tol }
    let right = min(cb.maxX, row.filter { $0.bounds.minX >= r.maxX - 1 }.map { $0.bounds.minX - 1 }.min() ?? .infinity)
    let left = max(cb.minX, row.filter { $0.bounds.maxX <= r.minX + 1 }.map { $0.bounds.maxX + 1 }.max() ?? -.infinity)
    if box {
        for (t, b) in lines(CGRect(x: r.maxX, y: r.minY - 3, width: right - r.maxX, height: r.height + 6))
        where b.maxX > r.maxX && abs(b.midY - r.midY) < tol { take(t, max(0, b.minX - r.maxX)) }
    }
    for (t, b) in lines(CGRect(x: left, y: r.minY - 2, width: r.minX - left, height: r.height + 4))
    where b.minX < r.minX && abs(b.midY - r.midY) < tol {  // (a line's bounds run on over the spaces after it)
        take(t, max(0, r.minX - b.maxX) * 0.5 + (box ? 12 : 0))
    }
    if !box || best == nil {
        let x0 = max(cb.minX, r.minX - 24)
        let above = lines(CGRect(x: x0, y: r.maxY - 1, width: r.maxX + 4 - x0, height: 36))
            .filter { $0.1.minY >= r.maxY - 4 && (x0 - cb.minX < 3 || blank(CGRect(x: x0 - 3, y: $0.1.midY - 1, width: 3, height: 2))) }
            .sorted { $0.1.minY < $1.1.minY }
        if let first = above.first, first.1.minY - r.maxY < 14 {
            var block = [first], top = first.1
            for l in above.dropFirst() where block.count < 3 && l.1.minY - top.maxY < 3 && l.1.minY > top.minY + 2
                && abs(l.1.minX - first.1.minX) < 20 && abs(l.1.height - first.1.height) < 1.5 {  // the same block of words
                block.append(l)
                top = l.1
            }
            take(block.reversed().map { $0.0 }.joined(separator: " "), first.1.minY - r.maxY + 10)
        }
    }
    guard let (t, score) = best, score < 60 else { return "" }
    return t.count > 120 ? String(t.prefix(119)) + "…" : t
}

func fieldInfo(_ doc: PDFDocument, _ f: Field) -> [String: Any] {
    let (pi, a) = f.widgets[0]
    let page = doc.page(at: pi)!
    let cb = page.bounds(for: .cropBox), r = a.bounds
    var d: [String: Any] = ["n": f.n, "name": f.name, "type": f.type, "value": fieldValue(f), "page": pi + 1,
                            "rect": [r1(r.minX - cb.minX), r1(cb.maxY - r.maxY), r1(r.width), r1(r.height)],
                            "readonly": a.isReadOnly]
    let tip = (a.toolTip ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    d["label"] = tip.isEmpty ? labelNear(page, r, box: f.type == "checkbox") : (tip.count > 160 ? String(tip.prefix(159)) + "…" : tip)
    if f.type == "text" && a.maximumLength > 0 { d["maxlen"] = a.maximumLength }
    if f.type == "text" && a.isMultiline { d["multiline"] = true }
    if f.type == "choice" { d["options"] = a.choices ?? [] }
    if f.type == "radio" {
        d["options"] = f.widgets.map { w -> [String: Any] in
            ["value": w.a.buttonWidgetStateString, "label": labelNear(doc.page(at: w.page)!, w.a.bounds, box: true)]
        }
    }
    return d
}

func pagePicture(_ page: PDFPage, side: CGFloat, part: String, marks: [(Int, CGRect)]) -> CGImage? {
    let box = PDFDisplayBox.cropBox
    let sz = shownSize(page)
    guard sz.width > 0, sz.height > 0 else { return nil }
    var y0: CGFloat = 0, vh = sz.height  // the part shown, from the top
    if part == "top" { vh = sz.height * 0.55 } else if part == "bottom" { y0 = sz.height * 0.45; vh = sz.height * 0.55 }
    let scale = min(4.0, side / max(sz.width, vh))
    let w = Int(sz.width * scale), h = Int(vh * scale)
    guard w > 0, h > 0, let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                           bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    ctx.translateBy(x: 0, y: -(sz.height - y0 - vh))
    ctx.saveGState()
    page.draw(with: box, to: ctx)
    ctx.restoreGState()
    if !marks.isEmpty {  // each field's number, red on white: inside a field's right end (its value starts on the left), left of a box
        let tf = page.transform(for: box)
        NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
        let font = NSFont.boldSystemFont(ofSize: max(6, 9 / max(1, scale / 2)))
        let red = NSColor(red: 0.85, green: 0, blue: 0, alpha: 1)
        for (n, rect) in marks {
            let r = rect.applying(tf)
            ctx.setStrokeColor(red.cgColor)
            ctx.setLineWidth(0.8)
            ctx.stroke(r)
            let s = "\(n)" as NSString, attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: red]
            let ts = s.size(withAttributes: attrs)
            let tag = CGRect(x: r.width < 40 ? r.minX - ts.width - 3 : r.maxX - ts.width - 3, y: r.midY - ts.height / 2, width: ts.width + 2, height: ts.height)
            ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 0.92))
            ctx.fill(tag)
            s.draw(at: NSPoint(x: tag.minX + 1, y: tag.minY), withAttributes: attrs)
        }
        NSGraphicsContext.current = nil
    }
    return ctx.makeImage()
}

func jpegData(_ img: CGImage, _ quality: Double) -> Data? {
    let d = NSMutableData()
    guard let dst = CGImageDestinationCreateWithData(d as CFMutableData, "public.jpeg" as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(dst, img, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
    return CGImageDestinationFinalize(dst) ? d as Data : nil
}

func pdfOut(_ doc: PDFDocument, flatten: Bool = false) -> Data {
    var opts: [AnyHashable: Any] = [:]
    if flatten, #available(macOS 13.0, *) { opts[PDFDocumentWriteOption.burnInAnnotationsOption] = true }
    guard let d = doc.dataRepresentation(options: opts) else { fail("couldn't write the PDF") }
    return d
}

func findField(_ key: String, _ fs: [Field]) -> Field? {
    let k = key.trimmingCharacters(in: .whitespaces)
    if let n = Int(k), n >= 1, n <= fs.count { return fs[n - 1] }
    if let f = fs.first(where: { $0.name == k }) { return f }
    let short = { (s: String) -> String in
        let last = s.split(separator: ".").last.map(String.init) ?? s
        return last.hasSuffix("[0]") ? String(last.dropLast(3)) : last
    }
    let hits = fs.filter { short($0.name) == short(k) }
    return hits.count == 1 ? hits[0] : nil
}

func setField(_ f: Field, _ v: Any) -> (Bool, String, Any?) {
    let a = f.widgets[0].a
    if a.isReadOnly { return (false, "it is read-only", nil) }
    switch f.type {
    case "text":
        var s = asText(v)
        var why = ""
        if a.maximumLength > 0 && s.count > a.maximumLength { s = String(s.prefix(a.maximumLength)); why = "cut to its \(a.maximumLength) characters" }
        for w in f.widgets { w.a.widgetStringValue = s }
        return (true, why, s)
    case "checkbox":
        guard let on = truthy(v) else { return (false, "a checkbox takes true or false", nil) }
        for w in f.widgets { w.a.buttonWidgetState = on ? .onState : .offState }
        return (true, "", on)
    case "radio":
        let s = asText(v).trimmingCharacters(in: .whitespaces)
        if truthy(v) == false && !f.widgets.contains(where: { $0.a.buttonWidgetStateString.lowercased() == s.lowercased() }) {
            for w in f.widgets { w.a.buttonWidgetState = .offState }
            return (true, "", "")
        }
        let pick = f.widgets.first { $0.a.buttonWidgetStateString.lowercased() == s.lowercased() }
        guard let p = pick else {
            return (false, "not one of its options: " + f.widgets.map { $0.a.buttonWidgetStateString }.joined(separator: ", "), nil)
        }
        for w in f.widgets where w.a !== p.a { w.a.buttonWidgetState = .offState }  // the others first: a widget turned
        p.a.buttonWidgetState = .onState                                           // off after it would clear the group's value
        return (true, "", p.a.buttonWidgetStateString)
    case "choice":
        let s = asText(v)
        let opts = a.choices ?? []
        var pick = s
        if !opts.isEmpty {
            guard let o = opts.first(where: { $0.lowercased() == s.lowercased() }) else {
                return (false, "not one of its options: " + opts.prefix(30).joined(separator: ", "), nil)
            }
            pick = o
        }
        for w in f.widgets { w.a.widgetStringValue = pick }
        return (true, "", pick)
    case "signature":
        return (false, "a signature field can't be filled in here; add_text can type a name on its line", nil)
    default:
        return (false, "it is a button, not a field", nil)
    }
}

func same(_ a: Any?, _ b: Any) -> Bool {
    if let x = a as? Bool, isBool(b) { return x == (b as! Bool) }
    return asText(a ?? "") == asText(b)
}

// a block of text placed on a page as a free-text annotation (Preview can still move or change it; flatten burns it in)
func placeText(_ doc: PDFDocument, _ item: [String: Any]) -> [String: Any] {
    let pn = (item["page"] as? Int) ?? 1
    guard pn >= 1, pn <= doc.pageCount, let page = doc.page(at: pn - 1) else { return ["error": "there is no page \(pn)"] }
    let text = (item["text"] as? String) ?? ""
    if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return ["error": "no text"] }
    let size = CGFloat(min(36, max(5, (item["size"] as? Double) ?? 10)))
    let font = NSFont(name: "Helvetica", size: size) ?? NSFont.systemFont(ofSize: size)
    let attrs: [NSAttributedString.Key: Any] = [.font: font]
    let lines = text.components(separatedBy: "\n")
    let w = lines.map { ($0 as NSString).size(withAttributes: attrs).width }.max()! + 6
    let lh = ceil(font.ascender - font.descender + font.leading) + 1
    let h = lh * CGFloat(lines.count) + 4
    let cb = page.bounds(for: .cropBox)
    var x: CGFloat, top: CGFloat
    if let anchor = (item["after"] as? String) ?? (item["below"] as? String) {
        let nth = max(1, (item["nth"] as? Int) ?? 1)
        let found: [PDFSelection] = doc.findString(anchor, withOptions: [.caseInsensitive]).filter { $0.pages.contains(page) }
        let rects: [CGRect] = found.map { $0.bounds(for: page) }
        let hits = rects.sorted { (a: CGRect, b: CGRect) -> Bool in
            if abs(a.midY - b.midY) > 2 { return a.midY > b.midY }
            return a.minX < b.minX
        }
        guard hits.count >= nth else {
            return ["error": hits.isEmpty ? "“\(anchor)” isn't on page \(pn)" : "“\(anchor)” is on page \(pn) only \(hits.count) time\(hits.count == 1 ? "" : "s")"]
        }
        let b = hits[nth - 1]
        if item["after"] != nil { x = b.maxX + 4; top = b.midY + h / 2 } else { x = b.minX; top = b.minY - 2 }
    } else {
        guard let xs = item["x"] as? Double, let ys = item["y"] as? Double else { return ["error": "give x and y, or after or below"] }
        x = cb.minX + CGFloat(xs)
        top = cb.maxY - CGFloat(ys)
    }
    let rect = CGRect(x: x - 2, y: top - h, width: w, height: h)
    if rect.minX < cb.minX - 1 || rect.maxX > cb.maxX + 1 || rect.minY < cb.minY - 1 || rect.maxY > cb.maxY + 1 {
        return ["error": "it would run off page \(pn) (the page is \(r1(cb.width)) × \(r1(cb.height)) points)"]
    }
    let a = PDFAnnotation(bounds: rect, forType: .freeText, withProperties: nil)
    a.contents = text
    a.font = font
    a.fontColor = NSColor.black
    a.color = NSColor.clear
    let border = PDFBorder()
    border.lineWidth = 0
    a.border = border
    a.userName = "StrixFold UI"
    page.addAnnotation(a)
    return ["page": pn, "x": r1(rect.minX - cb.minX), "y": r1(cb.maxY - rect.maxY), "w": r1(rect.width), "h": r1(rect.height)]
}

// the space between blocks TextKit's HTML import leaves out (it drops CSS margins): after a paragraph, a list and a
// table, around a heading (a line in a larger type)
func spaceOut(_ attr: NSMutableAttributedString, body: CGFloat = 11) {
    let ns = attr.string as NSString
    var paras: [NSRange] = [], at = 0
    while at < ns.length {
        let r = ns.paragraphRange(for: NSRange(location: at, length: 0))
        paras.append(r)
        at = NSMaxRange(r)
    }
    let styles = paras.map { (attr.attribute(.paragraphStyle, at: $0.location, effectiveRange: nil) as? NSParagraphStyle) ?? NSParagraphStyle.default }
    let fonts = paras.map { (attr.attribute(.font, at: $0.location, effectiveRange: nil) as? NSFont) ?? NSFont.systemFont(ofSize: body) }
    let sizes = fonts.map { $0.pointSize }
    var before = Array(repeating: CGFloat(0), count: paras.count)
    for k in paras.indices {
        let st = styles[k].mutableCopy() as! NSMutableParagraphStyle
        let table = !st.textBlocks.isEmpty, list = !st.textLists.isEmpty
        let nextTable = k + 1 < paras.count && !styles[k + 1].textBlocks.isEmpty
        let nextList = k + 1 < paras.count && !styles[k + 1].textLists.isEmpty && styles[k + 1].textLists.first === st.textLists.first
        let code = fonts[k].isFixedPitch && k + 1 < paras.count && fonts[k + 1].isFixedPitch  // a code block's lines
        if table {
            if !nextTable && k + 1 < paras.count { before[k + 1] = 10 }
        } else if list {
            st.paragraphSpacing = max(st.paragraphSpacing, nextList ? 2 : 8)
        } else if code {
            st.paragraphSpacing = 0
        } else if sizes[k] >= body + 1.5 {
            st.paragraphSpacingBefore = max(st.paragraphSpacingBefore, k == 0 ? 0 : sizes[k] * 0.75)
            st.paragraphSpacing = max(st.paragraphSpacing, 5)
        } else {
            st.paragraphSpacing = max(st.paragraphSpacing, 8)
        }
        st.paragraphSpacingBefore = max(st.paragraphSpacingBefore, before[k])
        attr.addAttribute(.paragraphStyle, value: st, range: paras[k])
    }
}

// every font in it at `factor` of its size (fitting a document to fewer pages)
func shrink(_ attr: NSAttributedString, _ factor: CGFloat) -> NSMutableAttributedString {
    let out = NSMutableAttributedString(attributedString: attr)
    out.enumerateAttribute(.font, in: NSRange(location: 0, length: out.length)) { v, r, _ in
        if let f = v as? NSFont { out.addAttribute(.font, value: f.withSize(max(5, f.pointSize * factor)), range: r) }
    }
    out.enumerateAttribute(.paragraphStyle, in: NSRange(location: 0, length: out.length)) { v, r, _ in
        if let p = (v as? NSParagraphStyle)?.mutableCopy() as? NSMutableParagraphStyle {
            p.paragraphSpacing *= factor
            p.paragraphSpacingBefore *= factor
            out.addAttribute(.paragraphStyle, value: p, range: r)
        }
    }
    return out
}

// HTML laid out by TextKit, page after page, into a PDF in memory; fit: at most this many pages, the type shrunk in
// steps down to 75% when it runs over (0: as it comes)
func htmlPDF(_ html: String, paper: CGSize, margin: CGFloat, footer: Bool, title: String, fit: Int = 0) -> (Data, Int) {
    _ = NSApplication.shared
    guard let data = html.data(using: .utf8),
          let imported = NSAttributedString(html: data, options: [.characterEncoding: String.Encoding.utf8.rawValue], documentAttributes: nil)
    else { fail("couldn't lay out the document") }
    let attr = NSMutableAttributedString(attributedString: imported)
    while true {
        let r = attr.mutableString.range(of: PAGE_BREAK)
        if r.location == NSNotFound { break }
        attr.replaceCharacters(in: r, with: "\u{0C}")
    }
    spaceOut(attr)
    let foot: CGFloat = footer ? 20 : 0
    let size = NSSize(width: paper.width - 2 * margin, height: paper.height - 2 * margin - foot)
    func layout(_ a: NSAttributedString) -> (NSTextStorage, NSLayoutManager, [NSTextContainer]) {
        let storage = NSTextStorage(attributedString: a)
        let lm = NSLayoutManager()
        storage.addLayoutManager(lm)
        var boxes: [NSTextContainer] = []
        repeat {
            let c = NSTextContainer(size: size)
            lm.addTextContainer(c)
            boxes.append(c)
            lm.ensureLayout(for: c)
            if boxes.count > 400 { fail("too long") }
        } while lm.glyphRange(for: boxes.last!).upperBound < lm.numberOfGlyphs
        while boxes.count > 1 && lm.glyphRange(for: boxes.last!).length == 0 { lm.removeTextContainer(at: boxes.count - 1); boxes.removeLast() }
        return (storage, lm, boxes)
    }
    var (storage, lm, boxes) = layout(attr)
    for factor in [0.94, 0.88, 0.82, 0.75] as [CGFloat] where fit > 0 && boxes.count > fit {
        (storage, lm, boxes) = layout(shrink(attr, factor))
    }
    _ = storage  // the layout manager's text lives as long as this
    let out = NSMutableData()
    var media = CGRect(origin: .zero, size: paper)
    let info = [kCGPDFContextTitle: title, kCGPDFContextCreator: "StrixFold UI"] as CFDictionary
    guard let consumer = CGDataConsumer(data: out as CFMutableData),
          let ctx = CGContext(consumer: consumer, mediaBox: &media, info) else { fail("couldn't write the PDF") }
    let fa: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 8.5), .foregroundColor: NSColor(white: 0.45, alpha: 1)]
    for (i, c) in boxes.enumerated() {
        ctx.beginPDFPage(nil)
        NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: true)
        ctx.saveGState()
        ctx.translateBy(x: 0, y: paper.height)
        ctx.scaleBy(x: 1, y: -1)
        let g = lm.glyphRange(for: c)
        lm.drawBackground(forGlyphRange: g, at: NSPoint(x: margin, y: margin))
        lm.drawGlyphs(forGlyphRange: g, at: NSPoint(x: margin, y: margin))
        if footer {
            let s = "\(i + 1) of \(boxes.count)" as NSString
            let w = s.size(withAttributes: fa).width
            s.draw(at: NSPoint(x: (paper.width - w) / 2, y: paper.height - margin + 4), withAttributes: fa)
        }
        ctx.restoreGState()
        NSGraphicsContext.current = nil
        ctx.endPDFPage()
    }
    ctx.closePDF()
    return (out as Data, boxes.count)
}

func docMain() {
    let (req, files) = docRequest()
    let op = (req["op"] as? String) ?? ""
    switch op {
    case "info":
        let d = files.first
        guard let data = d, let doc = PDFDocument(data: data) else { fail("unreadable") }
        if doc.isLocked && !doc.unlock(withPassword: "") { out(["pages": doc.pageCount, "locked": true]); return }
        let sizes = (0..<min(doc.pageCount, 1000)).map { i -> [Double] in let s = shownSize(doc.page(at: i)!); return [r1(s.width), r1(s.height)] }
        let first = doc.pageCount > 0 ? (doc.page(at: 0)?.string ?? "") : ""
        let placeholder = first.contains("Please wait") && first.contains("not eventually replaced")
        out(["pages": doc.pageCount, "locked": false, "sizes": sizes, "fields": fieldsOf(doc).count, "placeholder": placeholder])
    case "page":
        let doc = openPDF(files.first)
        let pn = (req["page"] as? Int) ?? 1
        guard pn >= 1, pn <= doc.pageCount, let page = doc.page(at: pn - 1) else { fail("page") }
        let marks: [(Int, CGRect)] = (req["marks"] as? Bool) == true
            ? fieldsOf(doc).flatMap { f in f.widgets.filter { $0.page == pn - 1 }.map { (f.n, $0.a.bounds) } } : []
        guard let img = pagePicture(page, side: CGFloat((req["side"] as? Double) ?? 1600), part: (req["part"] as? String) ?? "whole", marks: marks),
              let jpg = jpegData(img, (req["quality"] as? Double) ?? 0.85) else { fail("couldn't draw the page") }
        out(["jpeg": jpg.base64EncodedString(), "width": img.width, "height": img.height, "page": pn, "pages": doc.pageCount,
             "text": (page.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)])
    case "text":  // pages' text, a scanned page's by OCR
        let doc = openPDF(files.first)
        var texts: [String] = []
        for pn in (req["pages"] as? [Int]) ?? [1] {
            guard pn >= 1, pn <= doc.pageCount, let page = doc.page(at: pn - 1) else { fail("page") }
            var t = (page.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if t.count < 8, let img = render(page) { t = ocr(img) }
            texts.append(t)
        }
        out(["pages": doc.pageCount, "text": texts])
    case "fields":
        let doc = openPDF(files.first)
        out(["pages": doc.pageCount, "fields": fieldsOf(doc).map { fieldInfo(doc, $0) }])
    case "fill":
        let doc = openPDF(files.first)
        let fs = fieldsOf(doc)
        let values = (req["values"] as? [String: Any]) ?? [:]
        var results: [[String: Any]] = [], set: [(Field, Any)] = []
        for key in values.keys.sorted(by: { (Int($0) ?? Int.max, $0) < (Int($1) ?? Int.max, $1) }) {
            guard let f = findField(key, fs) else { results.append(["key": key, "ok": false, "why": "no such field"]); continue }
            let (ok, why, v) = setField(f, values[key]!)
            results.append(["key": key, "n": f.n, "name": f.name, "ok": ok, "why": why, "value": v ?? NSNull()])
            if ok, let v = v { set.append((f, v)) }
        }
        let flatten = (req["flatten"] as? Bool) == true
        var data = pdfOut(doc)
        if let back = PDFDocument(data: data) {  // read back from the saved file: what didn't hold is said
            let again = fieldsOf(back)
            for (f, v) in set where !(again.first { $0.name == f.name }.map { same(fieldValue($0), v) } ?? false) {
                if let i = results.firstIndex(where: { ($0["n"] as? Int) == f.n }) { results[i]["ok"] = false; results[i]["why"] = "the value didn't hold in the saved file" }
            }
        }
        if flatten { data = pdfOut(openPDF(data), flatten: true) }
        out(["pdf": data.base64EncodedString(), "pages": doc.pageCount, "results": results])
    case "write":
        let doc = openPDF(files.first)
        let placed = ((req["items"] as? [[String: Any]]) ?? []).map { placeText(doc, $0) }
        out(["pdf": pdfOut(doc, flatten: (req["flatten"] as? Bool) == true).base64EncodedString(), "pages": doc.pageCount, "placed": placed])
    case "combine":
        let docs = files.map { openPDF($0) }
        let made = PDFDocument()
        for part in (req["parts"] as? [[String: Any]]) ?? [] {
            let fi = (part["file"] as? Int) ?? 0
            guard fi >= 0, fi < docs.count else { fail("bad request") }
            let rot = (part["rotate"] as? Int) ?? 0
            for pn in (part["pages"] as? [Int]) ?? Array(1...max(1, docs[fi].pageCount)) {
                guard pn >= 1, pn <= docs[fi].pageCount, let p = docs[fi].page(at: pn - 1)?.copy() as? PDFPage else { fail("page") }
                if rot != 0 { p.rotation = ((p.rotation + rot) % 360 + 360) % 360 }
                made.insert(p, at: made.pageCount)
            }
        }
        if made.pageCount == 0 { fail("no pages") }
        out(["pdf": pdfOut(made).base64EncodedString(), "pages": made.pageCount])
    case "html":
        let paper = (req["paper"] as? [Double]).map { CGSize(width: $0[0], height: $0[1]) } ?? CGSize(width: 612, height: 792)
        let (d, n) = htmlPDF((req["html"] as? String) ?? "", paper: paper, margin: CGFloat((req["margin"] as? Double) ?? 54),
                             footer: (req["footer"] as? Bool) ?? true, title: (req["title"] as? String) ?? "", fit: (req["fit"] as? Int) ?? 0)
        out(["pdf": d.base64EncodedString(), "pages": n])
    default:
        fail("bad request")
    }
}

let args = CommandLine.arguments
guard args.count >= 3 else { fail("usage: halo-extract pdf FILE MAX_PAGES MAX_OCR_PAGES | image FILE") }
let url = URL(fileURLWithPath: args[2])
let fromStdin = args[2] == "-"

func inputData() -> Data? {
    fromStdin ? FileHandle.standardInput.readDataToEndOfFile() : try? Data(contentsOf: url)
}

func imageSource() -> CGImageSource? {
    guard let src = fromStdin ? inputData().flatMap({ CGImageSourceCreateWithData($0 as CFData, nil) }) : CGImageSourceCreateWithURL(url as CFURL, nil),
          CGImageSourceGetCount(src) > 0 else { return nil }
    return src
}

switch args[1] {
case "pdf":
    let maxPages = args.count > 3 ? Int(args[3]) ?? 200 : 200
    let maxOCR = args.count > 4 ? Int(args[4]) ?? 20 : 20
    guard let doc = fromStdin ? inputData().flatMap({ PDFDocument(data: $0) }) : PDFDocument(url: url) else { fail("not a readable PDF") }
    if doc.isLocked && !doc.unlock(withPassword: "") {
        out(["pages": doc.pageCount, "read": 0, "ocr": 0, "locked": true, "text": []])
        exit(0)
    }
    var texts: [String] = []
    var ocrDone = 0
    for i in 0..<min(doc.pageCount, maxPages) {
        guard let page = doc.page(at: i) else { texts.append(""); continue }
        var t = (page.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if t.count < 8 && ocrDone < maxOCR, let img = render(page) {  // no text layer: a scanned page
            t = ocr(img)
            ocrDone += 1
        }
        texts.append(t)
    }
    out(["pages": doc.pageCount, "read": texts.count, "ocr": ocrDone, "locked": false, "text": texts])
case "image":
    guard let src = imageSource() else { fail("not a readable image") }
    let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any] ?? [:]
    let w = props[kCGImagePropertyPixelWidth] as? Int ?? 0, h = props[kCGImagePropertyPixelHeight] as? Int ?? 0
    // at most 4096 px on the long side (a huge image is read scaled down), turned upright as its EXIF says
    let opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: 4096,
                                 kCGImageSourceCreateThumbnailWithTransform: true]
    guard let img = CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary) else { fail("not a readable image") }
    out(["width": w, "height": h, "text": ocr(img)])
case "see":
    let maxPixels = args.count > 3 ? max(1024, Double(args[3]) ?? 1_100_000) : 1_100_000
    let quality = args.count > 4 ? min(1, max(0.3, Double(args[4]) ?? 0.85)) : 0.85
    guard let src = imageSource() else { fail("not a readable image") }
    let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any] ?? [:]
    let w = Double(props[kCGImagePropertyPixelWidth] as? Int ?? 0), h = Double(props[kCGImagePropertyPixelHeight] as? Int ?? 0)
    guard w > 0, h > 0 else { fail("not a readable image") }
    let side = max(32, Int(max(w, h) * min(1.0, (maxPixels / (w * h)).squareRoot())))
    let opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: side,
                                 kCGImageSourceCreateThumbnailWithTransform: true]
    guard let img = CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary) else { fail("not a readable image") }
    let jpeg = NSMutableData()  // a new image with only its pixels: no EXIF, GPS or camera data is carried over
    guard let dst = CGImageDestinationCreateWithData(jpeg as CFMutableData, "public.jpeg" as CFString, 1, nil) else { fail("cannot encode") }
    CGImageDestinationAddImage(dst, img, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
    guard CGImageDestinationFinalize(dst) else { fail("cannot encode") }
    out(["width": img.width, "height": img.height, "jpeg": (jpeg as Data).base64EncodedString()])
case "make-pdf":
    let text = args.count > 3 ? args[3] : "StrixFold UI test"
    var box = CGRect(x: 0, y: 0, width: 612, height: 792)
    guard let ctx = CGContext(url as CFURL, mediaBox: &box, nil) else { fail("cannot write") }
    ctx.beginPDFPage(nil)
    if args.count > 4 && args[4] == "scan" {
        ctx.draw(textImage(text), in: CGRect(x: 36, y: 500, width: 540, height: 193))
    } else {
        NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
        (text as NSString).draw(in: CGRect(x: 72, y: 400, width: 468, height: 320), withAttributes: [.font: NSFont.systemFont(ofSize: 18)])
        NSGraphicsContext.current = nil
    }
    ctx.endPDFPage()
    ctx.closePDF()
    out(["ok": true])
case "make-png":
    let img = textImage(args.count > 3 ? args[3] : "StrixFold UI test")
    guard let dst = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else { fail("cannot write") }
    CGImageDestinationAddImage(dst, img, nil)
    CGImageDestinationFinalize(dst)
    out(["ok": true])
case "make-photo":
    let img = textImage(args.count > 3 ? args[3] : "StrixFold UI test")
    guard let dst = CGImageDestinationCreateWithURL(url as CFURL, "public.jpeg" as CFString, 1, nil) else { fail("cannot write") }
    let meta: [CFString: Any] = [
        kCGImagePropertyOrientation: 6,
        kCGImagePropertyGPSDictionary: [kCGImagePropertyGPSLatitude: 37.7749, kCGImagePropertyGPSLatitudeRef: "N",
                                        kCGImagePropertyGPSLongitude: 122.4194, kCGImagePropertyGPSLongitudeRef: "W"],
        kCGImagePropertyTIFFDictionary: [kCGImagePropertyTIFFMake: "HaloTestPhone", kCGImagePropertyTIFFModel: "Model Q"],
        kCGImagePropertyExifDictionary: [kCGImagePropertyExifUserComment: "halo test comment"]]
    CGImageDestinationAddImage(dst, img, meta as CFDictionary)
    guard CGImageDestinationFinalize(dst) else { fail("cannot write") }
    out(["ok": true])
case "doc":
    docMain()
case "make-form":
    // tests only: a fillable two-page form (fields beside their labels; "Phone:" has no field; page 2 turned a quarter)
    var box = CGRect(x: 0, y: 0, width: 612, height: 792)
    let pdf = NSMutableData()
    guard let consumer = CGDataConsumer(data: pdf as CFMutableData), let ctx = CGContext(consumer: consumer, mediaBox: &box, nil) else { fail("cannot write") }
    let f: [NSAttributedString.Key: Any] = [.font: NSFont(name: "Helvetica", size: 12)!, .foregroundColor: NSColor.black]
    ctx.beginPDFPage(nil)
    NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
    ("Membership form" as NSString).draw(at: NSPoint(x: 72, y: 740), withAttributes: [.font: NSFont(name: "Helvetica-Bold", size: 18)!])
    for (s, x, y) in [("Full name:", 72.0, 700.0), ("Email address:", 72.0, 660.0), ("I agree to the terms", 92.0, 620.0), ("Plan:", 72.0, 580.0),
                      ("Basic", 138.0, 580.0), ("Pro", 218.0, 580.0), ("Country:", 72.0, 540.0), ("Phone:", 72.0, 500.0)] {
        (s as NSString).draw(at: NSPoint(x: x, y: y), withAttributes: f)
    }
    NSGraphicsContext.current = nil
    ctx.endPDFPage()
    ctx.beginPDFPage(nil)
    NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
    ("ROTATED PAGE" as NSString).draw(at: NSPoint(x: 72, y: 700), withAttributes: f)
    NSGraphicsContext.current = nil
    ctx.endPDFPage()
    ctx.closePDF()
    guard let doc = PDFDocument(data: pdf as Data), let p1 = doc.page(at: 0) else { fail("cannot write") }
    func widget(_ r: CGRect, _ name: String, _ t: PDFAnnotationWidgetSubtype) -> PDFAnnotation {
        let a = PDFAnnotation(bounds: r, forType: .widget, withProperties: nil)
        a.widgetFieldType = t
        a.fieldName = name
        a.font = NSFont(name: "Helvetica", size: 11)
        a.backgroundColor = NSColor(white: 0.95, alpha: 1)
        p1.addAnnotation(a)
        return a
    }
    _ = widget(CGRect(x: 140, y: 696, width: 300, height: 20), "full_name", .text)
    widget(CGRect(x: 160, y: 656, width: 280, height: 20), "email", .text).maximumLength = 40
    let cb = widget(CGRect(x: 72, y: 620, width: 14, height: 14), "agree", .button)
    cb.widgetControlType = .checkBoxControl
    cb.buttonWidgetStateString = "Yes"
    for (x, v) in [(120.0, "Basic"), (200.0, "Pro")] {
        let r = widget(CGRect(x: x, y: 580, width: 14, height: 14), "plan", .button)
        r.widgetControlType = .radioButtonControl
        r.buttonWidgetStateString = v
    }
    let ch = widget(CGRect(x: 140, y: 536, width: 200, height: 20), "country", .choice)
    ch.isListChoice = false
    ch.choices = ["Canada", "Mexico", "United States"]
    doc.page(at: 1)?.rotation = 90
    guard doc.write(to: url) else { fail("cannot write") }
    out(["ok": true])
default:
    fail("usage: halo-extract pdf FILE MAX_PAGES MAX_OCR_PAGES | image FILE")
}
