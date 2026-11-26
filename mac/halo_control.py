#!/usr/bin/env python3
"""StrixFold UI on the Mac: the dashboard's small local server and the SSH tunnels it needs.

  halo_control.py serve [--fake] [--port N] [--endpoint-port N] [--halod-port N]
      run in the foreground (the halo-control command and launchd use this; the port options let a fake dashboard
      run beside the real one)

It binds 127.0.0.1 only. It serves the dashboard (mac/ui), proxies /api/* to halod on box2 through an SSH tunnel,
and keeps a second tunnel that puts the model's OpenAI-compatible endpoint at http://127.0.0.1:18080/v1 for agents
on this Mac. Both tunnels use keepalives and come back with backoff after sleep, Wi-Fi changes or a dropped link.
State-changing requests need this install's token header (injected into the page it serves), a Host of
127.0.0.1/localhost and a same-origin Origin, so another web page cannot drive it (CSRF, DNS rebinding). It calls no
host other than the two boxes over SSH. Python 3.9 standard library only.
"""
import gzip
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agents  # noqa: E402
import chats  # noqa: E402
COUSTEAU_SECTION = False
import studio  # noqa: E402
import halo_chat  # noqa: E402
import chat_direct  # noqa: E402  (built-in command runner, loaded before the runtime import lock)
import outbox as outbox_mod  # noqa: E402
import studio_http  # noqa: E402
import studio_transcribe  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "box"))
import profiles as _profiles  # noqa: E402  (the model names only)
MODEL_NAMES = {m["id"]: m["name"] for m in _profiles.MODELS}

REPO = os.path.dirname(HERE)
UI = os.path.join(HERE, "ui")
CONF_DIR = os.path.expanduser("~/.config/halo-control")
LOG = os.path.expanduser("~/Library/Logs/halo-control.log")
VERSION = "1.1"

DEFAULTS = {
    "ui_port": 8711,             # the dashboard: http://127.0.0.1:8711/
    "halod_local_port": 8712,    # this Mac's end of the tunnel to halod (box2 127.0.0.1:8710)
    "halod_host": "box2", "halod_port": 8710,
    "endpoint_local_port": 18080,  # the model for agents: http://127.0.0.1:18080/v1
    "endpoint_host": "box1", "endpoint_port": 18080,
    "endpoint2_local_port": 18090,  # Qwen3.8 (Gufo's halo-qwen, or box1's copy of Kyojin / Gufo v0.21): http://127.0.0.1:18090/v1
    "endpoint2_host": "box1", "endpoint2_port": 18090,
    # box2's copy of a one-per-box Qwen engine (Kyojin, Gufo v0.21: `up each`): http://127.0.0.1:18095/v1. Chat and
    # the agent runner give each conversation one copy when it starts and keep it there (App.endpoint_for).
    "endpoint3_local_port": 18095,
    "endpoint3_host": "box2", "endpoint3_port": 18090,
    # The main pair's boxes in rank order (rank 0 serves the model, rank 1 runs its halod) and
    # its name on the pages; a setup whose main pair is not the main boxes names its own (site_defaults: the extra pairs)
    "halos_boxes": ["box1", "box2"], "halos_label": "box1 + box2",
    # More pairs, each with its own halod and its model endpoint (name, label, halod_host / _local_port / _port, endpoint_host
    # / _local_port / _port): a chat goes to one while it serves the chat's model (App.chat_state; Private chats,
    # Studio and the rest stay on the main boxes)
    "pairs": [],
    # Direct access's computers besides this Mac (chat_direct.TARGETS: id -> name, host, shell, note) and the web browser's
    # ssh host (chat_browse): none unless configured
    "direct_targets": {},
    "browse_host": None,
    "ssh_opts": ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
                 "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes", "-o", "TCPKeepAlive=yes"],
    # The tunnels that carry prompts and media (the model endpoints) go straight to the boxes' tailnet
    # addresses (ssh -o ProxyCommand=none: the system Tailscale's route) rather than through ~/.ssh/config's
    # ProxyCommand (the userspace tailscaled's SOCKS port): a tunnel that had been up a minute or more carried 1.31 MB
    # in 2.5-5 s that way, against about 0.3 s direct. When the direct path does not come up in 15 s, a tunnel uses the
    # ssh config's path and tries direct again 10 minutes later. halod's tunnel (small JSON) keeps the ssh config's
    # path. false: the ssh config's path only. ~/.ssh/config itself is never changed.
    "tunnel_direct": True,
    # a tunnel up this long is rebuilt when nothing is using it (a long-lived path that has slowed starts afresh)
    "tunnel_refresh_h": 6,
    # Phone access on the home network (no login). The dashboard also listens on
    # all interfaces; only private-range clients get an answer, and only under this Mac's own names (DNS rebinding).
    # The model endpoint stays 127.0.0.1-only. Off unless config.json has "lan": true; --no-lan turns it off.
    "lan": False,
    # StrixFold UI's private store (HALO_STUDIO_ROOT or --studio-root move it; --fake uses the synthetic test store)
    "studio_root": "~/Pictures/StrixFoldUI",
    # Studio away from the Mac goes only over HTTPS on the tailnet: `tailscale serve` (never Funnel) proxies
    # https://<this Mac>.<tailnet>.ts.net/ to this 127.0.0.1-only port. Plain HTTP from the LAN never opens Studio.
    "studio_tailnet_port": 8713,
    # Host names the tailnet listener accepts besides the name Tailscale reports for this Mac (which follows a rename
    # by itself), e.g. ["halo.example-tailnet.ts.net"]
    "studio_tailnet_hosts": [],
    "tailscale": "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
}

SITE_MAIN = {}  # this setup's main pair, when it is not the main boxes (load_config: the running app's, not DEFAULTS)
try:
    import site_defaults  # this setup's own machines (the open-source export leaves it out)
    DEFAULTS.update(getattr(site_defaults, "CONFIG", None) or {})
    SITE_MAIN = dict(getattr(site_defaults, "MAIN_PAIR", None) or {})
except ImportError:
    pass


_ts = {"t": 0.0, "name": None, "ready": False}


def tailnet_name(cfg, fresh=False):
    """(this Mac's tailnet name, whether `tailscale serve` proxies Studio's port), re-read every 60 s."""
    if not fresh and time.time() - _ts["t"] < 60:
        return _ts["name"], _ts["ready"]
    name, ready = None, False
    # the Mac app's CLI without TERM tries to start its GUI and fails ("The Tailscale GUI failed to start"), and launchd
    # (`halo-control install-login`) gives the server no TERM: no name, so the phone got "host not allowed"
    env = dict(os.environ, TERM=os.environ.get("TERM") or "dumb")
    try:
        st = json.loads(subprocess.run([cfg["tailscale"], "status", "--json"], capture_output=True, timeout=10, env=env).stdout or b"{}")
        name = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".").lower() or None
        sv = subprocess.run([cfg["tailscale"], "serve", "status", "--json"], capture_output=True, timeout=10, env=env).stdout or b"{}"
        ready = f"127.0.0.1:{cfg['studio_tailnet_port']}" in sv.decode("utf-8", "replace")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    _ts.update(t=time.time(), name=name, ready=ready)
    return name, ready


def _private_ip(ip):
    """True for loopback and RFC 1918 / link-local IPv4 (and their IPv4-mapped IPv6 forms)."""
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    if ip in ("127.0.0.1", "::1") or ip.startswith("127."):
        return True
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except ValueError:
        return False
    return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31) or (a == 169 and b == 254)


_lan_cache = {"t": 0.0, "names": set()}


def lan_names():
    """This Mac's LAN addresses and Bonjour name, lower-case, re-read every 30 s (DHCP can move the address)."""
    if time.time() - _lan_cache["t"] < 30:
        return _lan_cache["names"]
    names = set()
    for ifc in ("en0", "en1", "en2"):
        try:
            ip = subprocess.run(["ipconfig", "getifaddr", ifc], capture_output=True, text=True, timeout=2).stdout.strip()
            if ip:
                names.add(ip)
        except Exception:
            pass
    try:
        host = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=2).stdout.strip()
        if host:
            names.add(host.lower() + ".local")
    except Exception:
        pass
    _lan_cache.update(t=time.time(), names=names)
    return names

_GZ = {}  # a page file's gzip bytes by its content hash (an edit is a new entry; a landing restarts the app)
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json", ".ico": "image/x-icon",
        ".webmanifest": "application/manifest+json"}


def log(msg):
    """One line on stdout; the halo-control command and launchd send stdout to ~/Library/Logs/halo-control.log."""
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def load_config():
    cfg = dict(DEFAULTS, **SITE_MAIN)
    p = os.path.join(CONF_DIR, "config.json")
    if os.path.exists(p):
        with open(p) as f:
            cfg.update(json.load(f))
    return cfg


def ui_token(name="ui-token"):
    """A page token kept on disk (600), so an open page's writes still work after a restart: the Mac page's "ui-token",
    and the tailnet page's own "tailnet-token" (a new one per start left the user's phone app failing
    every write with "missing or wrong token" until the user force-quit it)."""
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    p = os.path.join(CONF_DIR, name)
    if not os.path.exists(p):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    with open(p) as f:
        return f.read().strip()


def port_free(port):
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# the host's address from ~/.ssh/config, reached without its ProxyCommand; only a host whose key is already pinned in
# known_hosts (the tailnet addresses are CGNAT space: with the system Tailscale down, a connect could leave by the
# default route, and accept-new would trust a stranger)
DIRECT = ["-o", "ProxyCommand=none", "-o", "StrictHostKeyChecking=yes"]
DIRECT_RETRY_S = 600
PAIR_GRACE_S = 120  # an extra pair out of reach this long after it last served a chat's model is no longer waited for


def clients_on(port):
    """Connections through this Mac's end of a tunnel now (lsof; 0 when it cannot tell, so a refresh waits)."""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP@127.0.0.1:{port}", "-sTCP:ESTABLISHED", "-t"], capture_output=True,
                             timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return len(out.stdout.split())


class Tunnel:
    """One `ssh -N -L` kept alive: restarted with backoff when it exits, killed and restarted when its check fails
    three times in a row or the Mac wakes from sleep. With direct, it tries the tailnet address without ~/.ssh/config's
    ProxyCommand first (DEFAULTS tunnel_direct) and falls back to the ssh config's path; with refresh_s, a tunnel up
    that long is rebuilt once nothing uses it."""

    def __init__(self, name, host, lport, rport, opts, check=None, direct=False, refresh_s=None, clients=None):
        self.name, self.host, self.lport, self.rport, self.opts, self.check = name, host, lport, rport, opts, check
        self.direct, self.refresh_s, self.clients = direct, refresh_s, clients or clients_on
        self.direct_failed = 0.0  # when the direct path last failed to come up
        self.via = None
        self.proc = None
        self.state, self.since, self.error, self.restarts = "starting", time.time(), None, 0
        self.fails = 0
        self.mu = threading.Lock()
        self.kick = threading.Event()
        self.stopped = False

    def use_direct(self):
        return self.direct and time.time() - self.direct_failed > DIRECT_RETRY_S

    def argv(self, direct=False):
        return (["ssh", "-N"] + self.opts + (DIRECT if direct else [])
                + ["-L", f"127.0.0.1:{self.lport}:127.0.0.1:{self.rport}", self.host])

    def set(self, state, error=None):
        if state != self.state:
            what = f"{state} via {self.via}" if state == "up" and self.via else state  # the path each tunnel uses, logged
            log(f"tunnel {self.name}: {what}{' (' + error + ')' if error else ''}")
            self.state, self.since = state, time.time()
        self.error = error

    def run(self):
        backoff = 1
        while not self.stopped:
            if not port_free(self.lport):
                self.set("blocked", f"port {self.lport} on this Mac is in use by another program")
                time.sleep(5)
                continue
            t0 = time.time()
            err = []
            direct = self.use_direct()
            try:
                p = subprocess.Popen(self.argv(direct), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except OSError as e:
                self.set("down", str(e))
                time.sleep(backoff)
                continue
            with self.mu:
                self.proc = p
            threading.Thread(target=lambda: [err.append(l.decode("utf-8", "replace").strip()) for l in p.stderr], daemon=True).start()
            # up once the local port listens (the direct path: within its ConnectTimeout, or it is not there)
            listening = False
            for _ in range(150 if direct else 100):
                if p.poll() is not None:
                    break
                if not port_free(self.lport):
                    listening = True
                    break
                time.sleep(0.1)
            if direct and not listening:  # the ssh config's path at once; direct again DIRECT_RETRY_S later
                if p.poll() is None:
                    p.kill()
                p.wait()
                self.direct_failed = time.time()
                why = " | ".join(e for e in err if e and "Warning" not in e)[-200:] or "no answer"
                log(f"tunnel {self.name}: the direct path did not come up ({why}); using the ssh config's path")
                continue
            if p.poll() is None:
                self.via = "direct" if direct else "ssh config"
                self.set("up")
                self.fails = 0
            refreshed = False
            while p.poll() is None and not self.stopped:
                if self.kick.wait(5):
                    self.kick.clear()
                    p.terminate()
                    break
                if self.refresh_s and time.time() - t0 > self.refresh_s and not self.clients(self.lport):
                    self.set("refreshing", f"up {int((time.time() - t0) / 3600)} h and unused: rebuilt")
                    refreshed = True
                    p.terminate()
                    break
                if self.check:
                    ok = self.check()
                    if ok is False:
                        self.fails += 1
                        if self.fails >= 3:
                            self.set("down", "no answer through the tunnel; reconnecting")
                            p.terminate()
                            break
                    elif ok:
                        self.fails = 0
                        if self.state != "up":
                            self.set("up")
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
            if self.stopped:
                break
            if refreshed:
                continue
            msg = " | ".join(e for e in err if e and "Warning" not in e)[-300:] or f"ssh exited ({p.returncode})"
            self.restarts += 1
            backoff = 1 if time.time() - t0 > 60 else min(30, backoff * 2)
            self.set("down", f"{msg}; retrying in {backoff} s")
            if self.kick.wait(backoff):
                self.kick.clear()

    def restart(self):
        self.kick.set()

    def stop(self):
        self.stopped = True
        with self.mu:
            p = self.proc
        if p and p.poll() is None:
            p.terminate()

    def view(self):
        return {"name": self.name, "state": self.state, "since": self.since, "error": self.error, "restarts": self.restarts,
                "local": f"127.0.0.1:{self.lport}", "remote": f"{self.host}:127.0.0.1:{self.rport}", "via": self.via}


class App:
    def __init__(self, cfg, fake=False):
        self.cfg, self.fake = cfg, fake
        if fake:  # Chat's Full access (chat_full.py) never makes folders in the real ~/HaloChats from the fake app
            os.environ.setdefault("HALO_CHATS_ROOT", os.path.expanduser("~/HaloChats-test"))
        self.code_check = None  # private_code.CodeCheck, set by serve at start; None: not checked, so Private can't unlock
        self.ui_token = ui_token()
        self.tailnet_token = ui_token("tailnet-token")  # the Studio-only page's own token (the tailnet never sees ui_token)
        self.halod_token = None
        self.halod = f"http://127.0.0.1:{cfg['halod_local_port']}"
        self.pairs = {p["name"]: dict(p, url=f"http://127.0.0.1:{p['halod_local_port']}", token=None, st=(0.0, None))
                      for p in cfg.get("pairs") or []}  # the extra pairs' halods
        self.pairs_on = False  # their tunnels run (start): an App that never started them (tests) never asks them
        self.tunnels = []
        self.fake_proc = None
        self.started = time.time()
        self.runner = None
        self._st = (0.0, None)
        self._models_seen = {}  # (run id, endpoint) -> (time, model names the tunnel answered with)
        self._aff, self._aff_run, self._aff_mu = {}, None, threading.Lock()  # conversation -> its copy, for one run


    # -- the agent runner's view of halod
    def halod_state(self, fresh=False):
        t = time.time()
        if t - self._st[0] < 1.0 and not fresh:
            return self._st[1]
        try:
            req = urllib.request.Request(self.halod + "/api/state", headers={"Host": "127.0.0.1"})
            with urllib.request.urlopen(req, timeout=5) as r:
                st = json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            st = None
        self._st = (t, st)
        return st

    def pair_state(self, name, fresh=False):
        """An extra pair's halod state, marked "pair": its name (local_endpoint gives its tunnel); None when it does not
        answer (and always with --fake, or before start() runs its tunnels)."""
        p = self.pairs.get(name)
        if p is None or self.fake or not self.pairs_on:
            return None
        t = time.time()
        if t - p["st"][0] < 1.0 and not fresh:
            return p["st"][1]
        try:
            req = urllib.request.Request(p["url"] + "/api/state", headers={"Host": "127.0.0.1"})
            with urllib.request.urlopen(req, timeout=5) as r:
                st = dict(json.load(r), pair=name)
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            st = None
        p["st"] = (t, st)
        return st

    def pair_check(self, name):
        try:
            with urllib.request.urlopen(self.pairs[name]["url"] + "/api/ping", timeout=4) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def pair_get(self, name, path):
        """The JSON of a GET from a pair's halod ("halos": halod on box2); None when it does not answer."""
        if name == "halos":
            return self.halod_get(path)
        p = self.pairs.get(name)
        if p is None or self.fake or not self.pairs_on:
            return None
        try:
            req = urllib.request.Request(p["url"] + path, headers={"Host": "127.0.0.1"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def pair_post(self, name, path, body, actor="team"):
        """(ok, answer) of a POST to a pair's halod ("halos": halod on box2), with its token."""
        if name == "halos":
            if not self.halod_token:
                self.fetch_halod_token()
            base, tok = self.halod, self.halod_token
        else:
            p = self.pairs[name]
            if not p["token"]:
                p["token"] = self._read_token(p["halod_host"])
            base, tok = p["url"], p["token"]
        try:
            req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Host": "127.0.0.1", "Content-Type": "application/json",
                                                                               "X-Halo-Token": tok or "", "X-Halo-Actor": actor})
            with urllib.request.urlopen(req, timeout=30) as r:
                return True, json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return False, json.load(e)
            except ValueError:
                return False, {"error": str(e)}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, {"error": str(e)}

    def pair_states(self, fresh=False):
        """[(name, label, state)] of every pair: the main boxes first, then the extra pairs (a state None: not answering)."""
        out = [("halos", self.cfg["halos_label"], self.halod_state(fresh=fresh))]
        return out + [(n, p["label"], self.pair_state(n, fresh)) for n, p in self.pairs.items()]

    def chat_state(self, model_id, hs=None, private=False, want=0, key=None):
        """The state of the pair a chat's message for model_id goes to (a Private chat: always the main boxes, the
        extra pairs' boxes are not in private_engines' facts). The main boxes while they serve the model in a window of
        at least the chat's size (want) and have a free stream (the chats and their leads, the orchestrators, run
        there; the agents flow to the extra pairs); else an extra pair that serves it in that window and whose tunnel
        answers with it (the conductor's pick: the chat's own while it has room, else the least busy); else the main
        boxes when theirs is that big (busy: the message waits its turn there); else an extra pair that served it in
        the last PAIR_GRACE_S but can't be reached now (its tunnel coming back after a restart): a "waiting" state, so
        the message waits for it rather than going to a smaller window (a 256K chat showed 63K, the main boxes'
        window); else any pair that serves the model; else the main boxes' state."""
        hs = hs if hs is not None else self.halod_state(fresh=True)
        if not model_id or private:
            return hs

        def win(st):
            return int(((st.get("profile") or {}).get("settings") or {}).get("context") or 0)
        serving, away, now = [], [], time.time()
        for name, p in self.pairs.items():
            b = self.pair_state(name)
            why = self.model_check(b) if b and b.get("state") == "ready" and (b.get("profile") or {}).get("model_id") == model_id else "other"
            if b and not why:
                p["good"] = (now, b)
                serving.append(b)
            elif (b is None or why == "unreachable") and p.get("good") and now - p["good"][0] < PAIR_GRACE_S:
                away.append(p["good"][1])
        halos = hs if hs and hs.get("state") == "ready" and (hs.get("profile") or {}).get("model_id") == model_id else None
        import conductor
        if halos and win(halos) >= want and (conductor.C is None or conductor.C.halos_free(halos, key)):
            return halos
        fits = [b for b in serving if win(b) >= want]
        if fits:  # the conductor's pick: the chat's own pair (its prompt cache) while it has room, else the least busy
            return (conductor.C.prefer(key, fits) if conductor.C is not None and key else None) or fits[0]
        if halos and win(halos) >= want:
            return halos
        for b in away:
            if win(b) >= want:
                return dict(b, state="waiting", reason="reconnecting to the extra pairs")
        return serving[0] if serving else hs

    def halod_start(self, pid, actor="agent runner"):
        """(ok, message): start a profile for an agent session (or Studio's waiting messages) that asked for it."""
        if not self.halod_token:
            self.fetch_halod_token()
        try:
            req = urllib.request.Request(self.halod + "/api/start", json.dumps({"profile": pid}).encode(),
                                         {"Host": "127.0.0.1", "Content-Type": "application/json",
                                          "X-Halo-Token": self.halod_token or "", "X-Halo-Actor": actor})
            with urllib.request.urlopen(req, timeout=30) as r:
                return True, json.load(r).get("note") or "starting"
        except urllib.error.HTTPError as e:
            try:
                return False, json.load(e).get("error") or str(e)
            except ValueError:
                return False, str(e)
        except (urllib.error.URLError, OSError) as e:
            return False, str(e)

    def private_root(self):
        """Studio's private store (~/Pictures/StrixFoldUI; the synthetic one for --fake and tests)."""
        return os.path.realpath(os.path.expanduser(os.environ.get("HALO_STUDIO_ROOT") or self.cfg.get("studio_root_override") or
                                                   ("~/Pictures/StrixFoldUI-test" if self.fake else self.cfg["studio_root"])))

    def start_runner(self):
        # Lab chat and the agent sessions live in Studio's private store, behind its passcode; their old
        # places (~/.config/halo-control/chats.sqlite and agents/) move in once, copied and checked first
        import private_move
        priv = self.private_root()
        os.makedirs(priv, mode=0o700, exist_ok=True)
        old = self.fake_root if self.fake else CONF_DIR
        os.makedirs(os.path.join(priv, "lab"), mode=0o700, exist_ok=True)
        lab = private_move.move_lab(os.path.join(old, "chats.sqlite"), os.path.join(priv, "lab", "chats.sqlite"), log)
        self.chats = chats.Store(lab)
        self.relay = chats.Relay()
        root = os.path.join(priv, "agents")
        private_move.move_agents(os.path.join(old, "agents"), root, log)
        self.runner = agents.Runner(root, self.local_endpoint, self.halod_state, self.halod_start, verify=self.model_check,
                                    pick=self.endpoint_for, copies=lambda st: len(self.replicas(st)),
                                    outside=os.path.expanduser(self.cfg.get("agents_outside_root") or (
                                        os.path.join(self.fake_root, "HaloAgents") if self.fake else "~/HaloAgents")))  # --fake: never the real one
        self.start_studio()

    # -- StrixFold UI
    @staticmethod
    def _link_orphans(st):
        """Chat uploads from before they were claimed at send go to their chats (studio.Store.link_orphans: links only)."""
        try:
            n = st.link_orphans()
            if n:
                log(f"studio: {n} upload(s) linked to their chats")
        except Exception as e:  # never stops the app; the next start tries again
            log(f"studio: linking uploads to their chats failed: {type(e).__name__}")

    def start_studio(self):
        root = self.private_root()
        st = studio.Store(root)
        threading.Thread(target=self._link_orphans, args=(st,), name="studio-link-orphans", daemon=True).start()
        if not self.fake:  # private end to end: the store is never copied by a backup (a sticky exclusion)
            studio.exclude_from_backup(root)
        import idle_engine
        eng = idle_engine.IdleEngine()
        relay = chats.Relay()
        agent = None
        chat_agent = halo_chat.ChatAgent(st, relay)
        chat_agent.app = self  # chat tools: the fake flag, the web tools' factory
        outbox = outbox_mod.Outbox(st, agent, eng, self.halod_state, self.local_endpoint,
                                   lambda pid: self.halod_start(pid, actor="studio"), lambda: st.settings().get("vision"), chat_agent,
                                   verify=self.model_check, relay=relay)
        outbox.pick = self.endpoint_for
        import chat_goal  # /goal: a chat that works on until its condition is met
        outbox.goals = chat_goal.Goals(st)
        outbox.route = self.chat_state  # a chat's waiting message goes to the pair that serves its model
        import chat_team  # team mode: a chat's lead spawns DeepSeek agents over the pairs (the Team chip)
        import conductor
        conductor.C = conductor.Conductor(self)  # every model request's slot and pair (conductor.py)
        chat_team.TEAM = chat_team.Team(self, st, outbox=outbox, fake=self.fake, conductor=conductor.C)
        # the photo look for the text-only chat models (look.py): the fake app never runs the real model
        import look
        looker = look.Looker(st, worker_cmd=[sys.executable, os.path.join(os.path.dirname(HERE), "box", "fake", "fake_look_worker.py"),
                                             "--delay", "1.5"] if self.fake else None)
        chat_agent.looker = looker
        import private_engines
        gate = private_engines.Gate(fake=self.fake)  # where a Private chat's words may go
        outbox.private_gate = gate
        self.studio = {"store": st, "auth": studio.Auth(st), "engine": eng, "relay": relay, "agent": agent, "outbox": outbox, "team": chat_team.TEAM,
                       "chat_agent": chat_agent, "transcriber": studio_transcribe.Transcriber(st), "looker": looker,
                       "private_gate": gate}
        # Agents' runner, for the Command Centre's leads (the Agents page, research runs and swarms are gone);
        # its runs in the private store's halo-agents/
        import agents_tools
        import halo_agents
        self.studio["agents"] = halo_agents.Runs(self, st.path("halo-agents"), agents_tools.make_factory(self), log=log)

        def outbox_loop():
            while not eng.stopped:
                try:
                    outbox.tick()
                except Exception as e:  # noqa: BLE001
                    log(f"studio outbox: {type(e).__name__}")
                time.sleep(1.0)
        threading.Thread(target=outbox_loop, name="studio-outbox", daemon=True).start()
        log("studio: store " + ("(synthetic test store)" if "StrixFoldUI-test" in root or self.fake else "ready") + "; it stays on this Mac")


    def studio_notify(self, text):
        s = self.studio["store"].settings()
        if s.get("notify_mac", True):
            self.studio_notify_mac(text)

    def studio_notify_mac(self, text):
        if self.fake:
            log("studio: notification (fake backend, not shown): " + text)
            return True
        threading.Thread(target=notify_mac, args=(text,), daemon=True).start()
        return True

    def halod_get(self, path):
        try:
            req = urllib.request.Request(self.halod + path, headers={"Host": "127.0.0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def halod_post(self, path, body):
        """(ok, answer) of a write to halod with its token."""
        if not self.halod_token:
            self.fetch_halod_token()
        try:
            req = urllib.request.Request(self.halod + path, json.dumps(body).encode(),
                                         {"Host": "127.0.0.1", "Content-Type": "application/json", "X-Halo-Token": self.halod_token or "",
                                          "X-Halo-Actor": "studio"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return True, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                self.halod_token = None
            try:
                return False, json.load(e)
            except ValueError:
                return False, {"error": str(e)}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, {"error": str(e)}

    def _copy_urls(self, st):
        """{box: this Mac's URL} for a one-per-box engine (its model's external "boxes" lists box2 too) served on the
        Qwen slot, else None: box1's copy through :18090, box2's through :18095."""
        c = self.cfg
        ext = (_profiles.model(((st or {}).get("profile") or {}).get("model_id")) or {}).get("external") or {}
        if self.fake or len(ext.get("boxes") or []) < 2 or urlparse((st or {}).get("endpoint") or "").port != c["endpoint2_port"]:
            return None
        b0, b1 = c["halos_boxes"][:2]
        return {b0: f"http://127.0.0.1:{c['endpoint2_local_port']}/v1", b1: f"http://127.0.0.1:{c['endpoint3_local_port']}/v1"}

    def replicas(self, st=None):
        """This Mac's URLs for every copy of the serving model that still serves (halod's "copies": a copy whose box
        failed is dropped while the other serves), box1's first."""
        urls = self._copy_urls(st)
        if not urls:
            return [self.local_endpoint(st)]
        names = self.cfg["halos_boxes"][:2]
        live = [urls[b] for b in names if b in ((st or {}).get("copies") or names)]
        return live or [self.local_endpoint(st)]

    def endpoint_for(self, st, key=None, busy=None):
        """The copy a conversation uses (key: a chat or an agent session). Picked when the conversation is first seen in
        this run: the copy that fewer of the `busy` conversations (those answering now; None: every one seen) are on,
        and only one that answers with the model halod started. Then kept: each server keeps its own prompt cache, so a
        conversation that moved boxes would read its whole history again. Back to the first copy when its own stops
        answering."""
        eps = self.replicas(st)
        if len(eps) == 1 or key is None:
            return eps[0]
        with self._aff_mu:
            if self._aff_run != (st or {}).get("run_id"):
                self._aff, self._aff_run = {}, (st or {}).get("run_id")
            ep = self._aff.get(key)
            if ep not in eps:  # new, or its copy was dropped: the copy fewer busy conversations use
                load = {e: 0 for e in eps}
                for k, e in self._aff.items():
                    if e in load and (busy is None or k in busy):
                        load[e] += 1
                ep = min(eps, key=lambda e: (load[e], eps.index(e)))
                self._aff[key] = ep
        if ep != eps[0] and self.model_check(st, endpoint=ep):
            with self._aff_mu:
                self._aff[key] = eps[0]
            return eps[0]
        return ep

    def model_check(self, st, model_id=None, endpoint=None):
        """None when the model halod started is the one answering on this Mac's tunnel; otherwise why not. Asked before
        every Studio, chat and agent request: halod's state is ready with a run id (and the expected model when one is
        named), and the tunnel's /v1/models names the model halod's own first request saw (served_model). The tunnel
        forwards to whatever listens on the box's port: a team bench there answered a request meant for
        Studio's model."""
        if not st or st.get("state") != "ready" or not st.get("run_id"):
            return "not_ready"
        if model_id and (st.get("profile") or {}).get("model_id") != model_id:
            return "other"
        served = st.get("served_model")
        if not served:
            return "unverified"
        ep = endpoint or self.local_endpoint(st)
        key = (st.get("run_id"), ep)
        hit = self._models_seen.get(key)
        if not hit or time.time() - hit[0] > 5:
            names = None
            try:
                with urllib.request.urlopen(urllib.request.Request(ep.rstrip("/") + "/models", headers={"Authorization": "Bearer none"}), timeout=3) as r:
                    names = [str(d.get("id")) for d in (json.load(r).get("data") or []) if isinstance(d, dict)]
            except (urllib.error.URLError, OSError, ValueError, AttributeError):
                names = None
            hit = (time.time(), names)
            if len(self._models_seen) > 32:
                self._models_seen.clear()
            self._models_seen[key] = hit
        if hit[1] is None:
            return "unreachable"
        if served not in hit[1]:
            log(f"model check: the tunnel at {ep} answers with {hit[1]}, not {served!r} that halod started; requests held")
            return "mismatch"
        return None

    def local_endpoint(self, st=None):
        """This Mac's URL for the endpoint halod names (each remote port has its tunnel; the fake runs locally)."""
        c = self.cfg
        if (st or {}).get("pair") in self.pairs:
            return f"http://127.0.0.1:{self.pairs[st['pair']]['endpoint_local_port']}/v1"
        ep = (st or {}).get("endpoint") or ""
        port = urlparse(ep).port
        if self.fake and port:
            return f"http://127.0.0.1:{port}/v1"
        cp = (st or {}).get("copies")
        b0, b1 = c["halos_boxes"][:2]
        if port == c["endpoint2_port"] and cp and b0 not in cp and b1 in cp and self._copy_urls(st):
            return f"http://127.0.0.1:{c['endpoint3_local_port']}/v1"  # rank 0's copy dropped: rank 1's serves
        if port == c["endpoint2_port"]:
            return f"http://127.0.0.1:{c['endpoint2_local_port']}/v1"
        return f"http://127.0.0.1:{c['endpoint_local_port']}/v1"

    # -- halod
    def fetch_halod_token(self):
        if self.fake:
            p = os.path.join(self.fake_root, "halod", "token")
            for _ in range(50):
                if os.path.exists(p):
                    break
                time.sleep(0.1)
            self.halod_token = open(p).read().strip()
            return
        if self.cfg.get("connect"):
            return
        self.halod_token = self._read_token(self.cfg["halod_host"]) or self.halod_token

    def _read_token(self, host):
        try:
            out = subprocess.run(["ssh"] + self.cfg["ssh_opts"][:4] + [host, "cat ~/halo-control/token"],
                                 capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
            if out.returncode == 0 and out.stdout.strip():
                log("halod token read from " + host)
                return out.stdout.decode().strip()
            log("cannot read halod's token: " + out.stderr.decode("utf-8", "replace").strip()[-200:])
        except (subprocess.TimeoutExpired, OSError) as e:
            log(f"cannot read halod's token: {e}")
        return None

    def halod_check(self):
        try:
            with urllib.request.urlopen(self.halod + "/api/ping", timeout=4) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def start(self):
        c = self.cfg
        import filetext
        threading.Thread(target=lambda: filetext.helper(), name="halo-extract-build", daemon=True).start()  # PDFs and OCR ready at once
        if self.fake:
            self.start_fake()
        elif c.get("connect"):  # a StrixFold server that is already running (connect.py): no halod, one tunnel
            import connect
            connect.start(self)
        else:
            how = {"direct": bool(c.get("tunnel_direct")), "refresh_s": (c.get("tunnel_refresh_h") or 0) * 3600 or None}
            t1 = Tunnel("halod", c["halod_host"], c["halod_local_port"], c["halod_port"], c["ssh_opts"], self.halod_check,
                        refresh_s=how["refresh_s"])
            t2 = Tunnel("endpoint", c["endpoint_host"], c["endpoint_local_port"], c["endpoint_port"], c["ssh_opts"], **how)
            t3 = Tunnel("qwen endpoint", c["endpoint2_host"], c["endpoint2_local_port"], c["endpoint2_port"], c["ssh_opts"], **how)
            t3b = Tunnel("qwen endpoint box2", c["endpoint3_host"], c["endpoint3_local_port"], c["endpoint3_port"], c["ssh_opts"], **how)
            self.tunnels = [t1, t2, t3, t3b]
            for n, p in self.pairs.items():  # each extra pair: its halod, its model's endpoint
                self.tunnels.append(Tunnel(f"{n} halod", p["halod_host"], p["halod_local_port"], p["halod_port"], c["ssh_opts"],
                                           lambda n=n: self.pair_check(n), refresh_s=how["refresh_s"]))
                self.tunnels.append(Tunnel(f"{n} endpoint", p["endpoint_host"], p["endpoint_local_port"], p["endpoint_port"], c["ssh_opts"], **how))
            self.pairs_on = True
            for t in self.tunnels:
                threading.Thread(target=t.run, name=f"tunnel-{t.name}", daemon=True).start()
            threading.Thread(target=self.wake_watch, daemon=True).start()
            threading.Thread(target=self.fetch_halod_token, daemon=True).start()
        self.start_runner()

    def wake_watch(self):
        """A Mac that slept or changed networks: the wall clock jumps past the monotonic one; restart both tunnels."""
        w0, m0 = time.time(), time.monotonic()
        while True:
            time.sleep(5)
            w1, m1 = time.time(), time.monotonic()
            if (w1 - w0) - (m1 - m0) > 20:
                log(f"woke after {int((w1 - w0) - (m1 - m0))} s asleep: restarting the tunnels")
                for t in self.tunnels:
                    t.restart()
            w0, m0 = w1, m1

    def start_fake(self):
        sys.path.insert(0, os.path.join(REPO, "box", "fake"))
        import fake_env
        self.fake_root = os.environ.get("HALO_FAKE_ROOT") or os.path.expanduser("~/Library/Caches/halo-control/fake")
        self.fake_leftovers()
        cfg = fake_env.build(self.fake_root, load_s=5, speed=1, port=self.cfg["halod_local_port"],
                             endpoint_port=self.cfg["endpoint_local_port"], tick_s=1.0, qwen_port=self.cfg["endpoint2_local_port"],
                             )
        cfg.update({"lock_try_s": 2, "lock_pair_s": 2, "lock_backoff_s": [2]})
        path = os.path.join(self.fake_root, "halod.json")
        with open(path, "w") as f:
            json.dump(cfg, f, indent=1)
        logf = open(os.path.join(self.fake_root, "halod.log"), "a")
        self.fake_proc = subprocess.Popen([sys.executable, os.path.join(REPO, "box", "halod.py"), "--config", path],
                                          stdout=logf, stderr=logf, stdin=subprocess.DEVNULL)
        log(f"fake backend: halod {self.fake_proc.pid} on 127.0.0.1:{self.cfg['halod_local_port']} ({self.fake_root})")
        self.fetch_halod_token()

    def fake_leftovers(self):
        """A fake app stopped hard (kill -9) leaves its halod and its stand-in model processes running, holding its
        ports, and the next fake on them fails or talks to the old halod (a normal stop ends them). A start
        on the same fake root ends them first: only the fake's own commands (box/halod.py, box/fake/) naming this
        root."""
        root = os.path.abspath(self.fake_root)
        home = os.path.abspath(os.path.expanduser("~"))
        if root in ("/", home) or home.startswith(root.rstrip("/") + "/") or len(root.strip("/").split("/")) < 3:
            return  # a root this wide could name other fakes' processes; never end anything then
        try:
            ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return
        ended, named = [], re.compile(re.escape(root) + r"(/|\s|$)")  # this root, not one beside it (fake2)
        for line in ps.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if not pid.isdigit() or int(pid) == os.getpid() or not named.search(cmd):
                continue
            if os.path.join("box", "halod.py") in cmd or os.path.join("box", "fake", "") in cmd:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    ended.append(int(pid))
                except OSError:
                    pass
        alive, t0 = list(ended), time.time()
        while alive and time.time() - t0 < 5:
            time.sleep(0.1)
            alive = [p for p in alive if not subprocess.run(["ps", "-o", "stat=", "-p", str(p)], capture_output=True, text=True).stdout.strip()[:1] in ("", "Z")]
        for p in alive:  # (a stand-in that ignores SIGTERM)
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        if ended:
            log(f"fake backend: ended {len(ended)} process(es) an earlier fake on {root} left running")

    def stop(self):
        if getattr(self, "discord_dashboard", None):
            self.discord_dashboard.stop_event.set()
        if getattr(self, "studio", None):
            self.studio["engine"].stopped = True
            if self.studio.get("agents"):
                self.studio["agents"].close()
                import agents_tools
                agents_tools.shutdown()  # the SearXNG child
        if self.runner:
            self.runner.close()  # running agent sessions stop with the dashboard: none is left behind
        for t in self.tunnels:
            t.stop()
        if self.fake_proc and self.fake_proc.poll() is None:
            # the fake ranks outlive halod by design (re-adoption): stop the fake run first so none is left behind
            try:
                req = urllib.request.Request(self.halod + "/api/stop", b"{}", {"Content-Type": "application/json", "X-Halo-Token": self.halod_token or "", "Host": "127.0.0.1"})
                urllib.request.urlopen(req, timeout=5).read()
                for _ in range(40):
                    st = json.load(urllib.request.urlopen(self.halod + "/api/state", timeout=5))["state"]
                    if st in ("idle", "failed"):
                        break
                    time.sleep(0.25)
            except (urllib.error.URLError, OSError, ValueError):
                pass
            self.fake_proc.terminate()

    def local_status(self):
        c = self.cfg
        return {"version": VERSION, "fake": self.fake, "started": self.started, "halod_token": bool(self.halod_token),
                "tunnels": [t.view() for t in self.tunnels],
                "endpoint": {"base_url": f"http://127.0.0.1:{c['endpoint_local_port']}/v1", "model": "dsv41"},
                "endpoints": {str(c["endpoint_port"]): f"http://127.0.0.1:{c['endpoint_local_port']}/v1",
                              str(c["endpoint2_port"]): f"http://127.0.0.1:{c['endpoint2_local_port']}/v1"},
                "qwen_box2": f"http://127.0.0.1:{c['endpoint3_local_port']}/v1",
                "ui": f"http://127.0.0.1:{c['ui_port']}/",
                "lan": sorted(f"http://{n}:{c['ui_port']}/" for n in lan_names()) if c.get("lan") else [],
                }


class Handler(BaseHTTPRequestHandler):
    tailnet = False  # TailnetHandler: requests that came through `tailscale serve` (HTTPS on the tailnet)

    server_version = "halo-control/" + VERSION
    A = None  # the App

    def log_message(self, *a):
        pass

    def channel(self):
        """How this request reached the dashboard: "local" (this Mac, 127.0.0.1), "tailnet" (HTTPS through tailscale
        serve) or "lan" (plain HTTP from the home network). Studio opens on local and tailnet only."""
        if self.tailnet:
            return "tailnet"
        ip = self.client_address[0]
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return "local" if (ip.startswith("127.") or ip in ("::1", "::ffff:127.0.0.1")) and host in ("127.0.0.1", "localhost") else "lan"

    def allowed_hosts(self):
        p = self.A.cfg["ui_port"]
        hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
        if self.A.cfg.get("lan"):
            hosts |= {f"{n}:{p}" for n in lan_names()}
        return hosts

    def headers_common(self, ctype, n, cache="no-store"):
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(n))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
                         "script-src 'self'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.headers_common(ctype, len(body))
        self.end_headers()
        self.wfile.write(body)

    def asset(self, body, ctype):
        """A page file (/ui/): kept by the browser and asked again on every use (no-cache), 304 while its ETag still
        matches the bytes this server would send, gzipped when the browser takes it (caching approved;
        the phone's home-screen app fetched ~1.6 MB on every open). The page itself, the API and media stay no-store."""
        tag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 1024 and ctype not in ("font/woff2", "image/png")
        etag = tag[:-1] + '-gz"' if gz else tag
        if etag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return
        if gz:
            body = _GZ.get(tag) or _GZ.setdefault(tag, gzip.compress(body, 6))
        self.send_response(200)
        self.headers_common(ctype, len(body), cache="no-cache")
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def expected_token(self):
        return self.A.ui_token

    def private_set(self):
        """Private is set up (a vault exists): the page then comes from its start copy (ui_bytes)."""
        S = getattr(self.A, "studio", None)
        if not S:
            return False
        try:
            return S["store"].vault.is_set()
        except Exception:  # noqa: BLE001 - the store can't be read: the start copy, the safer side
            return True

    def origins(self):
        return {f"https://{h}" for h in self.allowed_hosts()} if self.tailnet else {f"http://{h}" for h in self.allowed_hosts()}

    def host_ok(self):
        if not _private_ip(self.client_address[0]):
            return False  # LAN listener: home-network clients only
        return (self.headers.get("Host") or "").lower() in self.allowed_hosts()

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "host not allowed"})
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            html = addon_blocks(ui_bytes("index.html", self.private_set()).decode()).replace("__HALO_UI_TOKEN__", self.A.ui_token)
            return self.send(200, html, MIME[".html"])
        if u.path.startswith("/ui/"):
            rel = os.path.normpath(u.path[len("/ui/"):])
            body = None if rel.startswith("..") or os.path.isabs(rel) else ui_bytes(rel, self.private_set())
            if body is None:
                return self.send(404, {"error": "not found"})
            return self.asset(body, MIME.get(os.path.splitext(rel)[1], "application/octet-stream"))
        if u.path == "/local/status":
            return self.send(200, self.A.local_status())
        if u.path == "/local/private":  # Lab chat's and the agents' lock, 200 either way: a locked page asks this first
            no = self.private_no()       # and never requests what it cannot have (no 401s in the console)
            return self.send(200, {"open": True} if not no else dict(no[1], open=False, code=no[0]))
        if u.path.startswith(("/local/agents", "/local/chats")):
            no = self.private_no()
            if no:
                return self.send(*no)
            return self.agents_get(u) if u.path.startswith("/local/agents") else self.chats_get(u)
        if u.path.startswith("/studio/"):
            return studio_http.get(self)
        if u.path.startswith("/api/"):
            return self.proxy("GET", None)
        if u.path.startswith("/pairs/"):  # an extra pair's halod: /pairs/<name>/api/...
            return self.proxy("GET", None, pair=u.path.split("/")[2])
        if u.path == "/favicon.ico":
            return self.send(204, b"", "image/x-icon")
        return self.send(404, {"error": "not found"})

    def write_ok(self):
        if not self.host_ok():
            return "host not allowed"
        origin = self.headers.get("Origin")
        if origin and origin not in self.origins():
            return "origin not allowed"
        if not secrets.compare_digest(self.headers.get("X-Halo-UI-Token") or "", self.expected_token()):
            return "missing or wrong token"
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return "JSON only"
        return None

    def do_POST(self):
        self.write("POST")

    def do_PUT(self):
        self.write("PUT")

    def do_DELETE(self):
        self.write("DELETE")

    def write(self, method):
        if urlparse(self.path).path.startswith("/studio/"):
            return studio_http.write(self, method)  # its own guards: uploads are not JSON, every route needs a session
        why = self.write_ok()
        if why:
            return self.send(403, {"error": why})
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            return self.send(413, {"error": "too large"})
        body = self.rfile.read(n) if n else b"{}"
        if u.path == "/local/tunnels/restart":
            for t in self.A.tunnels:
                t.restart()
            return self.send(200, {"ok": True})
        if u.path.startswith(("/local/agents", "/local/chats")):
            no = self.private_no()
            if no:
                return self.send(*no)
            return self.agents_write(method, u, body) if u.path.startswith("/local/agents") else self.chats_write(method, u, body)
        if u.path.startswith("/api/"):
            return self.proxy(method, body)
        if u.path.startswith("/pairs/"):
            return self.proxy(method, body, pair=u.path.split("/")[2])
        return self.send(404, {"error": "not found"})


    def private_no(self):
        """Lab chat and the agent sessions live in Studio's private store: their routes need Studio's session (the
        passcode or a paired device), as Studio's own do. From the home network over plain HTTP: the pointer to the
        tailnet, as for Studio (the tailnet listener never serves these routes). None when allowed, else (code, body)."""
        S = getattr(self.A, "studio", None)
        if not S:
            return 503, {"error": "the private store is not set up"}
        if self.channel() == "lan":
            return 403, dict(studio_http.https_page(self), private=True)
        if not studio_http.device(self):
            return 401, {"error": "locked", "private": True, "set": S["auth"].is_set()}
        return None

    # -- chats (mac/chats.py): stored on this Mac (in the private store), answers streamed from the serving model
    def chats_get(self, u):
        C = self.A.chats
        parts = u.path.strip("/").split("/")  # local chats [id [export]]
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if len(parts) == 2:
                return self.send(200, {"chats": C.list(q.get("q", ""))})
            if len(parts) == 3:
                c = C.get(parts[2])
                c["streaming"] = self.A.relay.busy(parts[2])
                return self.send(200, c)
            if len(parts) == 4 and parts[3] == "export":
                md = C.export_md(parts[2])
                self.send_response(200)
                body = md.encode()
                self.headers_common("text/markdown; charset=utf-8", len(body))
                self.send_header("Content-Disposition", f'attachment; filename="chat-{parts[2]}.md"')
                self.end_headers()
                return self.wfile.write(body)
        except KeyError:
            return self.send(404, {"error": "no such chat"})
        return self.send(404, {"error": "not found"})

    def chats_write(self, method, u, body):
        C, A = self.A.chats, self.A
        parts = u.path.strip("/").split("/")
        try:
            b = json.loads(body or b"{}")
            if method == "POST" and len(parts) == 2:
                return self.send(200, C.create(b.get("title") or "", b.get("system") or "", b.get("settings") or {}))
            if len(parts) == 3 and method == "PUT":
                return self.send(200, C.update(parts[2], b.get("title"), b.get("system"), b.get("settings")))
            if len(parts) == 3 and method == "DELETE":
                A.relay.stop(parts[2])
                C.delete(parts[2])
                return self.send(200, {"ok": True})
            if len(parts) == 4 and parts[3] == "stop" and method == "POST":
                return self.send(200, {"ok": True, "stopped": A.relay.stop(parts[2])})
            if len(parts) == 4 and parts[3] == "send" and method == "POST":
                return self.chat_stream(parts[2], b)
        except KeyError:
            return self.send(404, {"error": "no such chat"})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        return self.send(404, {"error": "not found"})

    def chat_stream(self, cid, b):
        """The answer as server-sent events: one JSON object per `data:` line."""
        A = self.A
        A.chats.get(cid)
        if A.relay.busy(cid):
            return self.send(409, {"error": "this chat is answering already; stop it first"})
        st = A.halod_state(fresh=True)
        if not st or st.get("state") != "ready":
            return self.send(409, {"error": "no model is serving", "state": (st or {}).get("state")})
        why = A.model_check(st)
        if why:  # never a model halod did not start (a team bench on the same port)
            return self.send(409, {"error": "the endpoint is not answering with the model halod started; try again in a moment", "state": why})
        p = st.get("profile") or {}
        label = MODEL_NAMES.get(p.get("model_id")) or p.get("model") or st.get("served_model") or "model"
        gen = chats.relay(A.chats, A.relay, cid, A.local_endpoint(st), st.get("served_model") or "dsv41",
                          b.get("content"), b.get("mode") or "send", b.get("attachments") or [], label)
        try:
            first = next(gen)  # refusals (an empty message, nothing to resend) come back as plain 400s
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            for ev in [first] + [None]:
                if ev is None:
                    break
                self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                self.wfile.flush()
            for ev in gen:
                self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            A.relay.stop(cid)  # the page went away: stop the model too, keep what came so far
            gen.close()

    def agents_get(self, u):
        R = self.A.runner
        parts = u.path.strip("/").split("/")  # local agents [id [log|result]]
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if len(parts) == 2:
                return self.send(200, R.view())
            if len(parts) == 4 and parts[3] == "log":
                return self.send(200, R.log_tail(parts[2], int(q.get("offset", 0))))
            if len(parts) == 4 and parts[3] == "result":
                s = R.sessions[parts[2]]
                p = os.path.join(s.dir, "result.md")
                return self.send(200, {"text": open(p, encoding="utf-8").read() if os.path.exists(p) else None})
        except KeyError:
            return self.send(404, {"error": "no such session"})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        return self.send(404, {"error": "not found"})

    def agents_write(self, method, u, body):
        R = self.A.runner
        parts = u.path.strip("/").split("/")
        try:
            if method == "POST" and len(parts) == 2:
                return self.send(200, {"ok": True, "session": R.create(json.loads(body or b"{}"))})
            if method == "POST" and parts[2:] == ["stop-all"]:
                return self.send(200, R.stop_all())
            if method == "POST" and len(parts) == 4 and parts[3] == "stop":
                return self.send(200, R.stop(parts[2]))
            if method == "DELETE" and len(parts) == 3:
                return self.send(200, R.remove(parts[2]))
        except KeyError:
            return self.send(404, {"error": "no such session"})
        except ValueError as e:
            return self.send(400, {"error": str(e), "errors": getattr(e, "errors", None)})
        return self.send(404, {"error": "not found"})

    def proxy(self, method, body, pair=None):
        """/api/* to the main boxes' halod; with pair, /pairs/<pair>/api/* to that extra pair's."""
        A = self.A
        h = {"Host": "127.0.0.1"}
        p = A.pairs.get(pair) if pair is not None else None
        if pair is not None and (p is None or not self.path.startswith(f"/pairs/{pair}/api/")):
            return self.send(404, {"error": "no such pair"})
        if method != "GET":
            if p is not None and not p["token"]:
                p["token"] = A._read_token(p["halod_host"])
            elif p is None and not A.halod_token:
                A.fetch_halod_token()
            tok = p["token"] if p is not None else A.halod_token
            if not tok:
                return self.send(503, {"error": f"no token for halod yet (cannot reach {p['halod_host'] if p else A.cfg['halod_host']})"})
            h.update({"X-Halo-Token": tok, "X-Halo-Actor": "dashboard", "Content-Type": "application/json"})
        req = urllib.request.Request((p["url"] + self.path[len(f"/pairs/{pair}"):]) if p is not None else (A.halod + self.path), body, h, method=method)
        try:
            wait = 60
            with urllib.request.urlopen(req, timeout=wait) as r:
                return self.send(r.status, r.read())
        except urllib.error.HTTPError as e:
            data = e.read()
            if e.code == 403 and method != "GET":  # halod's token may have changed: read it again next time
                if p is not None:
                    p["token"] = None
                else:
                    A.halod_token = None
            return self.send(e.code, data or {"error": str(e)})
        except (urllib.error.URLError, OSError) as e:
            return self.send(502, {"error": f"halod not reachable through the tunnel: {getattr(e, 'reason', e)}", "unreachable": True})


def ui_bytes(rel, start_copy=False):
    """A page file's bytes. While Private is set up (start_copy: a vault exists), from the copy read at start
    (private_code.UI_FILES), so an edit after start never reaches the page where the user types the passcode and
    the private token lives (landings restart the app anyway); otherwise from disk, as before (builders' fake-app page
    checks pick up their edits). None when there is no such file."""
    snap = getattr(sys.modules.get("private_code"), "UI_FILES", None)
    if start_copy and snap is not None:
        return snap.get(rel)
    full = os.path.join(UI, rel)
    if not os.path.realpath(full).startswith(os.path.realpath(UI) + os.sep) or not os.path.isfile(full):
        return None
    with open(full, "rb") as f:
        return f.read()


ADDONS = {"cousteau"} if COUSTEAU_SECTION else set()  # the add-ons whose server sides are here (the open source: none)


def notify_mac(text):
    """A macOS notification (Notification Center) with a content-free text."""
    try:
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"StrixFold UI\""],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def addon_blocks(html):
    """A page with the blocks of absent add-ons taken out: `<!-- addon:NAME -->` ... `<!-- /addon:NAME -->` (their
    styles, scripts, sidebar entries and pages), so a copy without mac/cousteau.py serves no Command Centre."""
    return re.sub(r"<!-- addon:(\w+) -->\n?(.*?)<!-- /addon:\1 -->\n?", lambda m: m.group(0) if m.group(1) in ADDONS else "",
                  html, flags=re.S)


def studio_page(token, start_copy=False):
    """The Studio-only page: studio-shell.html with the Studio blocks of index.html (one source for both)."""
    shell = addon_blocks(ui_bytes("studio-shell.html", start_copy).decode())
    full = addon_blocks(ui_bytes("index.html", start_copy).decode())
    for name in ("side", "pages", "viewer"):
        a, b = f"<!-- studio:{name} -->", f"<!-- /studio:{name} -->"
        shell = shell.replace(f"__STUDIO_{name.upper()}__", full[full.index(a) + len(a):full.index(b)])
    return shell.replace("__HALO_STUDIO_TOKEN__", token)


class TailnetHandler(Handler):
    """StrixFold UI alone, as `tailscale serve` hands it over (HTTPS on the tailnet, proxied to 127.0.0.1): the Studio
    page (its own token, never the dashboard's), its static files, and /studio/ behind Studio's session. Every other
    path is 403: the dashboard's chats, agents, models and halod are not reachable from the tailnet. Only this Mac's
    tailnet name is accepted as Host; writes need an https:// Origin; Studio's cookie is Secure here."""
    tailnet = True
    ASSETS = ({"app.css", "chat.css", "studio.css", "halo-chat.css", "md.js", "studio-shell.js", "studio.js", "halo-chat.js", "mark.svg",
               "fonts/InterTight-normal.woff2", "motion.js", "halo-agents.js", "polish.css", "theme.js", "side-collapse.js",
               # the home-screen app (no service worker)
               "manifest.webmanifest", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"})

    def names(self):
        """This Mac's tailnet name as Tailscale reports it now (a rename in Tailscale follows within a minute), plus
        config.json's `studio_tailnet_hosts` (names to accept as well, e.g. halo.example-tailnet.ts.net while renaming)."""
        name, _ = tailnet_name(self.A.cfg)
        extra = {str(x).strip().lower().rstrip(".") for x in self.A.cfg.get("studio_tailnet_hosts") or []}
        return sorted(({name} if name else set()) | {x for x in extra if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", x)})

    def allowed_hosts(self):
        # a fake dashboard (previews, tests) also answers its own test port over plain HTTP; the live app never does
        port = self.server.server_address[1]
        return {h for n in self.names() for h in (n, f"{n}:443") + ((f"{n}:{port}",) if self.A.fake else ())}

    def origins(self):
        port = self.server.server_address[1]
        return {o for n in self.names() for o in (f"https://{n}",) + ((f"http://{n}:{port}",) if self.A.fake else ())}

    def host_ok(self):
        ip = self.client_address[0]
        return (ip.startswith("127.") or ip == "::1") and (self.headers.get("Host") or "").lower() in self.allowed_hosts()

    def expected_token(self):
        return self.A.tailnet_token

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "host not allowed"})
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self.send(200, studio_page(self.A.tailnet_token, self.private_set()), MIME[".html"])
        body = ui_bytes(u.path[4:], self.private_set()) if u.path.startswith("/ui/") and u.path[4:] in self.ASSETS else None
        if body is not None:
            return self.asset(body, MIME.get(os.path.splitext(u.path)[1], "application/octet-stream"))
        if u.path.startswith("/studio/"):
            return studio_http.get(self)
        return self.send(403, {"error": "the tailnet reaches StrixFold UI only"})

    def write(self, method):
        if not self.host_ok():
            return self.send(403, {"error": "host not allowed"})
        if urlparse(self.path).path.startswith("/studio/"):
            return studio_http.write(self, method)
        return self.send(403, {"error": "the tailnet reaches StrixFold UI only"})


PID_FILE = os.path.expanduser("~/Library/Caches/halo-control/server.pid")


def pid_file(fake, argv):
    """The real dashboard's PID file, which bin/halo-control (status, stop) and the landing scripts
    read; None for a fake or test dashboard on its own ports, which must never claim it. The server writes it itself,
    because launchd (`halo-control install-login`) starts it with no launcher to write it."""
    return None if fake or "--port" in argv else PID_FILE


def open_dir():
    """This Mac account's own cache folder outside the home folder (/var/folders/.../C, 0700): a VM or container given the
    home folder doesn't see it. ~/Library/Caches where macOS doesn't name one."""
    try:
        d = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        d = ""
    return os.path.join(d if d.startswith("/") else os.path.expanduser("~/Library/Caches"), "halo-control")


def write_open_key(A, port):
    """A new key, and a page that opens the app with it, readable by this Mac account only (0600, outside the home
    folder): the first passcode and the first screen's Skip need it, so another account on this Mac, which reaches
    127.0.0.1 too, can do neither. bin/halo-control opens the page, so the key is never in a command line `ps` shows
    other accounts. Made at start and again after each Skip (a copy in the browser's history is then dead). If it can't
    be written, nobody can set the first passcode or skip until it can: the log and the page say so."""
    A.open_key = secrets.token_urlsafe(24)
    where = getattr(A, "fake_root", None) if A.fake else open_dir()  # a fake app's in its own folder (tests, previews)
    if not where:
        return
    page = (f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=http://127.0.0.1:{port}/#k={A.open_key}">'
            f'<title>StrixFold UI</title><p><a href="http://127.0.0.1:{port}/#k={A.open_key}">Open</a></p>\n')
    p = os.path.join(where, f"open-{port}.html")
    tmp = p + ".new"
    try:
        os.makedirs(where, mode=0o700, exist_ok=True)
        if os.path.lexists(tmp):
            os.unlink(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(page)
        os.replace(tmp, p)
    except OSError as e:
        log(f"the page that opens the app with its key couldn't be written ({type(e).__name__}): the first passcode "
            f"and Skip wait until it can ({p})")


def write_pid(path):
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(f"{os.getpid()}\n")
    os.replace(tmp, path)


def serve(argv):
    cfg = load_config()
    fake = "--fake" in argv
    if "--port" in argv:
        cfg["ui_port"] = int(argv[argv.index("--port") + 1])
    if fake:
        cfg.pop("connect", None)  # (the fake boxes: connect.py's server has no part in them)
        cfg["halod_local_port"] = cfg.get("fake_halod_port", 8722)
    if "--endpoint-port" in argv:  # a second, fake dashboard beside the real one (tests, screenshots)
        cfg["endpoint_local_port"] = int(argv[argv.index("--endpoint-port") + 1])
    if "--halod-port" in argv:
        cfg["halod_local_port"] = int(argv[argv.index("--halod-port") + 1])
    if "--qwen-port" in argv:
        cfg["endpoint2_local_port"] = int(argv[argv.index("--qwen-port") + 1])
    if "--studio-root" in argv:  # a synthetic store for tests and screenshots
        cfg["studio_root_override"] = argv[argv.index("--studio-root") + 1]
    if fake or "--no-lan" in argv:
        cfg["lan"] = False  # test and screenshot dashboards stay on 127.0.0.1
    import chat_browse
    import chat_direct
    chat_direct.TARGETS.update(cfg.get("direct_targets") or {})
    chat_browse.HOST = cfg.get("browse_host")
    A = App(cfg, fake)
    Handler.A = A
    # Private chats: the running code is recorded now, and from here on a module from a user-writable folder
    # loads only if it was there, unchanged, at this moment (private_code.Guard)
    import private_code
    A.code_check = private_code.install(REPO, fake=fake)
    bind = "0.0.0.0" if cfg.get("lan") else "127.0.0.1"
    try:
        srv = ThreadingHTTPServer((bind, cfg["ui_port"]), Handler)
        if "--tailnet-port" in argv:  # tests: the tailnet listener on a free port, also with the fake backend
            cfg["studio_tailnet_port"] = int(argv[argv.index("--tailnet-port") + 1])
        if cfg.get("studio_tailnet_port") and (not fake or "--tailnet-port" in argv) and os.path.exists(cfg["tailscale"]):
            tsrv = ThreadingHTTPServer(("127.0.0.1", cfg["studio_tailnet_port"]), TailnetHandler)  # tailscale serve's target
            tsrv.daemon_threads = True
            threading.Thread(target=tsrv.serve_forever, name="tailnet", daemon=True).start()
    except OSError as e:
        sys.exit(f"halo-control: cannot listen on {bind}:{cfg['ui_port']}: {e}")
    srv.daemon_threads = True
    A.start()
    write_pid(pid_file(fake, argv))
    write_open_key(A, cfg["ui_port"])
    log(f"halo-control {VERSION} on http://127.0.0.1:{cfg['ui_port']}/{' (fake backend)' if fake else ''}")
    if cfg.get("lan"):
        log("home network: " + ", ".join(f"http://{n}:{cfg['ui_port']}/" for n in sorted(lan_names()))
            + " (private-range clients only; --no-lan turns it off)")

    def bye(*_):
        A.stop()
        os._exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve(sys.argv[2:])
    else:
        print(__doc__)

