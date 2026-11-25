"""Synthetic Office files for the tests (built here with zipfile; never a real document): an Excel workbook with
shared and inline strings, number, percent, currency and date formats, a formula with and without its cached value,
a merged category column, empty cells and rows, a hidden sheet and a long sheet; a Word document with headings, a
list and a table; a PowerPoint deck whose slide order differs from its file names."""
import io
import zipfile

CT = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def rels(items):
    return (f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PR}">'
            + "".join(f'<Relationship Id="{i}" Type="{R}/{t}" Target="{tg}"/>' for i, t, tg in items) + "</Relationships>")


def pack(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


def sheet(rows, merges=(), dim=None):
    body = "".join(f'<row r="{r}">' + "".join(cells) + "</row>" for r, cells in rows)
    mc = f'<mergeCells count="{len(merges)}">' + "".join(f'<mergeCell ref="{m}"/>' for m in merges) + "</mergeCells>" if merges else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{S}" xmlns:r="{R}"><sheetData>{body}</sheetData>{mc}</worksheet>'


def xlsx(long_rows=0, extra=None):
    shared = ["Region", "Item", "Sales", "Share", "Date", "North", "Widgets", "Gadgets", "South", "Gizmos", "Total"]
    sst = (f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{S}" count="{len(shared)}" uniqueCount="{len(shared)}">'
           + "".join(f"<si><t>{s}</t></si>" for s in shared[:-1])
           + '<si><r><t>To</t></r><r><t>tal</t></r><rPh sb="0" eb="1"><t>ignored</t></rPh></si></sst>')
    styles = (f'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{S}"><numFmts count="2">'
              '<numFmt numFmtId="164" formatCode="&quot;$&quot;#,##0.00"/><numFmt numFmtId="165" formatCode="yyyy-mm-dd"/></numFmts>'
              '<cellStyleXfs count="1"><xf numFmtId="0"/></cellStyleXfs>'
              '<cellXfs count="5"><xf numFmtId="0"/><xf numFmtId="164"/><xf numFmtId="10"/><xf numFmtId="14"/><xf numFmtId="165"/></cellXfs></styleSheet>')
    s = lambda ref, i: f'<c r="{ref}" t="s"><v>{i}</v></c>'  # noqa: E731
    v = lambda ref, x, st=0: f'<c r="{ref}" s="{st}"><v>{x}</v></c>'  # noqa: E731
    data = sheet([
        (1, [s("B1", 0), s("C1", 1), s("D1", 2), s("E1", 3), s("F1", 4)]),
        (2, [s("B2", 5), s("C2", 6), v("D2", 1234.5, 1), v("E2", 0.4567, 2), v("F2", 45950, 4)]),
        (3, [s("C3", 7), v("D3", 99, 1), '<c r="E3" s="2"/>', v("F3", 45951, 3)]),
        (5, [s("B5", 8), s("C5", 9), v("D5", -20, 1), '<c r="E5" t="inlineStr"><is><t>inline, "quoted"</t></is></c>']),
        (6, [s("B6", 10), '<c r="D6" s="1"><f>SUM(D2:D5)</f><v>1313.5</v></c>', '<c r="E6"><f>NOW()</f></c>',
             '<c r="F6" t="b"><v>1</v></c>']),
        (7, ['<c r="B7" s="0"/>']),  # a styled blank: no row
    ], merges=("B2:B3",))
    parts = {
        "_rels/.rels": rels([("rId1", "officeDocument", "xl/workbook.xml")]),
        "xl/workbook.xml": (f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{S}" xmlns:r="{R}"><workbookPr/><sheets>'
                            '<sheet name="Sales" sheetId="1" r:id="rId1"/><sheet name="Notes" sheetId="2" state="hidden" r:id="rId2"/>'
                            + ('<sheet name="Log" sheetId="3" r:id="rId3"/>' if long_rows else "") + "</sheets></workbook>"),
        "xl/_rels/workbook.xml.rels": rels([("rId1", "worksheet", "worksheets/sheet1.xml"), ("rId2", "worksheet", "/xl/worksheets/sheet2.xml"),
                                            ("rId3", "worksheet", "worksheets/sheet3.xml"), ("rId9", "sharedStrings", "sharedStrings.xml"),
                                            ("rId8", "styles", "styles.xml")]),
        "xl/sharedStrings.xml": sst, "xl/styles.xml": styles, "xl/worksheets/sheet1.xml": data,
        "xl/worksheets/sheet2.xml": sheet([(1, ['<c r="A1" t="inlineStr"><is><t>secret-ish note</t></is></c>'])]),
    }
    if long_rows:
        parts["xl/worksheets/sheet3.xml"] = sheet([(r, [f'<c r="A{r}"><v>{r}</v></c>', f'<c r="B{r}" t="inlineStr"><is><t>entry number {r} with some words</t></is></c>'])
                                                   for r in range(1, long_rows + 1)])
    parts.update(extra or {})
    return pack(parts)


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx():
    p = lambda text, style=None, num=False: ('<w:p><w:pPr>' + (f'<w:pStyle w:val="{style}"/>' if style else "")  # noqa: E731
                                            + ('<w:numPr><w:ilvl w:val="0"/></w:numPr>' if num else "") + f'</w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')
    cell = lambda t: f"<w:tc><w:p><w:r><w:t>{t}</w:t></w:r></w:p></w:tc>"  # noqa: E731
    body = (p("Quarterly plan", "Title") + p("Goals", "Heading1") + p("Ship the widget", num=True) + p("Hire two people", num=True)
            + '<w:p><w:r><w:t>Before</w:t></w:r><w:r><w:tab/><w:t>tab</w:t></w:r><w:del><w:r><w:delText>gone</w:delText></w:r></w:del></w:p>'
            + "<w:tbl>" + "".join("<w:tr>" + "".join(cell(c) for c in row) + "</w:tr>" for row in (("Name", "Role"), ("Ada", "Lead|Eng"))) + "</w:tbl>"
            + p(""))
    return pack({"_rels/.rels": rels([("rId1", "officeDocument", "word/document.xml")]),
                 "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'})


P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def pptx():
    slide = lambda *paras: (f'<?xml version="1.0" encoding="UTF-8"?><p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'  # noqa: E731
                            + "".join(f'<p:sp><p:txBody><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:txBody></p:sp>' for t in paras)
                            + "</p:spTree></p:cSld></p:sld>")
    return pack({"_rels/.rels": rels([("rId1", "officeDocument", "ppt/presentation.xml")]),
                 "ppt/presentation.xml": (f'<?xml version="1.0" encoding="UTF-8"?><p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
                                          '<p:sldId id="256" r:id="rId7"/><p:sldId id="257" r:id="rId3"/></p:sldIdLst></p:presentation>'),
                 "ppt/_rels/presentation.xml.rels": rels([("rId3", "slide", "slides/slide1.xml"), ("rId7", "slide", "slides/slide2.xml")]),
                 "ppt/slides/slide1.xml": slide("Second slide", "Details here"),
                 "ppt/slides/slide2.xml": slide("Opening title", "Subtitle")})
