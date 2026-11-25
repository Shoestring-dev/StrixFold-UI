"""The web tools a research run calls, behind one small interface (halo_agents.Research uses only this):

  web_search(query) -> [{title, url, snippet, engine}]
  web_read(url)     -> {url (final), title, text, date, content_type, bytes, redirects}
  files_write(rel, text)                 the run's own workspace only
  strip_tracking(url)                    utm_*, fbclid, gclid, mc_*, ref and the like removed
  url_decision(url, notes, allowed, seen), query_decision(q, notes, allowed)   Allow / Ask / Deny
  Denied, Failed                         a denied-network attempt (counted, alerts) / an ordinary failure (logged)

The real tools are agents_web (egress guard, SearXNG client, the sandboxed parser, the files tool, and the
one copy of the URL and query rules: url_decision / query_decision). Under --fake and in the tests the
Stub serves fixed search results and pages from tests/fixtures/agents (no network at all), including a page with
injected instructions for the injection test; that stub is never used by the live app (make_factory refuses it
unless the app is fake).
"""
import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(os.path.dirname(HERE), "tests", "fixtures", "agents")
TRACKING = re.compile(r"^(utm_[a-z_]+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|mc_[a-z]+|ref|ref_src|ref_url|igshid|yclid|_hsenc|_hsmi|"
                      r"spm|scid|cmpid|campaign_id|ad_id|si|vero_id|oly_[a-z]+|wt_mc|s_cid)$", re.I)


from agents_web import Denied  # noqa: E402  (agents_web's guard refusal: one class, its decision holds the rule)
from agents_web.decision import Deny  # noqa: E402


class Failed(Exception):
    """An ordinary failure (timeout, HTTP error, too large, not text): the run notes it and moves on."""


def strip_tracking(url):
    try:
        u = urlparse(str(url).strip())
    except ValueError:
        return str(url)
    if u.scheme not in ("http", "https"):
        return str(url).strip()
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urlunparse((u.scheme, u.netloc, u.path or "/", u.params, urlencode(q, doseq=True), ""))


class Files:
    """The run's workspace: relative paths only, no links out, 700/600 (the files tool replaces this)."""

    def __init__(self, root):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, mode=0o700, exist_ok=True)

    def path(self, rel):
        rel = str(rel or "")
        if not rel or os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
            raise Failed("a relative path inside the workspace")
        full = os.path.join(self.root, rel)
        d = os.path.realpath(os.path.dirname(full))
        if d != self.root and not d.startswith(self.root + os.sep):
            raise Failed("outside the workspace")
        if os.path.islink(full):
            raise Failed("a link")
        return full

    def write(self, rel, text):
        import halo_agents
        halo_agents.write_private(self.path(rel), text)


class Stub:
    """Fixed results and pages (tests/fixtures/agents/web.json): the fake backend and the tests only. No network: but
    the same rules as the live tools: agents_web's URL and query decisions (agents_web.exfil), its static deny list
    (check_static) and the definition's network lists on results and reads."""
    Denied, Failed = Denied, Failed

    def __init__(self, workspace, fixtures=None, web=None, network=None):
        self.files = Files(workspace)
        self.network = network or {"allow": ["*"], "deny": []}
        self.last_health = {}
        if web is None:
            with open(os.path.join(fixtures or FIXTURES, "web.json"), encoding="utf-8") as f:
                web = json.load(f)
        self.web = web
        self.calls = []

    def strip_tracking(self, url):
        from agents_web import clean_url
        try:
            return clean_url(url)
        except Denied:
            return str(url or "").strip()

    def allowed(self, url):
        import agent_defs
        return agent_defs.host_allowed((urlparse(url).hostname or "").lower(), self.network)

    def web_search(self, query, domains=None):
        self.calls.append(("web_search", query))
        res = self.web.get("searches", {}).get(query.lower()) or self.web.get("search_default") or []
        self.last_health = dict(self.web.get("health") or {"fake": "ok"})
        return [dict(r, engines=["fake"]) for r in res if self.allowed(str(r.get("url") or ""))]

    def web_read(self, url):
        from agents_web import check_static
        self.calls.append(("web_read", url))
        host = (urlparse(url).hostname or "").lower()
        d = check_static(url)
        if d.kind == "deny":
            raise Denied(d, url)
        if not self.allowed(url):
            raise Denied(Deny("not_allowed", "The address is outside this agent's network list."), url)
        if host in self.web.get("denied_hosts", []):
            raise Denied(Deny("address", "The address resolves to a private network."), url)
        p = self.web.get("pages", {}).get(url)
        if p is None:
            raise Failed("HTTP 404")
        return {"url": p.get("final_url") or url, "title": p.get("title") or "", "text": p.get("text") or "",
                "date": p.get("date"), "content_type": "text/html", "bytes": len((p.get("text") or "").encode()), "redirects": []}

    def files_write(self, rel, text):
        self.files.write(rel, text)

    def url_decision(self, url, corpus="", allowed="", seen=()):
        from agents_web import exfil
        return exfil.check_url(url, notes=corpus, question=allowed, seen=seen)

    def query_decision(self, query, corpus="", allowed=""):
        from agents_web import exfil
        return exfil.check_query(query, notes=corpus, question=allowed)

    def close(self):
        pass


def make_factory(app):
    """The tools for a run, from its trusted record: agents_web's real ones (the guard with fake=False, the definition's
    network lists on every hop and on results); the stub only on the fake backend."""
    if getattr(app, "fake", False):
        return lambda ws, task=None: Stub(ws, network=(task or {}).get("network"))
    import agents_web
    return lambda ws, task=None: agents_web.Tools(ws, network=(task or {}).get("network"))


def shutdown():
    """At app stop: the SearXNG child (one for every run) stops."""
    try:
        import agents_web
    except ImportError:
        return
    agents_web.shutdown()
