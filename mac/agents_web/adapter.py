"""agents_web.Tools: the interface halo_agents.Research calls (agents_tools.Stub has the same shape), over
the guard, SearXNG, the sandboxed parser and the files tool.

  web_search(query, domains=None) -> [{title, url, snippet, engines, retrieved}]; last_health: {engine: status}
  web_read(url)     -> {url (final), title, text, date, content_type, bytes, redirects, links, flags}
  files_write(rel, text), strip_tracking(url), close()
  url_reasons(url, corpus, allowed, seen), query_reasons(query, corpus, allowed)   the spec section 4 asks
  url_decision(...), query_decision(...)   the same as Allow / Ask / Deny objects (rule codes, to_log() for logs)
  Tools.Denied      the egress guard refused (a denied-network attempt; str(e) is the reason)
  Tools.Failed      anything else (a timeout, HTTP error, too large, the parser or SearXNG unavailable, a files refusal)

The harness asks the user before exfiltration-shaped URLs and queries (url_reasons, query_reasons below, or its own) and
then calls these, so Tools does not ask again; the egress guard's rules always apply. One SearXNG serves every run:
shutdown() stops it at app stop (it also stops after 10 minutes idle, at exit, or if the app dies).
"""
import threading

from . import exfil, fetcher, files, guard, read, search
from .decision import Denied, FetchFailed, FilesDenied, ParserUnavailable, SearchUnavailable, ToolError


class Failed(ToolError):
    """setup=True: the tools are not set up (SearXNG or the parser missing, or the sandbox refused), so a run should
    stop rather than research blind. Raised from the original error's except block (__context__ is that error)."""

    def __init__(self, reason, setup=False):
        super().__init__(reason)
        self.reason, self.setup = reason, setup


_service, _lock = None, threading.Lock()


def shared_service():
    global _service
    with _lock:
        if _service is None:
            _service = search.SearxService()
        return _service


def status(check_parser=True):
    """For the Agents page; starts nothing. {"searxng": SearxService.status(), "parser": {"ready", "why"}}. The parser
    check runs its sandbox probe once (about 0.1 s), then is cached."""
    out = {"searxng": shared_service().status()}
    if check_parser:
        ok, why = read.default_parser().check()
        out["parser"] = {"ready": ok, "why": why}
    return out


def shutdown():
    with _lock:
        if _service is not None:
            _service.stop()


class Tools:
    Denied, Failed = Denied, Failed

    def __init__(self, workspace, *, fake=False, web=None, parser=None, network=None):
        """fake=True needs web, a started fakeweb.FakeWeb: the real guard with its --fake test route, the fake
        SearXNG, no internet. network: the agent definition's {"allow", "deny"} host lists, enforced by the guard on
        every hop and applied to search results (the fixed deny list always applies on top)."""
        self.ws = files.Workspace(workspace)
        self.network = network
        # the fetch runs in its own sandboxed child (fetcher.SandboxedGuard: network, no programs, no file reads)
        self.guard = fetcher.SandboxedGuard(fake=fake, network=network)
        if fake:
            if web is None:
                raise ValueError("a fake Tools needs a FakeWeb")
            web.install(self.guard)
            self.service = web.search_service()
        else:
            self.service = shared_service()
        self.parser = parser
        self.last_health = {}

    def strip_tracking(self, url):
        try:
            return guard.clean_url(url)
        except Denied:
            return str(url or "").strip()

    def web_search(self, query, domains=None):
        """Results as search.search_results gives them; each engine's health for this search in self.last_health."""
        try:
            out = search.search_results(query, service=self.service, approved=query, domains=domains,
                                        network=self.network)
            self.last_health = out["health"]
            return out["results"]
        except SearchUnavailable as e:
            raise Failed(e.reason, setup=True)
        except FetchFailed as e:
            raise Failed(e.reason)

    def web_read(self, url):
        d = exfil.check_url(url)
        if d.kind == "deny":
            raise Denied(d, str(url or ""))
        try:
            page = read.web_read(url, guard=self.guard, approved=d.key if d.kind == "ask" else None, parser=self.parser)
        except ParserUnavailable as e:
            raise Failed(e.reason, setup=True)
        except FetchFailed as e:
            raise Failed(e.reason)
        return {"url": page["url"], "title": page["title"], "text": page["markdown"], "date": page["date"],
                "content_type": page["content_type"], "bytes": page["bytes"], "redirects": page["hops"][1:],
                "links": page["links"], "flags": page["flags"]}

    def files_write(self, rel, text):
        try:
            self.ws.write_text(rel, text)
        except FilesDenied as e:
            raise Failed(e.reason)

    def url_reasons(self, url, corpus="", allowed="", seen=()):
        return url_reasons(url, corpus, allowed, seen)

    def query_reasons(self, query, corpus="", allowed=""):
        return query_reasons(query, corpus, allowed)

    def url_decision(self, url, corpus="", allowed="", seen=()):
        return exfil.check_url(url, notes=corpus, question=allowed, seen=seen)

    def query_decision(self, query, corpus="", allowed=""):
        return exfil.check_query(query, notes=corpus, question=allowed)

    def close(self):
        pass


def url_reasons(url, corpus="", allowed="", seen=()):
    """Why the user must approve this URL first (spec section 4), as a list; empty when it may go. A URL the guard refuses
    returns [] here: web_read raises Denied for it."""
    d = exfil.check_url(url, notes=corpus, question=allowed, seen=seen)
    return [d.reason] if d.kind == "ask" else []


def query_reasons(query, corpus="", allowed=""):
    """Why the user must approve this search first, as a list; empty when it may go."""
    d = exfil.check_query(query, notes=corpus, question=allowed)
    return [d.reason] if d.kind == "ask" else []
