"""Office files for StrixFold UI's chat, read with the standard library only (zipfile + xml.etree): Excel (.xlsx, .xlsm)
sheets as CSV tables, Word (.docx) paragraphs and tables, PowerPoint (.pptx) slide text, in order. Older binary
Office files (.xls, .doc, .ppt), password-protected files, PDFs and other archives are refused with what to do
instead. Nothing here runs a macro, follows a link or expands an XML entity: a part that declares a DTD is refused
(this Python's expat predates entity-expansion limits), every part is read in chunks, and the bytes inflated from
the zip are capped (a zip bomb stops at the cap).

read(data, name, limit) -> Doc(text, detail, kind) for an Office file, None for anything else (text, media), or
raises Refused (a ValueError) with a message for the user."""
import csv
import datetime
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_CHARS = 400_000        # the most text one attachment carries (halo_chat.MAX_FILE_CHARS)
MAX_BYTES = 50 << 20       # the Office file itself
MAX_INFLATE = 256 << 20    # bytes decompressed from it, all parts together
CHUNK = 1 << 16
CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE compound file: .xls/.doc/.ppt, or an encrypted .xlsx/.docx/.pptx


class Refused(ValueError):
    pass


class Doc:
    def __init__(self, text, detail, kind, truncated=False):
        self.text, self.detail, self.kind, self.truncated = text, detail, kind, truncated


def ext(name):
    return posixpath.splitext(str(name or "").lower())[1]


def refusal(data, name):
    """Why a file that is not text, media or a readable Office file cannot be attached, in words for the user."""
    e = ext(name)
    if data[:8] == CFB:
        if e in (".xlsx", ".xlsm", ".docx", ".pptx"):
            return "this file is password-protected. Remove the password, save it and attach it again."
        what = {".xls": ("an older Excel file", ".xlsx or .csv"), ".doc": ("an older Word file", ".docx"),
                ".ppt": ("an older PowerPoint file", ".pptx")}.get(e, ("an older Office file", ".xlsx, .docx or .pptx"))
        return f"{what[0]} ({e or 'binary'}). Save it as {what[1]} and attach it again."
    if data[:5] == b"%PDF-":
        return "PDFs aren't read here yet. Save it as .docx or .txt, or paste the text, and attach it again."
    if e == ".xlsb":
        return "a binary Excel workbook (.xlsb). Save it as .xlsx or .csv and attach it again."
    if e in (".ods", ".odt", ".odp", ".numbers", ".pages", ".key"):
        return f"a {e} document. Save it as .xlsx, .docx, .pptx or .csv and attach it again."
    if data[:4] == b"PK\x03\x04":
        return "a .zip archive. Attach the files inside it instead."
    return "this kind of file can't be read. Attach text, code, CSV, or an Excel, Word or PowerPoint file."


def read(data, name="", limit=MAX_CHARS):
    if data[:8] == CFB or data[:5] == b"%PDF-":
        raise Refused(refusal(data, name))
    if data[:4] != b"PK\x03\x04":
        return None
    if len(data) > MAX_BYTES:
        raise Refused(f"Office files up to {MAX_BYTES >> 20} MB")
    try:
        p = Package(data)
        main, kind = p.main()
        if kind == "xlsx":
            return xlsx(p, main, limit)
        if kind == "docx":
            return docx(p, main, limit)
        if kind == "pptx":
            return pptx(p, main, limit)
    except Refused:
        raise
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, IndexError, OSError, EOFError, NotImplementedError,
            RuntimeError):
        raise Refused("the file could not be read. Save it again from its app and attach it again.")
    raise Refused(refusal(data, name))


# ---------------------------------------------------------------- the package (zip + XML parts)
def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def attr(el, name):
    """An attribute by its local name, whatever its namespace (transitional or strict OOXML)."""
    for k, v in el.attrib.items():
        if local(k) == name:
            return v
    return None


def rid(el):
    """The relationship id (r:id) of an element: namespaced, never a plain "id" beside it (a slide's sldId has both)."""
    return next((v for k, v in el.attrib.items() if k.startswith("{") and local(k) == "id"), None)


class Package:
    def __init__(self, data):
        self.z = zipfile.ZipFile(io.BytesIO(data))
        self.names = {n for n in self.z.namelist()}
        self.inflated = 0

    def stream(self, name):
        """A part's elements as they end (read in chunks: a DTD refused, inflation capped). The caller may stop early."""
        if name not in self.names:
            raise KeyError(name)
        p = ET.XMLPullParser(events=("end",))
        tail = b""
        with self.z.open(name) as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                self.inflated += len(chunk)
                if self.inflated > MAX_INFLATE:
                    raise Refused(f"the file holds more than {MAX_INFLATE >> 20} MB of data, too much to read here")
                look = tail + chunk
                if b"<!DOCTYPE" in look or b"<!ENTITY" in look:
                    raise Refused("the file could not be read safely")
                tail = chunk[-16:]
                p.feed(chunk)
                for _, el in p.read_events():
                    yield el

    def tree(self, name):
        """A whole (small) part: its root element, read through stream()."""
        root = None
        for el in self.stream(name):
            root = el
        if root is None:
            raise ValueError(f"{name} is empty")
        return root

    def rels(self, part):
        """{relationship id: (type, part name)} of a part (internal targets only)."""
        d, b = posixpath.split(part)
        rp = posixpath.join(d, "_rels", b + ".rels")
        out = {}
        if rp not in self.names:
            return out
        for el in self.tree(rp).iter():
            if local(el.tag) == "Relationship" and (el.get("TargetMode") or "").lower() != "external":
                t = el.get("Target") or ""
                target = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join(d, t))
                out[el.get("Id")] = (el.get("Type") or "", target)
        return out

    def main(self):
        for typ, target in self.rels("").values():
            if typ.endswith("/officeDocument") and target in self.names:
                low = target.lower()
                kind = "xlsx" if low.startswith("xl/") and low.endswith(".xml") else "docx" if low.startswith("word/") else \
                    "pptx" if low.startswith("ppt/") else None
                return target, kind
        return None, None

    def related(self, part, kind):
        return next((t for typ, t in self.rels(part).values() if typ.endswith("/" + kind) and t in self.names), None)


def n(x, word):
    return f"{x:,} {word}{'' if x == 1 else 's'}"


# ---------------------------------------------------------------- Excel
# 14 and 22 follow the computer's date setting in Excel: shown here as ISO dates, which read the same everywhere
BUILTIN_FORMATS = {0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 11: "0.00E+00",
                   12: "General", 13: "General", 14: "yyyy-mm-dd", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy", 18: "h:mm AM/PM",
                   19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss", 22: "yyyy-mm-dd h:mm", 37: "#,##0 ;(#,##0)", 38: "#,##0 ;(#,##0)",
                   39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;(#,##0.00)", 45: "mm:ss", 46: "[h]:mm:ss", 47: "mm:ss.0",
                   48: "##0.0E+0", 49: "@"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def general(x):
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    s = f"{x:.10g}"
    return s.replace("e+", "E+").replace("e-", "E-")


def split_literals(code):
    """(code with each literal as one private-use character, the literals): quoted text, \\x escapes, [$€-409]
    currency; colours, conditions, _x paddings and *x fills dropped."""
    lits, out, i = [], [], 0
    while i < len(code):
        ch = code[i]
        if ch == '"':
            j = code.find('"', i + 1)
            j = len(code) if j < 0 else j
            lits.append(code[i + 1:j])
            out.append(chr(0xE000 + len(lits) - 1))
            i = j + 1
        elif ch == "\\" and i + 1 < len(code):
            lits.append(code[i + 1])
            out.append(chr(0xE000 + len(lits) - 1))
            i += 2
        elif ch in "_*" and i + 1 < len(code):
            i += 2
        elif ch == "[":
            j = code.find("]", i)
            j = len(code) if j < 0 else j
            inner = code[i + 1:j]
            if inner.startswith("$"):
                sym = inner[1:].split("-", 1)[0]
                if sym:
                    lits.append(sym)
                    out.append(chr(0xE000 + len(lits) - 1))
            elif re.fullmatch(r"[hHmMsS]+", inner):
                out.append("[" + inner.lower() + "]")  # elapsed time
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out), lits


def restore(s, lits):
    return "".join(lits[ord(c) - 0xE000] if 0xE000 <= ord(c) < 0xE000 + len(lits) else c for c in s)


def is_date(code):
    if not code or code.lower() in ("general", "@"):
        return False
    c, _ = split_literals(code.split(";")[0])
    return bool(re.search(r"[ymdhs]", re.sub(r"(?i)general|am/pm|a/p", "", c), re.I))


def show_date(x, code, date1904):
    """A date/time serial as the format code shows it (falling back to ISO)."""
    sec, lits = split_literals(code.split(";")[0])
    # Excel's day 1 is 1900-01-01 and it counts a 29 February 1900 that never was: before day 61 the base moves a day
    base = datetime.datetime(1904, 1, 1) if date1904 else datetime.datetime(1899, 12, 31 if x < 61 else 30)
    try:
        dt = base + datetime.timedelta(days=x)
    except OverflowError:
        return general(x)
    if not re.search(r"\.0", sec):  # shown to the second: rounded, as Excel does
        dt = (dt + datetime.timedelta(microseconds=500_000)).replace(microsecond=0)
    toks = list(re.finditer(r"(?i)\[h+\]|\[m+\]|\[s+\]|yyyy|yy|mmmmm|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s|am/pm|a/p|\.0+", sec))
    ampm = any(t.group().lower() in ("am/pm", "a/p") for t in toks)
    out, pos = [], 0
    for i, t in enumerate(toks):
        out.append(sec[pos:t.start()])
        pos = t.end()
        g = t.group().lower()
        prev = next((x.group().lower() for x in reversed(toks[:i]) if x.group().lower()[0] in "hdmy" or x.group().startswith("[")), "")
        nxt = next((x.group().lower() for x in toks[i + 1:] if x.group().lower()[0] in "sdmy" or x.group().startswith("[")), "")
        minute = g in ("m", "mm") and (prev.startswith(("h", "[h")) or nxt.startswith(("s", "[s")))
        h12 = (dt.hour % 12) or 12
        total = x * 86400
        out.append({
            "yyyy": f"{dt.year:04d}", "yy": f"{dt.year % 100:02d}", "mmmmm": MONTHS[dt.month - 1][0], "mmmm": MONTHS[dt.month - 1],
            "mmm": MONTHS[dt.month - 1][:3], "mm": f"{dt.minute:02d}" if minute else f"{dt.month:02d}",
            "m": str(dt.minute) if minute else str(dt.month), "dddd": DAYS[dt.weekday()], "ddd": DAYS[dt.weekday()][:3],
            "dd": f"{dt.day:02d}", "d": str(dt.day), "hh": f"{(h12 if ampm else dt.hour):02d}", "h": str(h12 if ampm else dt.hour),
            "ss": f"{dt.second:02d}", "s": str(dt.second), "am/pm": "AM" if dt.hour < 12 else "PM", "a/p": "A" if dt.hour < 12 else "P",
        }.get(g) or (str(int(total // 3600)) if g.startswith("[h") else str(int(total // 60)) if g.startswith("[m")
                     else str(int(total)) if g.startswith("[s") else (f"{(x * 86400) % 1:.{len(g) - 1}f}"[1:] if g.startswith(".") else t.group())))
    out.append(sec[pos:])
    return restore("".join(out), lits).strip()


def show_number(x, code, date1904=False):
    """A number as its cell's format code shows it, for the common codes (decimals, thousands, %, scientific, currency
    and other literals, a negative section); anything else as Excel's General."""
    if not code or code.lower() in ("general", "@"):
        return general(x)
    if is_date(code):
        return show_date(x, code, date1904)
    secs = code.split(";")
    second = x < 0 and len(secs) > 1 and secs[1] != ""  # a negative number's own section (brackets, a sign of its own)
    sec, v, neg = (secs[1] if second else secs[0]), abs(x), x < 0 and not second
    s, lits = split_literals(sec)
    if s.lower().strip() == "general":
        return restore(general(v), lits)
    m = re.search(r"[0#?][0#?,.]*(?:[eE][+-][0#?]+)?|\.[0#?]+", s)
    if not m:
        return restore(s, lits) if s.strip() else general(x)
    core = m.group()
    v = v * 100 ** s.count("%")
    e = re.search(r"[eE][+-]", core)
    if e:
        mant = core[:e.start()]
        dec = len(mant.split(".")[1]) if "." in mant else 0
        mpart, epart = f"{v:.{dec}E}".split("E")
        body = f"{mpart}E{epart[0]}{int(epart[1:]):0{max(1, len(core) - e.end())}d}"
    else:
        scale = len(core) - len(core.rstrip(","))  # each trailing comma divides by 1,000 ("0.0,," shows millions)
        ip, _, fp = core.rstrip(",").partition(".")
        v = v / 1000 ** scale
        dec = len(re.findall(r"[0#?]", fp))
        group = "," in ip.rstrip(",")
        body = f"{v:,.{dec}f}" if group else f"{v:.{dec}f}"
        if body.lstrip("-").startswith("0") and not re.search(r"0", ip) and ip:  # "#.00": no leading zero
            body = body.replace("0.", ".", 1)
    out = restore(s[:m.start()] + body + s[m.end():], lits).strip()
    return ("-" if neg else "") + out


def col_index(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        return None
    i = 0
    for ch in letters.group():
        i = i * 26 + ord(ch) - 64
    return i - 1


def col_name(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx(p, wb, limit):
    root = p.tree(wb)
    date1904 = any(local(e.tag) == "workbookPr" and (e.get("date1904") or "").lower() in ("1", "true") for e in root.iter())
    rels = p.rels(wb)
    sheets = [(e.get("name") or "Sheet", (e.get("state") or "visible"), rels.get(rid(e), ("", None))[1])
              for e in root.iter() if local(e.tag) == "sheet"]
    shared = []
    ss = p.related(wb, "sharedStrings")
    if ss:
        for el in p.stream(ss):
            if local(el.tag) == "si":  # plain text, or rich-text runs; the phonetic guide (rPh) left out
                bits = []
                for ch in el:
                    if local(ch.tag) == "t":
                        bits.append(ch.text or "")
                    elif local(ch.tag) == "r":
                        bits += [x.text or "" for x in ch if local(x.tag) == "t"]
                shared.append("".join(bits))
                el.clear()
    fmts, xfs = dict(BUILTIN_FORMATS), []
    st = p.related(wb, "styles")
    if st:
        sroot = p.tree(st)
        for e in sroot.iter():
            if local(e.tag) == "numFmt" and (e.get("numFmtId") or "").isdigit():
                fmts[int(e.get("numFmtId"))] = e.get("formatCode") or "General"
        for e in sroot:
            if local(e.tag) == "cellXfs":
                xfs = [int(x.get("numFmtId") or 0) for x in e if local(x.tag) == "xf"]
    code_of = lambda s: fmts.get(xfs[s], "General") if 0 <= s < len(xfs) else "General"  # noqa: E731

    def value(c):
        t = c.get("t") or "n"
        v = f = None
        inline = []
        for ch in c:
            k = local(ch.tag)
            if k == "v":
                v = ch.text
            elif k == "f":
                f = ch.text
            elif k == "is":
                inline = [x.text or "" for x in ch.iter() if local(x.tag) == "t"]
        if t == "inlineStr":
            return "".join(inline)
        if v is None or v == "":
            return f"={f}" if f else ""  # a formula never calculated: its text
        if t == "s":
            i = int(v)
            return shared[i] if 0 <= i < len(shared) else ""
        if t == "b":
            return "TRUE" if v.strip() in ("1", "true") else "FALSE"
        if t in ("str", "e", "d"):
            return v
        try:
            x = float(v)
        except ValueError:
            return v
        try:
            return show_number(x, code_of(int(c.get("s") or 0)), date1904)
        except (ValueError, OverflowError, IndexError, ZeroDivisionError):
            return general(x)

    used, cut = 0, False
    sheet_rows = []
    for name, state, part in sheets:
        if not part or part not in p.names:
            continue
        rows, merges, cur, last_r, n_rows, seen = {}, [], {}, 0, 0, False
        for el in p.stream(part):
            k = local(el.tag)
            if k == "c":
                if cut:  # past the text limit: rows are counted, not read
                    seen = seen or any(local(ch.tag) in ("v", "is", "f") for ch in el)
                else:
                    ci = col_index(el.get("r"))
                    ci = (max(cur) + 1 if cur else 0) if ci is None else ci
                    val = value(el)
                    if val != "":
                        cur[ci] = val
                el.clear()
            elif k == "row":
                r = int(el.get("r")) if (el.get("r") or "").isdigit() else last_r + 1
                last_r = r
                if cur:
                    rows[r] = cur
                    used += sum(len(x) for x in cur.values()) + 2 * len(cur) + 8
                    cut = used > limit
                n_rows += bool(cur or seen)
                cur, seen = {}, False
                el.clear()
            elif k == "mergeCell" and not cut:
                merges.append(el.get("ref") or "")
        # merged cells: a merge down one column repeats its value (a category beside its rows); wider merges keep it once
        for ref in merges:
            a, _, b = ref.partition(":")
            if not b:
                continue
            c1, c2 = col_index(a), col_index(b)
            r1, r2 = int(re.sub(r"[A-Z]", "", a) or 0), int(re.sub(r"[A-Z]", "", b) or 0)
            if c1 == c2 and r2 > r1 and c1 in rows.get(r1, {}):
                for r in range(r1 + 1, r2 + 1):
                    if r in rows:
                        rows[r].setdefault(c1, rows[r1][c1])
        sheet_rows.append((name, state, rows, n_rows))
    return render_sheets(sheet_rows, "xlsx")


def render_sheets(sheet_rows, kind):
    """[(sheet name, state, {row number: {column index: shown value}}, rows in the sheet)] as the chat reads a workbook:
    each sheet as CSV under its name, with the sheet's row numbers, and a note where the text limit cut it."""
    parts = []
    rows_total = sum(x[3] for x in sheet_rows)
    rows_read = sum(len(x[2]) for x in sheet_rows)
    for name, state, rows, n_rows in sheet_rows:
        read_n = len(rows)
        head = f'## Sheet "{name}"' + (" (hidden)" if state != "visible" else "")
        if not rows:
            parts.append(f"{head}: empty" if not n_rows else f"{head}: not read ({n(n_rows, 'row')}; the file's text limit was reached)")
            continue
        cols = sorted({c for r in rows.values() for c in r})
        lo, hi = cols[0], cols[-1]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["row"] + [col_name(c) for c in range(lo, hi + 1)])
        for r in sorted(rows):
            w.writerow([r] + [rows[r].get(c, "") for c in range(lo, hi + 1)])
        note = f"\n(truncated after {n(read_n, 'row')} of {n_rows:,}: the rest did not fit)" if read_n < n_rows else ""
        parts.append(f"{head}: {n(n_rows, 'row')}, columns {col_name(lo)}-{col_name(hi)}, as CSV with the sheet's row numbers\n"
                     f"{buf.getvalue().rstrip()}{note}")
    detail = f"{n(len(sheet_rows), 'sheet')} · {n(rows_total, 'row')}" + (f" · first {rows_read:,} read" if rows_read < rows_total else "")
    return Doc("\n\n".join(parts), detail, kind, rows_read < rows_total)


# ---------------------------------------------------------------- Word
def para_text(p):
    out = []
    for e in p.iter():
        k = local(e.tag)
        if k == "t" and e.text:
            out.append(e.text)
        elif k == "tab":
            out.append("\t")
        elif k in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def docx(p, main, limit):
    root = p.tree(main)
    body = next((e for e in root if local(e.tag) == "body"), root)
    out, used, n_par, n_tab, words, cut = [], 0, 0, 0, 0, False

    def add(s):
        nonlocal used, cut
        if cut:
            return
        if used + len(s) > limit:
            cut = True
            return
        out.append(s)
        used += len(s) + 2

    def walk(parent):
        nonlocal n_par, n_tab, words
        for e in parent:
            k = local(e.tag)
            if k == "p":
                t = para_text(e).strip()
                if not t:
                    continue
                n_par += 1
                words += len(t.split())
                style = next((attr(s, "val") or "" for s in e.iter() if local(s.tag) == "pStyle"), "")
                lvl = re.match(r"(?i)heading\s*(\d)", style)
                pre = "#" * min(6, int(lvl.group(1))) + " " if lvl else "# " if style.lower() == "title" else \
                    "- " if any(local(x.tag) == "numPr" for x in e.iter()) else ""
                add(pre + t)
            elif k == "tbl":
                n_tab += 1
                rows = []
                for tr in (x for x in e if local(x.tag) == "tr"):
                    cells = [" ".join(para_text(q).strip() for q in tc.iter() if local(q.tag) == "p").strip().replace("|", "\\|").replace("\n", " ")
                             for tc in tr if local(tc.tag) == "tc"]
                    rows.append(cells)
                    words += sum(len(c.split()) for c in cells)
                if rows:
                    width = max(len(r) for r in rows)
                    rows = [r + [""] * (width - len(r)) for r in rows]
                    md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width] + ["| " + " | ".join(r) + " |" for r in rows[1:]]
                    add("\n".join(md))
            elif k in ("sdt", "sdtContent", "customXml", "smartTag", "ins"):
                walk(e)
    walk(body)
    text = "\n\n".join(out) + ("\n\n(truncated here: the rest of the document did not fit)" if cut else "")
    detail = f"{n(words, 'word')}" + (f" · {n(n_tab, 'table')}" if n_tab else "") + (" · first part read" if cut else "")
    return Doc(text, detail, "docx", cut)


# ---------------------------------------------------------------- PowerPoint
def pptx(p, main, limit):
    root = p.tree(main)
    rels = p.rels(main)
    order = [rels.get(rid(e), ("", None))[1] for e in root.iter() if local(e.tag) == "sldId"]
    out, used, cut, n_slides = [], 0, False, 0
    for i, part in enumerate(order, 1):
        if not part or part not in p.names:
            continue
        n_slides += 1
        if cut:
            continue
        paras = []
        for e in p.tree(part).iter():
            if local(e.tag) == "p":
                t = "".join((x.text or "") if local(x.tag) == "t" else "\n" if local(x.tag) == "br" else "" for x in e.iter()).strip()
                if t:
                    paras.append(t)
        s = f"## Slide {i}\n" + ("\n".join(paras) if paras else "(no text)")
        if used + len(s) > limit:
            cut = True
            continue
        out.append(s)
        used += len(s) + 2
    read_n = len(out)
    text = "\n\n".join(out) + (f"\n\n(truncated after {n(read_n, 'slide')} of {n_slides}: the rest did not fit)" if cut else "")
    detail = n(n_slides, "slide") + (f" · first {read_n} read" if cut else "")
    return Doc(text, detail, "pptx", cut)
