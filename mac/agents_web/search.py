"""web_search: one query to SearXNG, which runs on this Mac as StrixFold UI's child process, under its own Seatbelt
profile (profiles/searxng.sb), bound to 127.0.0.1 on a free port.

- The query guard runs first (exfil.check_query): a Deny raises Denied, an Ask raises NeedsApproval until the user approves
  that exact query.
- The client sends only the query (q, with SearXNG's !bang, :language and <timeout syntax taken out, so the model
  never picks engines or options) and format=json, and reads only result titles, snippets, URLs and the engines that
  found them, plus each engine's health. It is the one piece of the tools that connects to 127.0.0.1, and only to the
  port of the SearXNG it started.
- Results: title, URL (tracking parameters off, checked by the guard's static rules), snippet, the engines that found
  it (kept through deduplication) and the retrieval time. A domain limit is a filter on the results, never site:.
- Seatbelt filters SearXNG's bind by port only, so start() also checks with lsof that it listens on 127.0.0.1:<port>
  and nothing else, and stops it otherwise.
- Its cache (which holds query text) is a fresh folder per start, removed when it stops; nothing else is written.
- SearXNG starts on the first search and stops after 10 minutes without one, when StrixFold UI stops, or if StrixFold UI
  Control dies (the child watches its parent). No launchd, nothing persistent. Its output is kept in memory only
  (the last 50 lines, for a failed start), because it can hold query text.
"""
import atexit
import collections
import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode

from . import exfil, guard, sandbox
from .decision import Denied, Deny, FetchFailed, NeedsApproval, SearchUnavailable

DEFAULT_ROOT = os.path.expanduser("~/Library/Application Support/HaloAgents/searxng")
ENGINES = ("duckduckgo web", "brave", "startpage", "mojeek", "bing", "yahoo", "wikipedia", "arxiv")  # as settings
MAX_RESULTS = 20
TITLE_MAX, SNIPPET_MAX = 300, 600
BOOT = ("import os, sys, threading, time, runpy\n"
        "parent = os.getppid()\n"
        "def watch():\n"
        "    while os.getppid() == parent:\n"
        "        time.sleep(2)\n"
        "    os._exit(0)\n"
        "threading.Thread(target=watch, daemon=True).start()\n"
        "sys.path.insert(0, os.environ['HALO_SEARXNG_SRC'])\n"
        "runpy.run_module('searx.webapp', run_name='__main__')\n")


def _port_free(port):
    """No process listens on 127.0.0.1:port (lsof), and it can be bound."""
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"], capture_output=True, timeout=5)
        if r.stdout.strip():
            return False
    except (OSError, subprocess.SubprocessError):
        pass
    return True


def free_port():
    for _ in range(20):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if _port_free(port):
            return port
    raise SearchUnavailable("No free port for SearXNG.")


class SearxService:
    def __init__(self, root=DEFAULT_ROOT, idle_stop_s=600.0):
        self.root, self.idle_stop_s = root, float(idle_stop_s)
        self.venv, self.src = os.path.join(root, "venv"), os.path.join(root, "src")
        self.settings, self.cache, self.secret = (os.path.join(root, n) for n in ("settings.yml", "cache", "secret"))
        self.port, self._proc, self._last, self._run_cache = None, None, 0.0, None
        self._lines = collections.deque(maxlen=50)
        self._lock = threading.RLock()
        atexit.register(self.stop)

    def installed(self):
        return all(os.path.exists(p) for p in (os.path.join(self.venv, "bin", "python"), self.settings, self.secret,
                                               os.path.join(self.src, "searx", "webapp.py")))

    def status(self):
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            return {"installed": self.installed(), "running": running, "port": self.port if running else None,
                    "pid": self._proc.pid if running else None,
                    "idle_s": round(time.monotonic() - self._last, 1) if running else None}

    def touch(self):
        self._last = time.monotonic()

    def _drain(self, proc):
        try:
            for line in iter(proc.stdout.readline, b""):
                self._lines.append(line.decode("utf-8", "replace").rstrip()[:300])
        except (OSError, ValueError):  # closed by stop()
            pass

    def _listening(self, pid):
        """The addresses process pid listens on over TCP, as lsof names them (127.0.0.1:8890, *:8890, [::1]:8890)."""
        try:
            r = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return []
        return sorted({ln.split()[-2] for ln in r.stdout.splitlines() if ln.endswith("(LISTEN)")})

    def _ready(self, port):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/healthz")
            ok = c.getresponse().status == 200
            c.close()
            return ok
        except (OSError, http.client.HTTPException):
            return False

    def new_cache(self):
        """A fresh cache folder for one SearXNG start (its engine cache holds query text, so it is removed at stop).
        Folders left by a StrixFold UI that is no longer running are removed first."""
        os.makedirs(self.cache, mode=0o700, exist_ok=True)
        for name in os.listdir(self.cache):
            path = os.path.join(self.cache, name)
            owner = name.split("-")[1] if name.startswith("run-") and name.count("-") >= 2 else None
            if owner is None:
                if os.path.isfile(path) and name.startswith("sxng_cache_"):  # from before folders were per start
                    os.unlink(path)
                continue
            try:
                os.kill(int(owner), 0)
            except (ValueError, ProcessLookupError):
                shutil.rmtree(path, ignore_errors=True)
            except PermissionError:
                pass
        return tempfile.mkdtemp(prefix="run-%d-" % os.getpid(), dir=self.cache)

    def _command(self, port, args, cache):
        """(sandbox-exec command, environment, cwd) running the venv's Python with args under profiles/searxng.sb,
        allowed to serve on 127.0.0.1:port and to write only `cache`. (Tests run a probe script this way.)"""
        try:
            py, runtime = sandbox.venv_runtime(self.venv)
            paths = {k: os.path.realpath(v) for k, v in (("VENV", self.venv), ("SRC", self.src),
                                                         ("SETTINGS", self.settings), ("CACHE", cache))}
            profile = sandbox.render("searxng.sb", list(paths.values()) + [runtime])
            with open(self.secret) as f:
                secret = f.read().strip()
        except (sandbox.Unavailable, OSError) as e:
            raise SearchUnavailable(str(e))
        params = dict(paths, PYTHON=py, RUNTIME=runtime, PORT=str(port))
        env = {"PATH": "/usr/bin:/bin", "HOME": paths["CACHE"], "TMPDIR": paths["CACHE"] + "/", "LC_ALL": "C.UTF-8",
               "SEARXNG_SETTINGS_PATH": paths["SETTINGS"], "SEARXNG_SECRET": secret, "SEARXNG_PORT": str(port),
               "SEARXNG_BIND_ADDRESS": "127.0.0.1", "HALO_SEARXNG_SRC": paths["SRC"]}
        python = os.path.join(paths["VENV"], "bin", "python")
        return sandbox.command(profile, params, [python, "-I", "-B", "-X", "utf8"] + list(args)), env, paths["CACHE"]

    def start(self, timeout_s=30.0):
        """The port of the running SearXNG, starting it first if needed."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self.touch()
                return self.port
            if not self.installed():
                raise SearchUnavailable("SearXNG is not installed (tools/setup_agents_tools.sh).")
            ok, why = sandbox.available()
            if not ok:
                raise SearchUnavailable(why)
            port = free_port()
            self._run_cache = self.new_cache()
            cmd, env, cwd = self._command(port, ["-c", BOOT], self._run_cache)
            self._lines.clear()
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env=env, cwd=cwd, start_new_session=True)
            threading.Thread(target=self._drain, args=(proc,), daemon=True, name="searxng-out").start()
            self._proc, self.port = proc, port
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if self._ready(port):
                    where = self._listening(proc.pid)
                    if where != ["127.0.0.1:%d" % port]:  # Seatbelt cannot pin the address, so check it here
                        self.stop()
                        raise SearchUnavailable("SearXNG listened on %s, not only 127.0.0.1:%d; stopped it."
                                                % (", ".join(where) or "nothing", port))
                    self.touch()
                    threading.Thread(target=self._idle, args=(proc,), daemon=True, name="searxng-idle").start()
                    return port
                time.sleep(0.25)
            self.stop()
            raise SearchUnavailable("SearXNG did not start." + (" Last output: " + self._lines[-1] if self._lines else ""))

    def _idle(self, proc):
        while proc.poll() is None:
            time.sleep(min(30.0, max(1.0, self.idle_stop_s / 4)))
            with self._lock:
                if self._proc is proc and time.monotonic() - self._last > self.idle_stop_s:
                    self.stop()
                    return

    def stop(self):
        """Stop the SearXNG this object started (by its saved process, never by pattern)."""
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(5)
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            if self._run_cache:
                shutil.rmtree(self._run_cache, ignore_errors=True)
                self._run_cache = None

    def query(self, q, timeout_s=20.0):
        """SearXNG's JSON answer for q: GET /search?q=...&format=json on 127.0.0.1:<its port>, nothing else."""
        port = self.start()
        self.touch()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
            c.request("GET", "/search?" + urlencode({"q": q, "format": "json"}), headers={"Accept": "application/json"})
            r = c.getresponse()
            body = r.read(4 << 20)
            c.close()
        except (OSError, http.client.HTTPException):
            raise FetchFailed("The search service did not answer.")
        if r.status != 200:
            raise FetchFailed("The search service answered HTTP %d." % r.status)
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise FetchFailed("The search service sent a broken answer.")


def neutral_query(query):
    """The query with SearXNG's routing syntax taken out, so the model cannot pick engines, categories, a language or a
    timeout: any word starting with "!" (bangs, "!!" external bangs and the lucky redirect), ":" or "<" loses those
    leading characters."""
    return " ".join(w.lstrip("!:<") for w in query.split() if w.lstrip("!:<"))


def _domain_ok(host, domains, network=None):
    if network is not None and not guard.host_in(host, network):
        return False
    return domains is None or any(guard._matches(host, d) for d in domains)


def search_results(query, *, service, notes="", question="", approved=None, max_results=10, domains=None,
                   network=None):
    """{"results": [{"title", "snippet", "url", "engines", "retrieved"}], "health": {engine: "ok" | "no results" |
    an error}, "retrieved": ISO time}. `domains` (None = any) keeps only results on those hosts and their subdomains
    ("example.com" or "*.example.com"); it filters the results, it is never sent as site:. `network` (an agent
    definition's {"allow", "deny"} lists) filters the same way."""
    d = exfil.check_query(query, notes=notes, question=question)
    if d.kind == "deny":
        raise Denied(d)
    if d.kind == "ask" and approved != d.key:
        raise NeedsApproval(d)
    q = neutral_query(query)
    if not q:
        raise Denied(Deny("empty", "The search is empty."))
    raw = service.query(q)
    retrieved = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    health = {e: "no results" for e in ENGINES}
    out, by_url = [], {}
    for r in raw.get("results") or []:
        if not isinstance(r, dict):
            continue
        engines = [str(e) for e in (r.get("engines") or [r.get("engine")]) if isinstance(e, str)][:10]
        for e in engines:
            health[e] = "ok"
        try:
            url = guard.clean_url(str(r.get("url") or ""))
        except Denied:
            continue
        if guard.check_static(url).kind != "allow" or not _domain_ok(guard._split(url)[1], domains, network):
            continue
        if url in by_url:  # a duplicate after cleaning: keep the first, add its engines
            by_url[url]["engines"] = sorted(set(by_url[url]["engines"]) | set(engines))
            continue
        if len(out) >= max(1, min(int(max_results), MAX_RESULTS)):
            continue
        item = {"title": " ".join(str(r.get("title") or "").split())[:TITLE_MAX],
                "snippet": " ".join(str(r.get("content") or "").split())[:SNIPPET_MAX], "url": url,
                "engines": sorted(set(engines)), "retrieved": retrieved}
        by_url[url] = item
        out.append(item)
    for e in raw.get("unresponsive_engines") or []:
        if isinstance(e, (list, tuple)) and e and isinstance(e[0], str):
            health[e[0]] = "error: " + " ".join(str(x) for x in e[1:2])[:80]
    if not raw.get("results") and raw.get("unresponsive_engines"):
        raise FetchFailed("No search engine answered (%d failed)." % len(raw["unresponsive_engines"]))
    return {"results": out, "health": health, "retrieved": retrieved}


def web_search(query, *, service, notes="", question="", approved=None, max_results=10, domains=None, network=None):
    """The results of search_results (see there and the module docstring)."""
    return search_results(query, service=service, notes=notes, question=question, approved=approved,
                          max_results=max_results, domains=domains, network=network)["results"]
