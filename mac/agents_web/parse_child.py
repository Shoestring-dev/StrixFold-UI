"""The page parser for web_read. It runs only inside its Seatbelt sandbox (profiles/parser.sb, started by read.Parser),
from the parser venv: trafilatura and lxml turn one page into Markdown with its title, date and links; pypdf reads
the text layer of a PDF (at most 300 pages).

stdin: one JSON line {"url", "content_type", "charset"}, then the page's bytes. stdout: one JSON object
{"title", "date", "markdown", "links", "extracted"} or {"error"}. `--probe` checks the sandbox is really on (a socket
and a read outside the allowed paths must both fail) and prints {"probe": "ok"}. It refuses to parse unless it is
sandboxed.
"""
import ctypes
import json
import os
import socket
import sys

MAX_MARKDOWN = 4 * 1024 * 1024
MAX_LINKS = 100
MAX_PDF_PAGES = 300
CPU_S = 15
TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown", "text/csv", "application/json", "text/json",
              "application/xml", "text/xml", "application/rss+xml", "application/atom+xml")


def sandboxed():
    """True when the kernel says this process is in a sandbox (sandbox_check with no operation)."""
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.sandbox_check.restype = ctypes.c_int
        lib.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        return lib.sandbox_check(os.getpid(), None, 0) == 1
    except (OSError, AttributeError):
        return False


def probe():
    out = {"sandboxed": sandboxed()}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("1.1.1.1", 443))
        out["socket"] = "open"
    except OSError:
        out["socket"] = "denied"
    try:
        with open(os.environ.get("PROBE_FILE") or "/nonexistent", "rb") as f:  # a canary the parent made outside
            f.read(1)                                                       # the allowed paths
        out["read"] = "open"
    except FileNotFoundError:
        out["read"] = "missing"
    except OSError:
        out["read"] = "denied"
    import resource
    cpu, files = resource.getrlimit(resource.RLIMIT_CPU)[1], resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    out["limits"] = "set" if (cpu, files) == (CPU_S, 64) else "missing"
    out["probe"] = "ok" if out == {"sandboxed": True, "socket": "denied", "read": "denied", "limits": "set"} else "failed"
    return out


def decode(body, charset):
    for enc in ([charset] if charset else []) + ["utf-8"]:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def links_of(tree, base):
    from urllib.parse import urljoin, urlsplit
    out, seen = [], set()
    for a in tree.iter("a"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        try:
            u = urljoin(base, href).split("#", 1)[0]
        except ValueError:
            continue
        if urlsplit(u).scheme not in ("http", "https") or u in seen or len(u) > 2000:
            continue
        seen.add(u)
        text = " ".join("".join(a.itertext()).split())[:200]
        out.append({"text": text, "url": u})
        if len(out) >= MAX_LINKS:
            break
    return out


def pdf_date(value):
    v = str(value or "")
    if v.startswith("D:"):
        v = v[2:]
    return "%s-%s-%s" % (v[0:4], v[4:6], v[6:8]) if len(v) >= 8 and v[:8].isdigit() else None


def parse_pdf(body):
    import io
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(body))
    if reader.is_encrypted:
        return {"error": "The PDF is encrypted."}
    meta = reader.metadata or {}
    pages, size = [], 0
    for i, page in enumerate(reader.pages):
        if i >= MAX_PDF_PAGES or size > MAX_MARKDOWN:
            pages.append("(The PDF has more pages; the first %d were read.)" % i)
            break
        text = (page.extract_text() or "").strip()
        if text:
            pages.append("## Page %d\n\n%s" % (i + 1, text))
            size += len(text)
    md = "\n\n".join(pages)[:MAX_MARKDOWN]
    if not md:
        return {"error": "The PDF has no text layer."}
    title = str(meta.get("/Title") or "").strip() or next((ln.strip() for ln in md.split("\n")[2:] if ln.strip()), "")
    return {"title": title[:500], "date": pdf_date(meta.get("/CreationDate")), "markdown": md, "links": [],
            "extracted": "pdf"}


def parse(head, body):
    ctype = (head.get("content_type") or "").lower()
    url = head.get("url") or ""
    if ctype == "application/pdf":
        return parse_pdf(body)
    if ctype in TEXT_TYPES or (ctype.startswith("text/") and ctype not in ("text/html",)):
        return {"title": "", "date": None, "markdown": decode(body, head.get("charset"))[:MAX_MARKDOWN], "links": [],
                "extracted": "text"}
    import trafilatura
    from trafilatura.utils import load_html
    html = decode(body, head.get("charset")) if head.get("charset") else body
    tree = load_html(html)
    if tree is None:
        return {"error": "The page has no readable HTML."}
    links = links_of(tree, url)
    meta = trafilatura.extract_metadata(tree, default_url=url)
    md = trafilatura.extract(tree, url=url, output_format="markdown", include_tables=True, include_formatting=True,
                             include_links=False, include_images=False, include_comments=False, with_metadata=False,
                             favor_recall=True)
    how = "article"
    if not md:
        md, how = trafilatura.html2txt(load_html(html)) or "", "fallback"
    return {"title": (meta.title if meta and meta.title else "")[:500], "date": meta.date if meta else None,
            "markdown": md[:MAX_MARKDOWN], "links": links, "extracted": how}


def limit():
    """Hard limits, set before any input is read: CPU seconds, file size, open files, no core dumps. (Memory and wall
    time are watched by the parent, which kills the child.)"""
    import resource
    for r, v in ((resource.RLIMIT_CPU, CPU_S), (resource.RLIMIT_FSIZE, 64 << 20), (resource.RLIMIT_NOFILE, 64),
                 (resource.RLIMIT_CORE, 0)):
        resource.setrlimit(r, (v, v))


def main():
    limit()
    if "--probe" in sys.argv:
        print(json.dumps(probe()))
        return
    if not sandboxed():
        print(json.dumps({"error": "The parser runs only inside its sandbox."}))
        sys.exit(3)
    data = sys.stdin.buffer.read()
    line, _, body = data.partition(b"\n")
    try:
        head = json.loads(line)
        out = parse(head, body)
    except Exception as e:  # any failure is reported, never raised past the sandbox
        out = {"error": "The page could not be parsed (%s)." % type(e).__name__}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
