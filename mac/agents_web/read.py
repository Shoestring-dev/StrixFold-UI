"""web_read: fetch one page through the egress guard (GET only) and turn it into Markdown in a sandboxed parser
(web pages, PDFs with a text layer, plain text, JSON and XML).

The URL guard runs first (exfil.check_url): a Deny raises Denied, an Ask raises NeedsApproval until the user approves that
exact URL. The page is parsed by parse_child.py (trafilatura + lxml from the hash-pinned parser venv) under
profiles/parser.sb: no network, reads only its venv, Python and system libraries, writes only its own temporary
folder, with CPU, memory, output and wall-time limits. Fail closed: no sandbox-exec, a profile the sandbox rejects,
a probe that finds the sandbox not in force, or no parser venv raises ParserUnavailable, and nothing is parsed.

Also here for the harness: chunk_markdown (pieces of about 6K tokens for per-chunk notes), est_tokens,
wrap_untrusted (the <untrusted source=...> delimiters, which a page cannot close) and instruction_flags (text aimed
at an AI, for the run log).
"""
import json
import math
import os
import re
import shutil
import tempfile
import threading

from . import exfil, guard as guard_mod, sandbox
from .decision import Denied, FetchFailed, NeedsApproval, ParserUnavailable

HOME_DIR = os.path.expanduser("~/Library/Application Support/HaloAgents")
DEFAULT_PARSER_VENV = os.path.join(HOME_DIR, "parser-venv")
CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parse_child.py")
CHARS_PER_TOKEN = 3.2  # as halo_chat
HTML_TYPES = ("text/html", "application/xhtml+xml")
PDF_TYPES = ("application/pdf",)
TEXT_TYPES = ("text/plain", "text/markdown", "text/x-markdown", "text/csv", "application/json", "text/json",
              "application/xml", "text/xml", "application/rss+xml", "application/atom+xml")


class Parser:
    """The sandboxed page parser. check() runs a probe once (the sandbox must be in force: a socket and a read
    outside the allowed paths both fail); parse() refuses until it has passed."""

    def __init__(self, venv=DEFAULT_PARSER_VENV, timeout_s=20.0, mem_bytes=768 << 20):
        self.venv, self.timeout_s, self.mem_bytes = venv, timeout_s, mem_bytes
        self._ok = None
        self._lock = threading.Lock()

    def _cmd(self, tmp, argv, child=CHILD):
        """The sandbox-exec command for the child script (tests pass a probe script instead) and its environment."""
        ok, why = sandbox.available()
        if not ok:
            raise ParserUnavailable(why)
        try:
            py, runtime = sandbox.venv_runtime(self.venv)
            venv = os.path.realpath(self.venv)
            child = os.path.realpath(child)
            profile = sandbox.render("parser.sb", [venv, runtime, child, tmp])
        except sandbox.Unavailable as e:
            raise ParserUnavailable("The page parser is not installed (tools/setup_agents_tools.sh): %s" % e)
        params = {"PYTHON": py, "VENV": venv, "RUNTIME": runtime, "CHILD": child, "TMP": tmp}
        env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "TMPDIR": tmp + "/", "LC_ALL": "C.UTF-8"}
        return sandbox.command(profile, params, [os.path.join(venv, "bin", "python"), "-I", "-X", "utf8", child] + argv), env

    def _run(self, argv, stdin=b"", extra_env=None):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="halo-parse-"))
        try:
            cmd, env = self._cmd(tmp, argv)
            env.update(extra_env or {})
            return sandbox.run(cmd, stdin=stdin, timeout_s=self.timeout_s, mem_bytes=self.mem_bytes, env=env, cwd=tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def check(self):
        """(True, "") once the probe has passed in the sandbox, else (False, why). Cached after a pass."""
        with self._lock:
            if self._ok:
                ok, why = sandbox.available()
                return (True, "") if ok else (False, why)
            outside = tempfile.mkdtemp(prefix="halo-probe-")
            canary = os.path.join(outside, "canary")
            with open(canary, "w") as f:
                f.write("canary")
            try:
                rc, out, err, killed = self._run(["--probe"], extra_env={"PROBE_FILE": os.path.realpath(canary)})
            except ParserUnavailable as e:
                return False, e.reason
            finally:
                shutil.rmtree(outside, ignore_errors=True)
            try:
                res = json.loads(out.decode() or "{}")
            except ValueError:
                res = {}
            if rc != 0 or res.get("probe") != "ok":
                if "sandbox-exec" in err:
                    return False, "The sandbox refused the parser's profile."
                return False, "The parser's sandbox check failed (%s)." % (
                    ", ".join("%s %s" % kv for kv in sorted(res.items()) if kv[0] != "probe") or "no answer")
            self._ok = True
            return True, ""

    def parse(self, body, *, url, content_type, charset=None):
        ok, why = self.check()
        if not ok:
            raise ParserUnavailable(why)
        head = json.dumps({"url": url, "content_type": content_type, "charset": charset}).encode() + b"\n"
        rc, out, err, killed = self._run([], stdin=head + body)
        if killed:
            raise FetchFailed({"time": "The parser took longer than %d seconds." % self.timeout_s,
                               "memory": "The parser went over its memory limit.",
                               "output": "The parser's output was too large."}[killed], url)
        if rc != 0 and err.startswith("sandbox-exec"):
            self._ok = None
            raise ParserUnavailable("The sandbox refused the parser's profile.")
        try:
            res = json.loads(out.decode("utf-8"))
        except ValueError:
            raise FetchFailed("The page could not be parsed.", url)
        if not isinstance(res, dict):  # the child's output is treated as untrusted too
            raise FetchFailed("The page could not be parsed.", url)
        if rc != 0 or "error" in res:
            raise FetchFailed(str(res.get("error") or "The page could not be parsed.")[:200], url)
        date = res.get("date")
        links = [{"text": str(ln.get("text") or "")[:200], "url": str(ln.get("url") or "")}
                 for ln in (res.get("links") if isinstance(res.get("links"), list) else [])[:100] if isinstance(ln, dict)]
        return {"title": str(res.get("title") or "")[:500],
                "date": date if isinstance(date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None,
                "markdown": str(res.get("markdown") or ""), "links": links, "extracted": str(res.get("extracted") or "")}


_default = None


def default_parser():
    global _default
    if _default is None:
        _default = Parser()
    return _default


def _sniff(resp):
    if resp.content_type:
        return resp.content_type
    head = resp.body[:2048].lstrip().lower()
    if head.startswith(b"%pdf-"):
        return "application/pdf"
    return "text/html" if head.startswith((b"<!doctype html", b"<html")) or b"<body" in head else "text/plain"


def web_read(url, *, guard, notes="", question="", seen=(), approved=None, parser=None):
    """One page as {"url", "requested", "title", "date", "markdown", "content_type", "bytes", "links", "hops",
    "flags"}.
    `seen`: URLs this run got from search results and page links (see exfil.check_url)."""
    d = exfil.check_url(url, notes=notes, question=question, seen=seen)
    if d.kind == "deny":
        raise Denied(d, url if isinstance(url, str) else "")
    if d.kind == "ask" and approved != d.key:
        raise NeedsApproval(d)
    resp = guard.fetch(d.key if d.kind == "ask" else url)
    if resp.status >= 400:
        raise FetchFailed("The site answered HTTP %d." % resp.status, resp.url)
    ctype = _sniff(resp)
    if ctype == "application/octet-stream" and resp.body[:5] == b"%PDF-":
        ctype = "application/pdf"
    if ctype not in HTML_TYPES + TEXT_TYPES + PDF_TYPES and not ctype.startswith("text/"):
        raise FetchFailed("The page is %s; web_read reads web pages, PDFs and text only." % (
            ctype or "of no stated type"), resp.url)
    page = (parser or default_parser()).parse(resp.body, url=resp.url, content_type=ctype, charset=resp.charset)
    links = []
    for ln in page["links"]:
        try:
            u = guard_mod.clean_url(ln["url"])
        except Denied:
            continue
        if guard_mod.check_static(u).kind == "allow":
            links.append({"text": ln["text"], "url": u})
    return {"url": resp.url, "requested": resp.requested, "title": page["title"], "date": page["date"],
            "markdown": page["markdown"], "content_type": ctype, "bytes": len(resp.body), "links": links,
            "hops": list(resp.hops), "flags": instruction_flags(page["title"] + "\n" + page["markdown"])}


# ---------------------------------------------------------------- helpers for the harness
def est_tokens(text):
    return int(math.ceil(len(text or "") / CHARS_PER_TOKEN))


def _split_hard(text, max_chars):
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def chunk_markdown(markdown, max_tokens=6000):
    """Pieces of at most max_tokens (estimated), split at headings, then blank lines, then lines, then hard."""
    max_chars = max(200, int(max_tokens * CHARS_PER_TOKEN))
    text = (markdown or "").strip()
    if not text:
        return []
    sections = re.split(r"\n(?=#{1,6} )", text)
    pieces = []
    for sec in sections:
        if len(sec) <= max_chars:
            pieces.append(sec)
            continue
        for para in re.split(r"\n\s*\n", sec):
            if len(para) <= max_chars:
                pieces.append(para)
                continue
            for line in para.split("\n"):
                pieces.extend([line] if len(line) <= max_chars else _split_hard(line, max_chars))
    chunks, cur = [], ""
    for p in pieces:
        p = p.strip("\n")
        if not p:
            continue
        if cur and len(cur) + 2 + len(p) > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = cur + "\n\n" + p if cur else p
    if cur:
        chunks.append(cur)
    return chunks


_DELIM = re.compile(r"<(\s*/?\s*untrusted)", re.I)


def wrap_untrusted(source, text):
    """<untrusted source="...">text</untrusted>. Any "<untrusted" or "</untrusted" inside text (any case or spacing)
    has its "<" replaced with "‹", so a page cannot close the block or open a fake one."""
    src = re.sub(r'["<>\s]', lambda m: "%%%02X" % ord(m.group(0)), str(source or ""))
    body = _DELIM.sub(lambda m: "‹" + m.group(1), str(text or ""))
    return '<untrusted source="%s">\n%s\n</untrusted>' % (src, body)


INSTRUCTION_PATTERNS = [
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier|preceding) (?:instructions|prompts?|rules)",
    r"disregard (?:all |any )?(?:the )?(?:previous|prior|above|earlier|your) (?:instructions|prompts?|rules)",
    r"forget (?:all |any )?(?:your|the|previous) (?:instructions|rules|prompts?)",
    r"(?:new|updated|real|actual) (?:system )?instructions\s*:",
    r"system prompt",
    r"you are (?:now )?(?:an? )?(?:ai|assistant|language model|llm|chatbot|agent)\b",
    r"(?:attention|note|message|instructions?) (?:to|for) (?:the |any )?(?:ai|assistant|llm|language model|agent|chatbot)s?\b",
    r"\bif you are an? (?:ai|llm|language model|assistant|agent)\b",
    r"\b(?:ai|llm) (?:agents?|assistants?|models?),? (?:must|should|please|reading this)\b",
    r"do not (?:tell|inform|mention (?:this )?to) the user",
    r"(?:send|post|upload|exfiltrate|leak|forward) (?:your|the|all) (?:notes|conversation|context|data|history|memory|system prompt)",
    r"(?:visit|open|fetch|load|go to) (?:this|the following) (?:url|link|address|page)",
    r"<\|?(?:im_start|im_end|system|endoftext)\|?>",
    r"\[/?(?:inst|sys)\]",
    r"^\s*(?:###\s*)?(?:system|assistant)\s*:",
]
_INSTR = re.compile("|".join("(?:%s)" % p for p in INSTRUCTION_PATTERNS), re.I | re.M)


def instruction_flags(text, limit=10):
    """Short phrases in text that look like instructions aimed at an AI (for the run log, never acted on)."""
    out = []
    for m in _INSTR.finditer(text or ""):
        s = " ".join(m.group(0).split())[:80]
        if s.lower() not in (o.lower() for o in out):
            out.append(s)
        if len(out) >= limit:
            break
    return out
