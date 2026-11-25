"""Any file for StrixFold UI's chat, read as text on this Mac, best effort. Nothing is refused: what cannot be read is
attached as its name, type, size and sha256, and the chip says so.

  text and code    UTF-8/16/32 by their byte-order mark, then UTF-8, then Windows-1252 or Mac Roman (Excel's "CSV" on
                   Windows and on a Mac), the likelier of the two by its letters; kept as UTF-8
  .xlsx .docx .pptx  officetext.py (the standard library: sheets as CSV tables, paragraphs, slides)
  PDF              the text layer (PDFKit), scanned pages by on-device OCR (Vision): halo-extract, a small Swift helper
                   built once from mac/halo_extract.swift with Xcode's swiftc
  images           on-device OCR (Vision), with the image's size; the image itself is kept for a model that sees
  .doc .rtf .odt   macOS textutil; .html by the standard library's parser (no fetching of anything it links)
  .zip             its listing, and the text, CSV and Office files inside (caps on members, bytes and ratio)
  anything else    name, type, size, sha256

Nothing from a file is executed; the helpers get a timeout; nothing here opens a network connection. read() returns
Doc(text, detail, kind, truncated) with text at most `limit` characters (a visible note where it was cut)."""
import hashlib
import html.parser
import io
import json
import os
import posixpath
import re
import subprocess
import tempfile
import threading
import zipfile

import officetext

HERE = os.path.dirname(os.path.abspath(__file__))
SWIFT = os.path.join(HERE, "halo_extract.swift")
FRAMEWORKS = ["PDFKit", "Vision", "AppKit"]
BIN_DIR = os.path.expanduser("~/Library/Application Support/StrixFoldUI/bin")
MAX_PDF_PAGES, MAX_OCR_PAGES = 300, 30
ZIP_MAX_MEMBERS, ZIP_MAX_BYTES, ZIP_MAX_RATIO, ZIP_MEMBER_BYTES = 2000, 32 << 20, 200, 8 << 20
OFFICE_VENV = os.path.expanduser("~/Library/Application Support/StrixFoldUI/office-venv")  # tools/setup_office_reader.sh
XLS_READER = os.path.join(HERE, "xls_reader.py")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".avif"}
TEXTUTIL_EXT = {".doc", ".rtf", ".odt", ".wordml", ".webarchive"}
_build = threading.Lock()


class Doc(officetext.Doc):
    pass


def size_words(n):
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}".replace(".0 ", " ")
    return f"{n} bytes"


# ---------------------------------------------------------------- text in any common encoding
ODD = set("√∫≈◊‡·¸˛ˇΩπ∂ƒﬁﬂŸ¬∆˚¯˘˙˝†‰ı¨ˆ˜‹›Œœ¦¤¢¥§©®ª«»¯±²³µ¶¹º¼½¾×÷")  # rare in prose: the wrong 8-bit guess shows them


def decode_text(data, name=""):
    """The bytes as text when they are a text file (no NUL, few control characters), else None."""
    if not data:
        return ""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            try:
                return sane(data.decode(enc))
            except UnicodeDecodeError:
                return None
    if b"\x00" in data[:1 << 20]:
        return None
    try:
        return sane(data.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    cands = []
    for enc in ("cp1252", "mac_roman"):
        try:
            t = data.decode(enc)
        except UnicodeDecodeError:
            continue
        high = [ch for ch in t if ord(ch) > 127]
        cands.append((sum(ch in ODD for ch in high), enc, t))
    cands.sort(key=lambda c: c[0])
    return sane(cands[0][2]) if cands else None


def sane(t):
    sample = t[:20000]
    ctl = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\n\r\t\f")
    return t if ctl <= len(sample) // 100 else None


def cap(text, limit, unit="text"):
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    cut = cut if cut > limit // 2 else limit
    return text[:cut] + f"\n\n(truncated here: the rest of this {unit} did not fit)", True


# ---------------------------------------------------------------- the Swift helper (PDFKit, Vision)
def helper():
    """halo-extract, built from its source on first use (and again when the source changes); None without swiftc."""
    src = open(SWIFT, "rb").read()
    path = os.path.join(BIN_DIR, "halo-extract-" + hashlib.sha256(src).hexdigest()[:12])
    with _build:
        if os.path.exists(path):
            return path
        os.makedirs(BIN_DIR, mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        r = subprocess.run(["/usr/bin/xcrun", "swiftc", "-O", "-o", tmp, SWIFT] + [x for f in FRAMEWORKS for x in ("-framework", f)],
                           capture_output=True, timeout=300)
        if r.returncode != 0 or not os.path.exists(tmp):
            return None
        os.replace(tmp, path)
        return path


def run_helper(args, data, suffix, timeout):
    exe = helper()
    if not exe:
        return None
    with tempfile.TemporaryDirectory(prefix="halo-read-") as d:
        os.chmod(d, 0o700)
        f = os.path.join(d, "in" + suffix)
        with open(f, "wb") as fh:
            fh.write(data)
        try:
            r = subprocess.run([exe, args[0], f] + [str(a) for a in args[1:]], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
    try:
        out = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return None if out.get("error") else out


def pdf(data, limit, run=None):
    """A PDF's text (run: another way to call the helper, e.g. a Private chat's reader, in memory)."""
    r = (run or run_helper)(["pdf", MAX_PDF_PAGES, MAX_OCR_PAGES], data, ".pdf", 600)
    if r is None:
        return None
    pages = int(r.get("pages") or 0)
    if r.get("locked"):
        return Doc("", f"{n(pages, 'page')} · password-protected: not read", "pdf")
    parts = [f"## Page {i}\n{t}" for i, t in enumerate(r.get("text") or [], 1) if (t or "").strip()]
    text, cut = cap("\n\n".join(parts), limit, "PDF")
    more = f"\n\n(only the first {r.get('read')} of {pages} pages were read)" if (r.get("read") or 0) < pages else ""
    words = len(re.sub(r"(?m)^## Page \d+$", "", text).split())
    detail = n(pages, "page") + (" · OCR" if r.get("ocr") else "") + (f" · {n(words, 'word')}" if words else " · no text found") \
        + (" · first part read" if cut or more else "")
    return Doc(text + more, detail, "pdf", cut or bool(more))


def image(data, limit, run=None):
    r = (run or run_helper)(["image"], data, ".img", 120)
    if r is None:
        return None
    text, cut = cap((r.get("text") or "").strip(), limit, "image's text")
    words = len(text.split())
    size = f"{r.get('width')}×{r.get('height')}" if r.get("width") else "image"
    return Doc(text, f"{size} · OCR · {n(words, 'word')}" if words else f"{size} · no text found", "image", cut)


# ---------------------------------------------------------------- other documents
class _Html(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append("\t")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_text(t):
    p = _Html()
    p.feed(t)
    p.close()
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))).strip()


def textutil(data, suffix):
    with tempfile.TemporaryDirectory(prefix="halo-read-") as d:
        os.chmod(d, 0o700)
        f = os.path.join(d, "in" + suffix)
        with open(f, "wb") as fh:
            fh.write(data)
        try:
            r = subprocess.run(["/usr/bin/textutil", "-convert", "txt", "-stdout", "-noload", "-nostore", f], capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 and r.stdout.strip() else None


def xls(data, limit):
    """An Excel 97-2003 workbook through xls_reader.py (xlrd, pinned, in its own venv and process); None without it."""
    py = os.path.join(OFFICE_VENV, "bin", "python")
    if not os.path.exists(py):
        return None
    with tempfile.TemporaryDirectory(prefix="halo-read-") as d:
        os.chmod(d, 0o700)
        f = os.path.join(d, "in.xls")
        with open(f, "wb") as fh:
            fh.write(data)
        try:
            r = subprocess.run([py, "-I", XLS_READER, f, str(int(limit))], capture_output=True, timeout=180)
        except subprocess.TimeoutExpired:
            return None
    try:
        out = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    if out.get("error") or not isinstance(out.get("text"), str):
        return None
    return Doc(out["text"], out.get("detail") or "", "xls", bool(out.get("truncated")))


def zip_listing(data, name, limit):
    """The archive's listing, and the readable text and Office files inside it (no nested archives), within caps."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    files = [i for i in infos if not i.is_dir()]
    lines = [f"Archive {name}: {n(len(files), 'file')}, {size_words(sum(i.file_size for i in files))} unpacked"]
    lines += [f"- {i.filename} ({size_words(i.file_size)})" for i in files[:ZIP_MAX_MEMBERS]]
    if len(files) > ZIP_MAX_MEMBERS:
        lines.append(f"- … and {len(files) - ZIP_MAX_MEMBERS:,} more")
    body, read_n, budget = [], 0, ZIP_MAX_BYTES
    for i in files[:ZIP_MAX_MEMBERS]:
        if i.file_size > ZIP_MEMBER_BYTES or i.file_size > budget or (i.compress_size and i.file_size / i.compress_size > ZIP_MAX_RATIO):
            continue
        e = posixpath.splitext(i.filename.lower())[1]
        if e in (".zip", ".jar", ".apk") or i.flag_bits & 0x1:  # nested archives and encrypted members stay closed
            continue
        try:
            with z.open(i) as f:
                raw = f.read(ZIP_MEMBER_BYTES + 1)
        except (RuntimeError, zipfile.BadZipFile, OSError, NotImplementedError, EOFError):
            continue
        budget -= len(raw)
        try:
            doc = officetext.read(raw, i.filename, limit)
        except ValueError:
            doc = None
        t = doc.text if doc else decode_text(raw, i.filename)
        if t and t.strip():
            body.append(f"## {i.filename}\n{t.strip()}")
            read_n += 1
    text, cut = cap("\n".join(lines) + ("\n\n" + "\n\n".join(body) if body else ""), limit, "archive")
    return Doc(text, f"{n(len(files), 'file')} · {read_n:,} read" + (" · first part shown" if cut else ""), "zip", cut)


def details(data, name, why="its content couldn't be read here"):
    sha = hashlib.sha256(data).hexdigest()
    kind = officetext.ext(name).lstrip(".").upper() or "unknown"
    text = f"[{name or 'A file'}: type {kind}, {size_words(len(data))}, sha256 {sha}; {why}.]"
    return Doc(text, f"{size_words(len(data))} · content not readable", "file")


def n(x, word):
    return officetext.n(x, word)


def read(data, name="", limit=officetext.MAX_CHARS, media_kind=None):
    """Text for the chat from any file (see the module's notes). media_kind: what mediainfo made of the bytes (an image,
    audio or video), when it recognised them."""
    e = officetext.ext(name)
    try:
        if media_kind == "image" or (media_kind is None and e in IMAGE_EXT):
            return image(data, limit) or details(data, name, "the text in this image couldn't be read")
        if data[:5] == b"%PDF-":
            return pdf(data, limit) or details(data, name, "this PDF couldn't be read")
        try:
            doc = officetext.read(data, name, limit)
        except officetext.Refused:
            doc = None
        if doc is not None:
            return doc
        if data[:8] == officetext.CFB and e in (".xls", ".xlt", ""):
            doc = xls(data, limit)
            if doc:
                return doc
        if e in TEXTUTIL_EXT or (data[:8] == officetext.CFB and e == ".doc") or data[:5] == b"{\\rtf":
            t = textutil(data, e or ".rtf")
            if t:
                text, cut = cap(t.strip(), limit, "document")
                return Doc(text, n(len(text.split()), "word") + (" · first part read" if cut else ""), "document", cut)
        if data[:4] == b"PK\x03\x04" and e not in (".xlsx", ".docx", ".pptx", ".xlsm"):
            z = zip_listing(data, name, limit)
            if z:
                return z
        t = decode_text(data, name)
        if t is not None:
            if e in (".html", ".htm", ".xhtml") or (e == "" and re.match(r"\s*<(!doctype html|html)", t[:200], re.I)):
                t = html_text(t)
            text, cut = cap(t, limit, "file")
            rows = sum(1 for line in text.splitlines() if line.strip())
            detail = n(rows, "row") if e in (".csv", ".tsv") else f"{len(text):,} characters"
            return Doc(text, detail + (" · first part read" if cut else ""), "text", cut)
        if data[:8] == officetext.CFB:
            return details(data, name, officetext.refusal(data, name).rstrip("."))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return details(data, name)
