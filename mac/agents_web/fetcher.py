"""SandboxedGuard: the egress guard run in its own sandboxed child for every fetch (security baseline item 2).

The app process never connects to the web itself: each fetch is fetch_child.py under profiles/fetch.sb, which has the
network (tcp 80/443 out and the DNS resolver; never loopback) but runs no other program and reads nothing beyond
Python, this package and the CA store. The guard's own rules (DNS checked by address, every hop, the deny list, TLS,
size and time caps) run inside it. Seatbelt is a second wall for loopback and other ports only: it cannot single out
LAN, tailnet or this Mac's other addresses on 80/443, which the guard alone refuses (pf or a broker: phase 2). Parsing stays in the parser's own
sandbox, which has no network. Fail closed: no sandbox-exec, a profile the sandbox rejects, a probe that finds it not
in force, or no runtime: ParserUnavailable (the tools say the web tools are not set up), and nothing is fetched.
"""
import base64
import json
import os
import shutil
import socket
import tempfile
import threading

from . import guard as G, sandbox
from .decision import Deny, Denied, FetchFailed, ParserUnavailable

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_child.py")
PKG = os.path.dirname(os.path.abspath(__file__))
_probe = {"ok": None, "why": ""}
_probe_lock = threading.Lock()


def _runtime():
    from .read import DEFAULT_PARSER_VENV  # the same pinned Python as the parser (stdlib only is used here)
    try:
        return sandbox.venv_runtime(DEFAULT_PARSER_VENV)
    except sandbox.Unavailable as e:
        raise ParserUnavailable("The sandboxed fetcher needs the tools' Python (tools/setup_agents_tools.sh): %s" % e)


def command(argv, routes=(), ca=None):
    """The sandbox-exec command for fetch_child.py and its environment. routes: local ports of --fake test routes."""
    ok, why = sandbox.available()
    if not ok:
        raise ParserUnavailable(why)
    py, runtime = _runtime()
    pkg, child = os.path.realpath(PKG), os.path.realpath(CHILD)
    paths = [runtime, pkg, child] + ([os.path.realpath(ca)] if ca else [])
    try:
        text = sandbox.render("fetch.sb", paths)
    except sandbox.Unavailable as e:
        raise ParserUnavailable(str(e))
    extra = " ".join('(remote tcp "localhost:%d")' % int(p) for p in sorted(set(routes)))
    text = text.replace("@TESTROUTES@", "(allow network-outbound %s)" % extra if extra else "")
    if ca:
        text += '\n(allow file-read* (literal "%s"))\n' % os.path.realpath(ca).replace('"', "")
    params = {"PYTHON": py, "RUNTIME": runtime, "PKG": pkg}
    env = {"PATH": "/usr/bin:/bin", "HOME": "/var/empty", "LC_ALL": "C.UTF-8"}
    return sandbox.command(text, params, [py, "-I", "-S", "-X", "utf8", child] + list(argv)), env


def check():
    """(True, "") once the probe has shown the sandbox in force (no program runs, a file outside cannot be read, this
    Mac cannot be reached), else (False, why). Cached after a pass."""
    with _probe_lock:
        if _probe["ok"]:
            return True, ""
        res = run_probe()
        bad = [k for k, v in res.items() if v]
        _probe["ok"] = not bad and bool(res)
        _probe["why"] = "" if _probe["ok"] else "The fetcher's sandbox is not in force (%s)." % (", ".join(bad) or "no answer")
        return _probe["ok"], _probe["why"]


def run_probe(home=None):
    """The probe's answer: {"spawn", "fork", "read", "connect", "list_home"} -> worked (all False in force)."""
    tmp = tempfile.mkdtemp(prefix="halo-fetch-probe-")
    canary = os.path.join(tmp, "canary")
    with open(canary, "w") as f:
        f.write("canary")
    lsn = socket.socket()
    lsn.bind(("127.0.0.1", 0))
    lsn.listen(1)
    try:
        cmd, env = command(["--probe"])
        env.update(PROBE_FILE=os.path.realpath(canary), PROBE_HOST=lsn.getsockname()[0], PROBE_PORT=str(lsn.getsockname()[1]),
                   PROBE_HOME=home or os.path.expanduser("~"))
        rc, out, err, killed = sandbox.run(cmd, timeout_s=15, env=env)
        try:
            return json.loads(out or b"{}")
        except ValueError:
            return {}
    finally:
        lsn.close()
        shutil.rmtree(tmp, ignore_errors=True)


class SandboxedGuard(G.EgressGuard):
    """EgressGuard with fetch() in the sandboxed child. Test routes and a test CA (--fake only) go to the child with the
    request; the live app's guard has none, and its profile allows no local port."""

    def fetch(self, url, *, accept=G.ACCEPT):
        ok, why = check()
        if not ok:
            raise ParserUnavailable(why)
        routes = dict(self._routes)
        req = {"url": url, "accept": accept, "network": self.network, "max_bytes": self.max_bytes, "timeout_s": self.timeout_s,
               "max_redirects": self.max_redirects, "fake": self.fake, "internet": self.internet, "routes": routes,
               "ca": self._extra_ca}
        cmd, env = command([], routes.values(), self._extra_ca)
        rc, out, err, killed = sandbox.run(cmd, stdin=json.dumps(req).encode(), timeout_s=self.timeout_s + 10,
                                           mem_bytes=512 << 20, max_out=int(self.max_bytes * 1.4) + (1 << 20), env=env)
        if killed:
            raise FetchFailed("The fetch was stopped (%s limit)." % killed, url)
        try:
            res = json.loads(out)
        except ValueError:
            raise FetchFailed("The fetcher did not answer.", url)
        if not res.get("ok"):
            if res.get("kind") == "denied":
                d = res.get("decision") or {}
                raise Denied(Deny(str(d.get("rule") or "address"), str(d.get("reason") or res.get("reason") or "")), res.get("url") or url)
            raise FetchFailed(str(res.get("reason") or "The page could not be fetched."), res.get("url") or url)
        r = res["response"]
        return G.Response(url=r["url"], requested=r["requested"], status=int(r["status"]), headers=r.get("headers") or {},
                          content_type=r.get("content_type") or "", charset=r.get("charset"), body=base64.b64decode(r["body_b64"]),
                          hops=list(r.get("hops") or []), ip=r.get("ip") or "", elapsed_s=float(r.get("elapsed_s") or 0))
