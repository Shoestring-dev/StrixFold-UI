"""The URL and query guards (spec v4 section 4): the user approves, per exact URL or query, a request shaped like it
carries data out.

web_read asks before a URL whose query string is over 200 characters, that carries a long hex or base64-looking
value, that has more than 3 parameters over 40 characters, that repeats 24+ characters of the agent's notes, or that
holds an email address or phone number. web_search asks before a query over 200 characters, with an encoded-looking
run, an email address or phone number, or 24+ characters of the notes. Search engines see the queries, so they count
as outbound data.

`notes` is what the run must not leak (notes.md; attachments when they exist). URLs inside it are left out, so
re-reading a source the notes list does not ask. `question` is the user's question: runs that are also in it do not count
as copied, so ordinary searches built from the question do not ask.

For a URL the copy check also covers the host name (a DNS lookup of <data>.evil.example leaks the data) and compares
letters and digits only, so notes turned into a slug or a subdomain still match. A URL the run has already seen in a
search result or a page's links (`seen`) skips the copy check: it was written by a third party before the agent
chose it, so it cannot carry the notes. Host names also ask on a long encoded-looking label.
"""
import re
from urllib.parse import unquote, unquote_plus, urlsplit

from . import guard
from .decision import ALLOW, Ask, Deny, Denied

MAX_QUERY_STRING = 200
MAX_SEARCH = 200
HARD_MAX_SEARCH = 1000
LONG_PARAM, MAX_LONG_PARAMS = 40, 3
COPY_RUN = 24
HEX_RUN, B64_RUN = 32, 40

URL_IN_TEXT = re.compile(r"(?:https?://|www\.)\S+", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w+])\+?\(?\d[\d ().-]{7,22}\d(?!\w)")
HEX = re.compile(r"[0-9a-fA-F]{%d,}" % HEX_RUN)
B64 = re.compile(r"[A-Za-z0-9+/=_-]{%d,}" % B64_RUN)
SEPARATORS = set("+/=_-")


def _norm(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def _norm_loose(text):
    """Lower case, with every run of other characters as one space: "Foo-Bar_baz" and "foo bar baz" match."""
    return re.sub(r"[^0-9a-z]+", " ", text.lower()).strip()


def _is_phone(m):
    s = m.group(0)
    digits = sum(c.isdigit() for c in s)
    groups = [g for g in re.split(r"[ ().-]+", s.lstrip("+")) if g]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):  # a date
        return False
    if s.startswith("+"):
        return 9 <= digits <= 15
    return 10 <= digits <= 15 and len(groups) >= 2


def has_phone(text):
    return any(_is_phone(m) for m in PHONE.finditer(text))


def has_email(text):
    return bool(EMAIL.search(text))


def has_encoded(text):
    """A long hex run (32+, digits and letters), or a long base64-looking run (40+, few separators, digits and
    letters in both cases or many digits). Slugs and wiki titles have a separator every word, so they pass."""
    for m in HEX.finditer(text):
        s = m.group(0)
        if any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
            return True
    for m in B64.finditer(text):
        s = m.group(0)
        seps = sum(c in SEPARATORS for c in s)
        digits = sum(c.isdigit() for c in s)
        upper, lower = any(c.isupper() for c in s), any(c.islower() for c in s)
        if seps <= 0.1 * len(s) and digits and (upper or lower) and ((upper and lower) or digits >= 0.15 * len(s)):
            return True
    return False


def encoded_label(label):
    """A host name label that looks like data: 20+ hex characters, or 24+ letters and digits, 10% or more digits."""
    core = label.replace("-", "")
    digits = sum(c.isdigit() for c in core)
    if len(core) >= 20 and all(c in "0123456789abcdef" for c in core) and 0 < digits < len(core):
        return True
    return len(core) >= 24 and label.count("-") <= 1 and digits >= 0.1 * len(core) and digits < len(core)


class _Copied:
    """24-character runs of the notes (URLs left out) that are not also in the question."""

    def __init__(self, notes, question, loose=False):
        self.norm = _norm_loose if loose else _norm
        n = self.norm(URL_IN_TEXT.sub(" ", notes or ""))
        q = self.norm(question or "")
        allowed = {q[i:i + COPY_RUN] for i in range(len(q) - COPY_RUN + 1)}
        self.runs = {n[i:i + COPY_RUN] for i in range(len(n) - COPY_RUN + 1)} - allowed

    def found_in(self, text):
        if not self.runs:
            return False
        t = self.norm(text)
        return any(t[i:i + COPY_RUN] in self.runs for i in range(len(t) - COPY_RUN + 1))


def _cleaned(urls):
    out = set()
    for u in urls or ():
        try:
            out.add(guard.clean_url(u))
        except Denied:
            pass
    return out


def check_url(url, *, notes="", question="", seen=()):
    """Deny (the egress guard's static rules), Ask (exfiltration-shaped, key = the cleaned URL) or Allow. `seen`: URLs
    this run got from search results or page links (they skip the copy check)."""
    try:
        clean = guard.clean_url(url)
    except Denied as e:
        return e.decision
    d = guard.check_static(clean)
    if d.kind != "allow":
        return d
    parts = urlsplit(clean)
    params = [p.split("=", 1) for p in parts.query.split("&") if p]
    values = [unquote_plus(p[1]) if len(p) > 1 else unquote_plus(p[0]) for p in params]
    path = unquote(parts.path)
    text = path + " " + " ".join(values)

    def ask(rule, reason):
        return Ask(rule, reason, clean)

    host = parts.hostname or ""
    if clean not in _cleaned(seen) and _Copied(notes, question, loose=True).found_in(
            " ".join([host, path] + values)):
        return ask("notes_copy", "The address repeats text from the agent's notes.")
    if has_email(text):
        return ask("email", "The address contains an email address.")
    if has_phone(text):
        return ask("phone", "The address contains a phone number.")
    if any(has_encoded(seg) for seg in path.split("/")) or any(has_encoded(v) for v in values) or any(
            encoded_label(lb) for lb in host.split(".")):
        return ask("encoded_value", "The address carries a long encoded value.")
    if sum(len(v) > LONG_PARAM for v in values) > MAX_LONG_PARAMS:
        return ask("long_params", "The address has more than 3 parameters over 40 characters.")
    if len(parts.query) > MAX_QUERY_STRING:
        return ask("long_query", "The address has a query string over 200 characters.")
    return ALLOW


def check_query(query, *, notes="", question=""):
    """Deny (empty or over 1,000 characters), Ask (exfiltration-shaped, key = the query exactly) or Allow."""
    q = query if isinstance(query, str) else ""
    if not q.strip():
        return Deny("empty", "The search is empty.")
    if len(q) > HARD_MAX_SEARCH:
        return Deny("too_long", "The search is over 1,000 characters.")

    def ask(rule, reason):
        return Ask(rule, reason, q)

    if _Copied(notes, question).found_in(q):
        return ask("notes_copy", "The search repeats text from the agent's notes.")
    if has_email(q):
        return ask("email", "The search contains an email address.")
    if has_phone(q):
        return ask("phone", "The search contains a phone number.")
    if has_encoded(q):
        return ask("encoded_value", "The search carries a long encoded value.")
    if len(q) > MAX_SEARCH:
        return ask("long_query", "The search is over 200 characters.")
    return ALLOW
