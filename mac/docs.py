"""Documents for StrixFold UI's chat: the work behind chat_docs's tools and the originals studio.Store keeps, on this Mac.

Each operation is one run of halo-extract's `doc` mode (PDFKit; TextKit for a new PDF): the request and the files go in
on stdin, the answer and any file made come back on stdout. Nothing is written to disk on the way and nothing is
fetched. A Private chat's run goes through Private's own reader (private_reader: its own build of the helper, its hash
checked before every run), any other chat's through the shared one (filetext.helper).

make_pdf lays out Markdown through md_html, this module's own small converter: every character the model wrote is
escaped (no HTML of its own gets through), a link is drawn as its words in link blue (never followed, and not a live
link in the PDF), and a picture is only a photo from the same chat, as a data: URI."""
import base64
import html
import json
import re
import subprocess

DOC_MAX = 50 << 20          # an original kept beside its text, and a file the tools read or make, at most
PAPER = {"letter": (612, 792), "a4": (595, 842), "legal": (612, 1008)}  # points
MARGIN = 54                 # 3/4 inch
OP_TIMEOUT = 120
MD_MAX = 200_000            # characters of Markdown in one make_pdf
PICTURE_PIXELS = 2_000_000  # a photo in a made PDF, at most (about 1600 x 1200)
PAGE_BREAK = "HALOPAGEBREAK7f3a"  # halo_extract.swift's: TextKit starts a new page there
# what an attachment's original is kept as (its text is what the model reads; a plain text file is its own original)
KINDS = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx", ".doc": "doc", ".xls": "xls",
         ".ppt": "ppt", ".rtf": "rtf", ".odt": "odt", ".ods": "ods", ".odp": "odp", ".pages": "pages", ".numbers": "numbers",
         ".key": "key"}
MIME = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "doc": "application/msword",
        "xls": "application/vnd.ms-excel", "ppt": "application/vnd.ms-powerpoint", "rtf": "application/rtf",
        "odt": "application/vnd.oasis.opendocument.text", "ods": "application/vnd.oasis.opendocument.spreadsheet",
        "odp": "application/vnd.oasis.opendocument.presentation"}
WORDS = {"unreadable": "It isn't a PDF that can be read.", "locked": "It is password-protected, so it can't be opened here.",
         "page": "That page isn't in it.", "no pages": "No pages were picked.", "too long": "It is too long: more than 400 pages.",
         "bad request": "The request wasn't understood."}


class DocError(ValueError):
    """Plain words for the model or the page: why a document couldn't be worked on."""


def kind_of(data, name=""):
    """What an attachment's original is kept as ("pdf", "docx", ...), or None (a text file, a photo: nothing to keep)."""
    if data[:5] == b"%PDF-":
        return "pdf"
    ext = ("." + str(name or "").rsplit(".", 1)[-1].lower()) if "." in str(name or "") else ""
    k = KINDS.get(ext)
    if k in ("docx", "xlsx", "pptx", "odt", "ods", "odp", "pages", "numbers", "key"):
        return k if data[:4] == b"PK\x03\x04" else None
    if k in ("doc", "xls", "ppt"):
        return k if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" else None
    if k == "rtf":
        return k if data[:5] == b"{\\rtf" else None
    return None


def file_name(name, default="document", ext=".pdf"):
    """A made file's name: no path, no control or reserved characters, at most 80 characters, ending in ext."""
    n = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', " ", str(name or ""))
    n = re.sub(r"\s+", " ", n).strip(" .")
    if n.lower().endswith(ext):
        n = n[:-len(ext)].rstrip()
    return (n[:76].rstrip() or default) + ext


def stem(name):
    n = str(name or "document")
    return n.rsplit(".", 1)[0] if "." in n else n


def pages_of(spec, n, cap=None):
    """Page numbers from "3", "1-3, 7", "all", 5 or [1, 2] (1-based, in order, no repeats); DocError in words."""
    if spec is None or str(spec).strip().lower() in ("", "all"):
        got = list(range(1, n + 1))
    elif isinstance(spec, bool):
        raise DocError("Name the pages as numbers, e.g. \"1-3, 5\".")
    elif isinstance(spec, int):
        got = [spec]
    elif isinstance(spec, list):
        try:
            got = [int(x) for x in spec]
        except (TypeError, ValueError):
            raise DocError("Name the pages as numbers, e.g. \"1-3, 5\".") from None
    else:
        got = []
        for part in str(spec).replace(";", ",").split(","):
            part = part.strip()
            m = re.fullmatch(r"(\d+)\s*(?:-|–|to)\s*(\d+|end)", part)
            if m:
                a, b = int(m.group(1)), n if m.group(2) == "end" else int(m.group(2))
                if b < a:
                    raise DocError(f"“{part}” runs backwards.")
                got += range(a, b + 1)
            elif part.isdigit():
                got.append(int(part))
            elif part:
                raise DocError(f"“{part}” isn't a page or a range of pages (e.g. \"1-3, 5\").")
    bad = [p for p in got if not 1 <= p <= n]
    if bad:
        raise DocError(f"Page {bad[0]} isn't in it: it has {n} page{'' if n == 1 else 's'}.")
    got = list(dict.fromkeys(got))
    if not got:
        raise DocError(WORDS["no pages"])
    if cap and len(got) > cap:
        raise DocError(f"That's {len(got)} pages; ask for at most {cap} at a time.")
    return got


# ---------------------------------------------------------------- the helper
def _frame(req, files):
    files = [bytes(f) for f in files]
    return json.dumps(dict(req, sizes=[len(f) for f in files])).encode() + b"\n" + b"".join(files)


def run(req, files=(), private=False, timeout=OP_TIMEOUT):
    """One `doc` request: its answer, with "pdf" and "jpeg" as bytes. DocError in words when it can't be done."""
    data = _frame(req, files)
    if private:
        import private_reader
        try:
            out = private_reader.reader().run("doc", data, timeout=timeout, errors=True)
        except private_reader.ReaderError as e:
            raise DocError(str(e)) from None
    else:
        import filetext
        exe = filetext.helper()
        if not exe:
            raise DocError("The document tools need Xcode's swiftc on this Mac.")
        try:
            r = subprocess.run([exe, "doc", "-"], input=data, capture_output=True, timeout=timeout,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        except subprocess.TimeoutExpired:
            out = None
        else:
            try:
                out = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
            except (ValueError, IndexError):
                out = None
    if not isinstance(out, dict):
        raise DocError("It couldn't be worked on here (the reader stopped or took too long).")
    if out.get("error"):
        raise DocError(WORDS.get(out["error"], str(out["error"])))
    for k in ("pdf", "jpeg"):
        if isinstance(out.get(k), str):
            out[k] = base64.b64decode(out[k])
    return out


def info(data, private=False):
    """{"pages", "locked", "sizes", "fields", "placeholder"}: a PDF at a glance."""
    return run({"op": "info"}, [data], private)


def page(data, n, side=1600, part="whole", marks=False, private=False, quality=0.85):
    """Page n as a JPEG ({"jpeg", "width", "height", "page", "pages", "text"})."""
    return run({"op": "page", "page": int(n), "side": float(side), "part": part, "marks": bool(marks), "quality": quality},
               [data], private)


def text(data, pages, private=False):
    """{"pages", "text": [each asked page's text]} (a scanned page read by OCR)."""
    return run({"op": "text", "pages": [int(p) for p in pages]}, [data], private, timeout=OP_TIMEOUT * 2)


def fields(data, private=False):
    return run({"op": "fields"}, [data], private)


def fill(data, values, flatten=False, private=False):
    return run({"op": "fill", "values": values, "flatten": bool(flatten)}, [data], private)


def write(data, items, flatten=False, private=False):
    return run({"op": "write", "items": items, "flatten": bool(flatten)}, [data], private)


def combine(datas, parts, private=False):
    return run({"op": "combine", "parts": parts}, datas, private)


def thumbnail(data, private=False):
    """Page 1 as a small JPEG, or None."""
    try:
        return page(data, 1, side=480, quality=0.8, private=private)["jpeg"]
    except DocError:
        return None


def summary(data, private=False):
    """A PDF's few words for its chip and the model ("6 pages · a form with 23 fields"), from info(); "" when it can't
    be read here."""
    try:
        i = info(data, private)
    except DocError:
        return ""
    if i.get("locked"):
        return "password-protected"
    if i.get("placeholder"):
        return "an XFA form (it can't be filled here)"
    f = int(i.get("fields") or 0)
    return f"a form with {f} field{'' if f == 1 else 's'}" if f else ""


def make_pdf(markdown, title="", paper="letter", landscape=False, images=None, private=False, fit=0):
    """A new PDF from Markdown ({"pdf", "pages"}). images: {chat photo id: (jpeg bytes, width, height)}; fit: at most
    this many pages (the type shrinks a little when it runs over; 0: as it comes)."""
    w, h = PAPER.get(str(paper or "letter").lower(), PAPER["letter"])
    if landscape:
        w, h = h, w
    body = md_html(markdown, title, images or {}, content_width=w - 2 * MARGIN)
    return run({"op": "html", "html": body, "paper": [w, h], "margin": MARGIN, "footer": True, "title": str(title or "")[:200],
                "fit": max(0, int(fit or 0))}, (), private)


# ---------------------------------------------------------------- Markdown to the HTML TextKit lays out
CSS = """
body { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; font-size: 11pt; color: #1d1d1f; }
h1 { font-size: 21pt; margin-top: 0px; margin-bottom: 10px; }
h2 { font-size: 15pt; margin-top: 18px; margin-bottom: 6px; }
h3 { font-size: 12.5pt; margin-top: 14px; margin-bottom: 4px; }
h4, h5, h6 { font-size: 11pt; margin-top: 12px; margin-bottom: 4px; }
p { margin-top: 0px; margin-bottom: 8px; }
ul, ol { margin-top: 0px; margin-bottom: 8px; }
li { margin-bottom: 3px; }
table { border-collapse: collapse; width: 100%; margin-top: 4px; margin-bottom: 12px; }
th, td { border: 0.5pt solid #b8b8bc; padding: 4px 7px; vertical-align: top; text-align: left; }
th { background-color: #f0f0f2; font-weight: bold; }
code { font-family: Menlo, monospace; font-size: 9.5pt; background-color: #f2f2f4; }
pre { font-family: Menlo, monospace; font-size: 9pt; background-color: #f5f5f7; margin-top: 0px; margin-bottom: 10px; }
blockquote { margin-top: 0px; margin-bottom: 10px; margin-left: 16px; color: #4a4a4f; font-style: italic; }
p.rule { font-size: 6pt; color: #c7c7cc; margin-top: 4px; margin-bottom: 10px; }
a { color: #0b57d0; }
.c { text-align: center; } .r { text-align: right; }
"""
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LIST = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$")
_RULE = re.compile(r"^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$")
_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
BREAKS = ("\\pagebreak", "\\newpage", "<!-- pagebreak -->", "<!-- page break -->", "<div style=\"page-break-after: always\"></div>")


def _cells(line):
    t = line.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|") and not t.endswith("\\|"):
        t = t[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", t)]


def inline(t, images=None):
    """One line's Markdown (code, pictures, links, bold, italic, struck) as HTML, the text itself escaped."""
    images = images or {}
    held = []

    def hold(s):
        held.append(s)
        return f"\x00{len(held) - 1}\x00"

    t = _CTRL.sub("", t)
    t = re.sub(r"(`+)(.+?)\1", lambda m: hold("<code>" + html.escape(m.group(2).strip(), quote=False) + "</code>"), t)

    def pic(m):
        alt, src = m.group(1).strip(), m.group(2).strip()
        p = images.get(src)
        if p:
            return hold(f'<img src="data:image/jpeg;base64,{base64.b64encode(p[0]).decode()}" width="{p[1]}" height="{p[2]}" '
                        f'alt="{html.escape(alt)}">')
        return f"[picture: {alt or src}]"

    t = re.sub(r"!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)", pic, t)
    t = html.escape(t, quote=False)

    def link(m):
        text, url = m.group(1), m.group(2)
        if re.match(r"(?i)(https?://|mailto:)", url):
            return hold(f'<a href="{url.replace(chr(34), "%22")}">{text}</a>')
        return text

    t = re.sub(r"\[([^\]]+)\]\(\s*([^)\s]+)\s*\)", link, t)
    t = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"<i>\1</i>", t)
    t = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", t)
    while "\x00" in t:
        t2 = re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], t)
        if t2 == t:
            break
        t = t2
    return t


def _blocks(lines, images):
    out, para, i = [], [], 0

    def flush():
        if para:
            text = ""
            for k, ln in enumerate(para):
                hard = ln.endswith("  ") or ln.endswith("\\")
                ln = ln.rstrip("\\ ").strip() if hard else ln.strip()
                text += inline(ln, images) + ("<br>" if hard and k < len(para) - 1 else " " if k < len(para) - 1 else "")
            out.append(f"<p>{text}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush()
            i += 1
            continue
        if s.lower() in BREAKS:
            flush()
            out.append(f"<p>{PAGE_BREAK}</p>")
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            flush()
            fence, body, i = m.group(1), [], i + 1
            while i < len(lines) and not lines[i].strip().startswith(fence[0] * len(fence)):
                body.append(_CTRL.sub("", lines[i]))
                i += 1
            i += 1
            out.append("<pre>" + html.escape("\n".join(body), quote=False) + "</pre>")
            continue
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush()
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2), images)}</h{n}>")
            i += 1
            continue
        if _RULE.match(line):
            flush()
            out.append('<p class="rule">' + "─" * 110 + "</p>")  # TextKit draws no <hr>
            i += 1
            continue
        if "|" in s and i + 1 < len(lines) and _SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush()
            head, aligns = _cells(line), []
            for c in _cells(lines[i + 1]):
                aligns.append("c" if c.startswith(":") and c.endswith(":") else "r" if c.endswith(":") else "")
            rows, i = [], i + 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(_cells(lines[i]))
                i += 1
            n = len(head)
            widths = _widths([head] + rows, n)

            def row(cells, tag):
                cells = (cells + [""] * n)[:n]
                return "<tr>" + "".join(f'<{tag} width="{widths[k]}%"{" class=" + chr(34) + aligns[k] + chr(34) if k < len(aligns) and aligns[k] else ""}>'
                                        f"{inline(c, images)}</{tag}>" for k, c in enumerate(cells)) + "</tr>"
            out.append("<table>" + row(head, "th") + "".join(row(r, "td") for r in rows) + "</table>")
            continue
        if s.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + "".join(_blocks(quote, images)) + "</blockquote>")
            continue
        if _LIST.match(line):
            flush()
            items, i = [], i
            while i < len(lines):
                m = _LIST.match(lines[i])
                if m:
                    items.append([len(m.group(1).expandtabs(4)), "ol" if m.group(2)[0].isdigit() else "ul", m.group(3)])
                    i += 1
                elif lines[i].strip() and items and (lines[i][:1] in (" ", "\t")):
                    items[-1][2] += " " + lines[i].strip()  # a wrapped item goes on
                    i += 1
                else:
                    break
            out.append(_list(items, images))
            continue
        para.append(line)
        i += 1
    flush()
    return out


def _widths(rows, n, page=504):
    """Each column's share of the table (whole percents): first room for its longest word (TextKit gave the columns
    equal shares, and "Line" broke as "Lin e"), then what is left by how much longer its text runs (up to 40 characters)."""
    plain = lambda c: re.sub(r"[*_`~]|!?\[([^\]]*)\]\([^)]*\)", r"\1", c)  # noqa: E731 - the text a cell shows
    want, floor = [], []
    for k in range(n):
        cells = [plain(r[k]) if k < len(r) else "" for r in rows]
        word = max((len(w) for c in cells for w in c.split()), default=1)
        want.append(max(word, min(40, max((len(c) for c in cells), default=1))))
        floor.append((min(word, 30) * 7.5 + 30) / page * 100)  # 11 pt bold type, up to 7.5 points a character, and the padding
    if sum(floor) >= 100:  # too many long words for the page: shares by the floors alone
        return [max(1, int(x * 100 / sum(floor))) for x in floor]
    extra = [max(0.0, w - f * page / 100 / 7.5) for w, f in zip(want, floor)]  # characters past each floor
    if not sum(extra):  # every column fits its floor: the rest by how long each runs
        extra = [float(w) for w in want]
    left, total = 100 - sum(floor), sum(extra)
    return [max(1, int(floor[k] + left * extra[k] / total)) for k in range(n)]


def _list(items, images):
    """Items [(indent, "ul" | "ol", text)] as nested lists (an item indented past the one before it is inside it)."""
    html_, stack = [], []
    for indent, kind, text in items:
        while stack and indent < stack[-1][0]:
            html_.append(f"</li></{stack.pop()[1]}>")
        if stack and indent == stack[-1][0] and kind != stack[-1][1]:
            html_.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            html_.append(f"<{kind}>")
            stack.append((indent, kind))
        else:
            html_.append("</li>")
        box = re.match(r"^\[([ xX])\]\s+(.*)$", text)
        if box:
            text = ("☑ " if box.group(1) != " " else "☐ ") + box.group(2)
        html_.append("<li>" + inline(text, images))
    while stack:
        html_.append(f"</li></{stack.pop()[1]}>")
    return "".join(html_)


def md_html(md, title="", images=None, content_width=504):
    """Markdown as a whole HTML document for TextKit. images: {chat photo id: (jpeg bytes, width, height)}, each drawn
    at most content_width points wide (TextKit lays out a CSS pixel as a point)."""
    sized = {}
    for k, (data, w, h) in (images or {}).items():
        scale = min(1.0, float(content_width) / max(1, w), 640.0 / max(1, h))  # a tall photo at most most of a page
        sized[k] = (data, max(1, int(w * scale)), max(1, int(h * scale)))
    lines = str(md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body = "".join(_blocks(lines, sized))
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + html.escape(str(title or "")) + "</title><style>"
            + CSS + "</style></head><body>" + body + "</body></html>")
