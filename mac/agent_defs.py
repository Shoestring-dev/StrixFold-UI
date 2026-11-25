"""Agents' definitions: one YAML file per agent in the private store (<store>/halo-agents/defs/<name>.yaml), read
and checked here.

The YAML reader is a small strict subset, enough for a definition (Python 3.9's standard library has none): block
mappings and sequences, flow mappings and sequences ({a: 1}, ["*"]), quoted and plain scalars, | and > text blocks
and # comments. Anything else (anchors, tags, several documents, tabs in the indentation, a key given twice) is
refused with its line number. A definition that does not check out is shown with its error and cannot start a run.
"""
import os
import re

RULES = ("allow", "ask", "deny", "off")
TOOLS = ("web_search", "web_read", "browser", "vision", "files", "shell")
PHASE_1B = ("browser", "vision")  # not in phase 1a: off or deny only
LOOPS = ("research",)

DEFAULT_NAME = "web-research"
DEFAULT_YAML = """\
# Agents: the web research agent (spec v4, section 1). Edit with care: a definition that does not check out
# cannot start a run. Tool rules: allow, ask (the user approves on the phone or the Mac), deny or off.
name: web-research
label: Research
purpose: "Research a question on the web and write a cited report."
loop: research
model: loaded             # whichever chat model is loaded; a run never loads or swaps a model
effort: low               # no thinking: thinking tokens would eat the reply cap
request_cap: { prompt_tokens: 8192, reply_tokens: 700 }   # every model request
input: question_only      # the run never sees the user's chats; no attachments in phase 1a
tools:
  web_search: allow       # SearXNG on this Mac
  web_read: allow         # GET only, through the egress guard
  browser: off            # phase 1b
  vision: off             # phase 1b
  files: allow            # its own run workspace only
  shell: deny
network:
  allow: ["*"]            # public internet only; the fixed deny list always applies
budget: { max_active_minutes: 20, max_wall_minutes: 120, max_tool_calls: 120, max_model_tokens: 400000 }
approvals: phone          # or yolo: no approvals (shape-only asks allowed and logged, leak-shaped ones skipped)
output: report.md
"""


class YAMLError(ValueError):
    pass


# ---------------------------------------------------------------- the YAML subset
def _strip_comment(line):
    """The line without a # comment (a # at the start or after a space, outside quotes)."""
    q = None
    for i, c in enumerate(line):
        if q:
            if c == q:
                if q == "'" and line[i + 1:i + 2] == "'":
                    continue
                q = None
            elif c == "\\" and q == '"':
                continue
        elif c in "\"'" and (i == 0 or line[i - 1] in " \t[{,:"):
            q = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(s, n):
    s = s.strip()
    if not s:
        return None
    if s[0] == '"':
        if len(s) < 2 or s[-1] != '"':
            raise YAMLError(f"line {n}: unclosed quote")
        body, out, i = s[1:-1], [], 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}.get(body[i + 1], "\\" + body[i + 1]))
                i += 2
                continue
            if c == '"':
                raise YAMLError(f"line {n}: a quote inside a quoted value (write \\\")")
            out.append(c)
            i += 1
        return "".join(out)
    if s[0] == "'":
        if len(s) < 2 or s[-1] != "'":
            raise YAMLError(f"line {n}: unclosed quote")
        return s[1:-1].replace("''", "'")
    if s[0] in "&*!|>%@`":
        raise YAMLError(f"line {n}: {s[0]!r} (anchors, tags and the like) is not supported here")
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"[-+]?\d+", s.replace("_", "")):
        return int(s.replace("_", ""))
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", s):
        return float(s)
    return s


def _flow(s, n):
    """A flow collection ({...} or [...]) on one line."""
    pos = [0]

    def ws():
        while pos[0] < len(s) and s[pos[0]] in " \t":
            pos[0] += 1

    def token(stop):
        ws()
        start = pos[0]
        if pos[0] < len(s) and s[pos[0]] in "\"'":
            q = s[pos[0]]
            pos[0] += 1
            while pos[0] < len(s):
                if s[pos[0]] == "\\" and q == '"':
                    pos[0] += 2
                    continue
                if s[pos[0]] == q:
                    if q == "'" and s[pos[0] + 1:pos[0] + 2] == "'":
                        pos[0] += 2
                        continue
                    break
                pos[0] += 1
            pos[0] += 1
            return _scalar(s[start:pos[0]], n)
        while pos[0] < len(s) and s[pos[0]] not in stop:
            pos[0] += 1
        return _scalar(s[start:pos[0]], n)

    def value(stop):
        ws()
        if pos[0] < len(s) and s[pos[0]] in "[{":
            return coll()
        return token(stop)

    def coll():
        open_ = s[pos[0]]
        close = "]" if open_ == "[" else "}"
        pos[0] += 1
        out = [] if open_ == "[" else {}
        while True:
            ws()
            if pos[0] >= len(s):
                raise YAMLError(f"line {n}: {open_} is not closed")
            if s[pos[0]] == close:
                pos[0] += 1
                return out
            if isinstance(out, list):
                out.append(value(",]"))
            else:
                k = token(":,}")
                ws()
                if pos[0] >= len(s) or s[pos[0]] != ":":
                    raise YAMLError(f"line {n}: a key without a value in {{...}}")
                pos[0] += 1
                if not isinstance(k, str) or not k:
                    raise YAMLError(f"line {n}: keys are names")
                if k in out:
                    raise YAMLError(f"line {n}: {k} is given twice")
                out[k] = value(",}")
            ws()
            if pos[0] < len(s) and s[pos[0]] == ",":
                pos[0] += 1
            elif pos[0] < len(s) and s[pos[0]] != close:
                raise YAMLError(f"line {n}: expected , or {close}")

    v = coll()
    ws()
    if pos[0] != len(s):
        raise YAMLError(f"line {n}: text after the closing bracket")
    return v


def _value(s, n):
    s = s.strip()
    if s[:1] in "[{":
        return _flow(s, n)
    return _scalar(s, n)


def parse_yaml(text):
    """The subset described above, as Python dicts, lists and scalars. Raises YAMLError."""
    if "\t" in "".join(l[:len(l) - len(l.lstrip())] for l in text.splitlines()):
        raise YAMLError("tabs in the indentation: use spaces")
    raw = text.splitlines()
    lines = []  # (number, indent, content) of the lines that carry something; text blocks keep theirs raw
    i = 0
    while i < len(raw):
        line = raw[i]
        if line.strip() in ("---", "...") and not lines:
            i += 1
            continue
        if line.strip() in ("---", "..."):
            raise YAMLError(f"line {i + 1}: one document per file")
        body = _strip_comment(line)
        if body.strip():
            lines.append((i + 1, len(body) - len(body.lstrip()), body.strip(), raw))
        i += 1
    pos = [0]

    def block(indent):
        n, ind, content, _ = lines[pos[0]]
        if content.startswith("- ") or content == "-":
            return seq(ind)
        return mapping(ind)

    def text_block(style, ind, n):
        # the raw lines after line n that are indented more than ind (blank lines kept)
        out, j = [], n  # raw index of the next line (n is 1-based)
        inner = None
        while j < len(raw):
            l = raw[j]
            if l.strip() and (len(l) - len(l.lstrip())) <= ind:
                break
            if l.strip():
                inner = inner if inner is not None else len(l) - len(l.lstrip())
            out.append(l)
            j += 1
        while pos[0] < len(lines) and lines[pos[0]][0] <= j:
            pos[0] += 1
        body = [l[inner:] if inner is not None else "" for l in out]
        while body and not body[-1].strip():
            body.pop()
        return "\n".join(body) + "\n" if style == "|" else re.sub(r"(?<!\n)\n(?!\n)", " ", "\n".join(body)).strip() + "\n"

    def item_value(rest, ind, n):
        rest = rest.strip()
        if rest in ("|", ">", "|-", ">-"):
            v = text_block(rest[0], ind, n)
            return v.rstrip("\n") if rest.endswith("-") else v
        if rest:
            pos[0] += 1
            return _value(rest, n)
        pos[0] += 1
        if pos[0] < len(lines) and lines[pos[0]][1] > ind:
            return block(lines[pos[0]][1])
        return None

    def mapping(ind):
        out = {}
        while pos[0] < len(lines):
            n, i2, content, _ = lines[pos[0]]
            if i2 < ind:
                break
            if i2 > ind:
                raise YAMLError(f"line {n}: unexpected indentation")
            if content.startswith("- "):
                raise YAMLError(f"line {n}: a list item where a key was expected")
            m = re.match(r"^(\"[^\"]*\"|'[^']*'|[^:#\[\]{},]+?)\s*:(\s+|$)(.*)$", content)
            if not m:
                raise YAMLError(f"line {n}: expected key: value")
            k = _scalar(m.group(1), n)
            if not isinstance(k, str) or not k:
                raise YAMLError(f"line {n}: keys are names")
            if k in out:
                raise YAMLError(f"line {n}: {k} is given twice")
            out[k] = item_value(m.group(3), ind, n)
        return out

    def seq(ind):
        out = []
        while pos[0] < len(lines):
            n, i2, content, _ = lines[pos[0]]
            if i2 < ind:
                break
            if i2 > ind or not (content.startswith("- ") or content == "-"):
                raise YAMLError(f"line {n}: expected a list item")
            rest = content[1:].strip()
            if re.match(r"^[^\"'\[{][^:]*:(\s|$)", rest):
                raise YAMLError(f"line {n}: mappings inside list items are not supported here; use {{...}}")
            out.append(item_value(rest, ind, n))
        return out

    if not lines:
        return {}
    v = block(lines[0][1])
    if pos[0] < len(lines):
        raise YAMLError(f"line {lines[pos[0]][0]}: unexpected indentation")
    return v


# ---------------------------------------------------------------- the definition
BUDGET_LIMITS = {"max_active_minutes": (1, 120), "max_wall_minutes": (1, 24 * 60), "max_tool_calls": (1, 500),
                 "max_model_tokens": (1000, 2_000_000)}
CAP_LIMITS = {"prompt_tokens": (1024, 8192), "reply_tokens": (64, 700)}
KEYS = {"name", "label", "purpose", "loop", "model", "effort", "request_cap", "input", "tools", "network", "budget",
        "approvals", "output"}


def check(d):
    """A parsed definition, checked and normalised; ValueError with every problem, in plain words."""
    if not isinstance(d, dict):
        raise ValueError("a definition is a set of key: value lines")
    errs = []
    extra = sorted(set(d) - KEYS)
    if extra:
        errs.append(f"unknown key{'s' if len(extra) > 1 else ''}: {', '.join(extra)}")
    out = {}
    name = d.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
        errs.append("name: lower-case letters, digits and dashes, up to 40")
    out["name"] = name
    out["label"] = str(d.get("label") or name or "")[:40]
    out["purpose"] = str(d.get("purpose") or "")[:300]
    out["loop"] = d.get("loop") or "research"
    if out["loop"] not in LOOPS:
        errs.append("loop: research (the one loop in phase 1a)")
    if d.get("model", "loaded") != "loaded":
        errs.append("model: loaded (a run uses the chat model that is loaded and never loads one)")
    out["model"] = "loaded"
    if d.get("effort", "low") != "low":
        errs.append("effort: low (phase 1a runs without thinking: thinking tokens would eat the reply cap)")
    out["effort"] = "low"
    cap = d.get("request_cap") or {}
    if not isinstance(cap, dict) or set(cap) - set(CAP_LIMITS):
        errs.append("request_cap: { prompt_tokens: N, reply_tokens: N }")
        cap = {}
    out["request_cap"] = {}
    for k, (lo, hi) in CAP_LIMITS.items():
        v = cap.get(k, hi)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            errs.append(f"request_cap.{k}: a whole number {lo:,} to {hi:,}")
        out["request_cap"][k] = v
    if d.get("input", "question_only") != "question_only":
        errs.append("input: question_only (phase 1a: the question, never the user's chats; no attachments)")
    out["input"] = "question_only"
    tools = d.get("tools") or {}
    if not isinstance(tools, dict):
        errs.append("tools: one rule per tool")
        tools = {}
    bad = sorted(set(tools) - set(TOOLS))
    if bad:
        errs.append(f"tools: unknown {', '.join(bad)} (known: {', '.join(TOOLS)})")
    out["tools"] = {}
    for t in TOOLS:
        r = tools.get(t, "off")
        if r is False:
            r = "off"  # YAML's plain off
        if r not in RULES:
            errs.append(f"tools.{t}: allow, ask, deny or off")
        elif t in PHASE_1B and r not in ("off", "deny"):
            errs.append(f"tools.{t}: off (phase 1b)")
        elif t == "shell" and r not in ("off", "deny"):
            errs.append("tools.shell: deny or off (no shell in phase 1a)")
        out["tools"][t] = r
    if out["tools"].get("web_read") in ("deny", "off") and out["tools"].get("web_search") in ("deny", "off"):
        errs.append("tools: a research agent needs web_search or web_read")
    net = d.get("network") or {}
    if not isinstance(net, dict) or set(net) - {"allow", "deny"}:
        errs.append("network: { allow: [...], deny: [...] }")
        net = {}
    out["network"] = {}
    for k, dflt in (("allow", ["*"]), ("deny", [])):
        v = net.get(k, dflt)
        if not isinstance(v, list) or not all(isinstance(x, str) and re.fullmatch(r"(\*|(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+)", x.lower())
                                              for x in v):
            errs.append(f"network.{k}: a list of host names, *.domain patterns or \"*\"")
            v = dflt
        out["network"][k] = [x.lower() for x in v]
    bud = d.get("budget") or {}
    if not isinstance(bud, dict) or set(bud) - set(BUDGET_LIMITS):
        errs.append(f"budget: {', '.join(BUDGET_LIMITS)}")
        bud = {}
    out["budget"] = {}
    for k, (lo, hi) in BUDGET_LIMITS.items():
        v = bud.get(k, {"max_active_minutes": 20, "max_wall_minutes": 120, "max_tool_calls": 120, "max_model_tokens": 400000}[k])
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            errs.append(f"budget.{k}: a whole number {lo:,} to {hi:,}")
        out["budget"][k] = v
    if d.get("approvals", "phone") not in ("phone", "yolo"):
        errs.append("approvals: phone (the user approves on the phone or the Mac) or yolo (no approvals: shape-only asks are "
                    "allowed and logged, leak-shaped ones skipped; deny, network limits, budgets and Stop still apply)")
    out["approvals"] = d.get("approvals", "phone") if d.get("approvals", "phone") in ("phone", "yolo") else "phone"
    if d.get("output", "report.md") != "report.md":
        errs.append("output: report.md")
    out["output"] = "report.md"
    if errs:
        raise ValueError("; ".join(errs))
    return out


def host_allowed(host, net):
    """Whether a host passes the definition's own network lists (the guard's fixed deny list comes on top)."""
    host = (host or "").lower().rstrip(".")

    def match(p):
        return p == "*" or host == p or (p.startswith("*.") and (host == p[2:] or host.endswith(p[1:])))
    if any(match(p) for p in net.get("deny") or []):
        return False
    return any(match(p) for p in net.get("allow") or [])


class Defs:
    """The definitions folder: the default one is written on first use (600); each file is read on every list, so
    an edit shows at once."""

    def __init__(self, root):
        self.dir = os.path.join(root, "defs")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        p = os.path.join(self.dir, DEFAULT_NAME + ".yaml")
        if not os.path.exists(p):
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(DEFAULT_YAML)

    def all(self):
        out = []
        for f in sorted(os.listdir(self.dir)):
            if not f.endswith((".yaml", ".yml")) or f.startswith("."):
                continue
            out.append(self.load_file(f))
        return out

    def load_file(self, f):
        p = os.path.join(self.dir, f)
        try:
            with open(p, encoding="utf-8") as fh:
                text = fh.read(64 * 1024)
            d = check(parse_yaml(text))
            if d["name"] + os.path.splitext(f)[1] != f:
                raise ValueError(f"name: {d['name']} differs from the file name {f}")
            return {"file": f, "ok": True, "def": d, "error": None}
        except (OSError, ValueError, UnicodeDecodeError) as e:
            return {"file": f, "ok": False, "def": None, "error": str(e)[:500]}

    def get(self, name):
        for x in self.all():
            if x["ok"] and x["def"]["name"] == name:
                return x["def"]
            if not x["ok"] and os.path.splitext(x["file"])[0] == name:
                raise ValueError(f"the definition {name} does not check out: {x['error']}")
        raise KeyError(name)
