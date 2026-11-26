"""Connect: the app on a StrixFold server that is already running, over SSH, and nothing else on the boxes.

    halo-control connect HOST[:PORT]   check it, remember it (config.json) and open the app on it
    halo-control disconnect            forget it

HOST is the box that serves (node0), as ssh knows it: a name from ~/.ssh/config or user@address. The app then makes one
SSH tunnel, to that box's 127.0.0.1:PORT (18080, as StrixFold serves), and reads the server's own /health and
/v1/models through it. Nothing is installed, started or stopped there. Stand answers on this Mac what halod would (its
state, the one model), so the rest of the app is as ever; what only halod can do (start, stop, switch, Private chats,
the boxes' readings) is refused in words. Team's agents take the server's free streams, one kept for the chats.

What answers on this Mac's end is believed only while the tunnel itself is up: another program on that port is never
taken for the server. The server's answers are read by one thread, with a deadline, a size limit and no redirects."""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = re.compile(r"(?![-.])[A-Za-z0-9_.@-]{1,200}")
PORT = 18080
HINT = "Start it on the boxes: first_light.sh up (StrixFold's docs/GETTING-STARTED.md, Serve)."
NOT_HERE = "The app is connected to a running StrixFold server: start, stop and change it on the boxes, with first_light.sh."
PRIVATE = "Private chats aren't there while the app is connected to a running StrixFold server: they need boxes pinned in the app's own code."
EVERY_S, READY_S, STALE_S = 3, 10, 40  # between two looks; once it is ready; a look this old no longer counts
DEADLINE_S, MAX_BYTES = 5, 64 * 1024
WINDOW = 8192  # StrixFold's own default: a server that is ready but names no window (earlier)
# nothing of the person's own ssh set-up beyond the host's entry: no shared or forwarded connection
SSH = [x for o in ("ControlMaster=no", "ControlPath=none", "ForwardAgent=no", "ForwardX11=no", "PermitLocalCommand=no", "StrictHostKeyChecking=yes") for x in ("-o", o)]
KEY_HELP = """Make a key for this and nothing else, in this terminal (it asks for a passphrase, and then once for the box's password):

  ssh-keygen -t ed25519 -f ~/.ssh/strixfold
  ssh-copy-id -i ~/.ssh/strixfold.pub -o PreferredAuthentications=keyboard-interactive,password {login}
  ssh-add --apple-use-keychain ~/.ssh/strixfold

Then give the box a name, at the end of ~/.ssh/config, and connect to that name:

  Host strixfold
      HostName {address}
      User {user}{sshport}
      IdentityFile ~/.ssh/strixfold
      IdentitiesOnly yes
      AddKeysToAgent yes
      UseKeychain yes

  ssh strixfold true && bin/halo-control connect strixfold{port}"""


def parse(text):
    """(host, port) of HOST[:PORT]; ValueError in words."""
    host, colon, port = (text if isinstance(text, str) else "").strip().partition(":")
    if not HOST.fullmatch(host) or host.count("@") > 1 or host.endswith("@"):
        raise ValueError("HOST is the box that serves, as ssh knows it: a name from ~/.ssh/config, or user@address")
    if colon and not (re.fullmatch(r"[0-9]{1,5}", port) and 0 < int(port) < 65536):
        raise ValueError("PORT is a number from 1 to 65535")
    return host, int(port or PORT)


def plain(x, n=120):
    """Text from elsewhere as one plain line."""
    return re.sub(r"[^ -~]+", " ", str(x)).strip()[:n]


def number(x, low, high, default):
    return x if isinstance(x, int) and not isinstance(x, bool) and low <= x <= high else default


def get(port, path, deadline=DEADLINE_S):
    """(status, JSON) of a GET to this Mac's own 127.0.0.1:port, or (None, None): whatever goes wrong, late, big or odd."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=deadline)
    ended = threading.Event()

    def end():  # whatever it is sending, headers or chunk sizes a byte at a time too
        ended.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer = threading.Timer(deadline, end)
    timer.daemon = True
    try:
        c.connect()
        sock = c.sock
        timer.start()
        c.request("GET", path, headers={"Authorization": "Bearer none", "Connection": "close"})
        r = c.getresponse()
        body = b""
        while not ended.is_set() and len(body) <= MAX_BYTES:
            piece = r.read1(8192)
            if not piece:
                return (None, None) if ended.is_set() else (r.status, json.loads(body) if r.status == 200 else None)
            body += piece
        return None, None
    except Exception:  # noqa: BLE001 - a server that isn't one is "not answering", whatever it sent
        return None, None
    finally:
        timer.cancel()
        c.close()


class Stand:
    """What halod would answer, from the tunnel's state and the server's own /health and /v1/models."""
    def __init__(self, cfg, host, tunnel):
        self.cfg, self.host, self.tunnel = cfg, host, tunnel
        self.port = cfg["endpoint_local_port"]
        self.now, self.why = None, "Connecting."  # the server as last read ({"model", "context", "streams"}), or why not
        self.windows, self.last = {}, None  # model -> the window it last named; the last of all
        self.since, self.at = time.time(), 0.0  # since when; when it was last read to the end

    def look(self):
        """Read the server once (one thread: loop)."""
        now, why = None, None
        state = getattr(self.tunnel, "state", None)
        if state == "blocked":
            why = f"Port {self.port} on this Mac is in use by another program. Close it, then this connects by itself."
        elif state == "starting":
            why = f"Connecting to {self.host}."
        elif state != "up":
            why = f"ssh can't reach {self.host}: {plain(getattr(self.tunnel, 'error', None) or 'no reason given')}"
        else:
            code, d = get(self.port, "/v1/models")
            data = d.get("data") if isinstance(d, dict) else None
            ids = [x["id"] for x in data if isinstance(x, dict) and isinstance(x.get("id"), str) and plain(x["id"])] if isinstance(data, list) else []
            hcode, h = get(self.port, "/health") if ids else (None, None)
            if code in (401, 403):
                why = f"The server on {self.host} asks for an API key, which the app doesn't send. Serve without one: it listens on that box only."
            elif not ids:
                why = f"StrixFold isn't answering on {self.host}. {HINT}"
            elif hcode != 200 or not isinstance(h, dict) or h.get("status") != "ok":
                why = f"StrixFold on {self.host} isn't ready{'' if hcode is None else ' (its /health says ' + plain((h or {}).get('status') if isinstance(h, dict) else hcode, 30) + ')'}."
            elif getattr(self.tunnel, "state", None) == "up":  # still the tunnel's end that answered
                self.windows[ids[0]] = number(h.get("context_length"), 1024, 1 << 23, self.windows.get(ids[0]) or WINDOW)
                self.last = self.windows[ids[0]]
                now = {"model": ids[0], "context": self.windows[ids[0]], "streams": number(h.get("max_batch_size"), 1, 256, 1)}
        if (now is None) != (self.now is None):
            self.since = time.time()
        self.now, self.why, self.at = now, why or self.why, time.time()

    def loop(self):
        while True:
            self.look()
            time.sleep(READY_S if self.now else EVERY_S)

    def window(self):
        """The server's window, in tokens (the last one while it doesn't answer); None until it has been read once."""
        return self.last

    def profile(self, s):
        """The chats' own default profile, at the window the server has."""
        import halo_chat
        p = dict(halo_chat._builtin()[halo_chat.DEFAULT_MODEL])
        return dict(p, name=halo_chat.MODEL_NAMES[p["model_id"]], settings=dict(p.get("settings") or {}, context=s["context"], streams=s["streams"]))

    def state(self):
        s, t = self.now, time.time()
        stale = t - self.at > STALE_S
        if getattr(self.tunnel, "state", None) != "up" or stale:
            s = None
        out = {"version": "connect", "fake": False, "connected": self.host, "t": t, "since": self.since, "hold": False, "intent": "run", "settings": {},
               "deploy": None, "steps": {}, "restarts": [], "copies": None, "boxes": {}, "timeouts": {}, "limits": {},
               "endpoint": f"http://127.0.0.1:{self.cfg['endpoint_port']}/v1", "idle": {"idle_min": 0, "hold": False}, "live": {}}
        if not s:
            return dict(out, state="failed", reason=(f"StrixFold on {self.host} stopped answering." if stale and self.now else self.why) or f"StrixFold isn't answering on {self.host}. {HINT}", profile=None, run_id=None,
                        served_model=None, health={"ok": False})
        p = self.profile(s)
        return dict(out, state="ready", reason=f"{p['name']} is serving on {self.host}", profile=p, settings=p["settings"], served_model=s["model"],
                    run_id=f"connect-{self.host}-{s['model']}-{p['settings']['context']}", health={"ok": True}, ready=self.since, started=self.since)

    def profiles(self):
        s = self.now
        return {"models": [], "profiles": [self.profile(s)] if s else [], "fields": {}, "max_context": s["context"] if s else 0}


class Handler(BaseHTTPRequestHandler):
    server_version = "connect"
    timeout = 10
    EMPTY = {"/api/requests": {"requests": []}, "/api/events": {"events": []}, "/api/locks": {"locks": {}},
             "/api/metrics": {"boxes": {}}, "/api/logs": {"lines": []}, "/api/deploys": {"deploys": []}}

    def log_message(self, *a):
        pass

    def reply(self, code, d):
        body = json.dumps(d).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def local(self):
        return (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        path, stand = self.path.split("?")[0], self.server.stand
        if not self.local():
            return self.reply(403, {"error": "host not allowed"})
        if path == "/api/ping":
            return self.reply(200, {"ok": True})
        if path == "/api/state":
            return self.reply(200, stand.state())
        if path == "/api/profiles":
            return self.reply(200, stand.profiles())
        return self.reply(200, self.EMPTY[path]) if path in self.EMPTY else self.reply(404, {"error": NOT_HERE})

    def refuse(self):
        n = self.headers.get("Content-Length") or "0"
        if re.fullmatch(r"[0-9]{1,7}", n):
            self.rfile.read(min(int(n), 1 << 20))
        self.reply(409 if self.local() else 403, {"error": NOT_HERE})

    do_POST = do_PUT = do_DELETE = do_PATCH = refuse


class Nowhere:
    """A tunnel's place for a config.json whose "connect" isn't a host."""
    state, error = "down", "config.json's \"connect\" isn't a host. Run connect again."

    def view(self):
        return {"name": "endpoint", "state": self.state, "error": self.error, "restarts": 0}

    def restart(self):
        pass

    stop = restart


def serve(app, stand):
    """The stand-in, on a port the system picks at the bind: nothing else can be on it when the app asks there."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads, srv.stand = True, stand
    app.stand, app.halod = srv, f"http://127.0.0.1:{srv.server_address[1]}"
    return srv.serve_forever


def start(app):
    """The tunnel and the stand-in, in place of halod's (App.start)."""
    import halo_chat
    import halo_control as H
    c = app.cfg
    try:
        host, rport = parse(c.get("connect"))
        t = H.Tunnel("endpoint", host, c["endpoint_local_port"], rport, c["ssh_opts"] + SSH)
    except ValueError as e:
        H.log(f"connect: {e}")
        host, t = "the box", Nowhere()
    app.halod_token = "connect"
    app.tunnels = [t]
    stand = Stand(c, host, t)
    halo_chat.CONNECTED = stand.window
    jobs = [(stand.loop, "connect-look"), (serve(app, stand), "connect")]
    if not isinstance(t, Nowhere):
        jobs += [(t.run, "tunnel-endpoint"), (app.wake_watch, "connect-wake")]
    for target, name in jobs:
        threading.Thread(target=target, name=name, daemon=True).start()


# ---------------------------------------------------------------- the command
def run(argv, timeout):
    """(exit status or None, stdout, stderr)."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout[:MAX_BYTES], p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return None, b"", ""


def check(host, port):
    """None when the app can connect, else what to do about it, in words."""
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *SSH]
    rc, _, err = run(["ssh", *opts, host, "true"], 30)
    last = plain((err.strip().splitlines() or ["no answer"])[-1], 200)
    if rc != 0 and ("ermission denied" in err or "Too many authentication failures" in err):
        _, out, _ = run(["ssh", *SSH, "-G", host], 10)  # what ssh makes of the name: its real address, user and port
        g = dict(x.split(" ", 1) for x in out.decode("utf-8", "replace").splitlines() if " " in x)
        address, user, sshport = plain(g.get("hostname") or host.rpartition("@")[2]), plain(g.get("user") or host.rpartition("@")[0] or "USER"), g.get("port") or "22"
        return f"{host} is there, but ssh can't log in to it by itself ({last}).\n" + KEY_HELP.format(
            login=("" if sshport == "22" else f"-p {plain(sshport)} ") + (f"-o ProxyJump={plain(g['proxyjump'])} " if g.get("proxyjump", "none") != "none" else "") + f"{user}@{address}", address=address, user=user,
            sshport=("" if sshport == "22" else f"\n      Port {plain(sshport)}") + (f"\n      ProxyJump {plain(g['proxyjump'])}" if g.get("proxyjump", "none") != "none" else ""), port="" if port == PORT else f":{port}")
    if rc != 0 and "ost key" in err:
        return f"This Mac doesn't know {host} yet, or its key changed ({last}).\nLog in once by hand, and look at the key it shows: ssh {host}"
    if rc != 0:
        return f"ssh can't reach {host} ({last}). Check the name or the address, and that the box is on."
    rc, out, err = run(["ssh", *opts, host, f"curl -sS -m 8 -w '\\n%{{http_code}}' http://127.0.0.1:{port}/v1/models"], 40)
    body, _, code = out.decode("utf-8", "replace").rpartition("\n")
    if rc == 127 or "curl: command not found" in err or "curl: not found" in err:
        return f"{host} has no curl, which StrixFold needs too. Install it there: sudo apt install curl"
    if code.strip() in ("401", "403"):
        return f"The server on {host} asks for an API key, which the app doesn't send. Serve without one: it listens on that box only."
    try:
        ids = [plain(x["id"]) for x in json.loads(body)["data"] if isinstance(x["id"], str) and plain(x["id"])]
    except (ValueError, KeyError, TypeError):
        ids = []
    if rc != 0 or code.strip() != "200" or not ids:
        return f"{host} answers, but no StrixFold server does on its 127.0.0.1:{port}. {HINT}"
    print(f"Found {', '.join(ids[:4])} on {host}.")
    return None


def save(value):
    """None when config.json now says so, else why not. The rest of it, its place (a link) and its mode stay."""
    import halo_control as H
    os.makedirs(H.CONF_DIR, mode=0o700, exist_ok=True)
    p = os.path.realpath(os.path.join(H.CONF_DIR, "config.json"))
    cfg, mode = {}, 0o600
    if os.path.exists(p):
        try:
            with open(p) as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            cfg = e
        if not isinstance(cfg, dict):
            return f"{p} can't be read as settings ({plain(cfg)}): mend it or move it away first"
        mode = os.stat(p).st_mode & 0o777
    if value is None:
        cfg.pop("connect", None)
    else:
        cfg["connect"] = value
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".config-")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(tmp, mode)
    os.replace(tmp, p)
    return None


def main(argv):
    if argv[:1] == ["disconnect"]:
        why = save(None)
        print(why or "Disconnected: the next start uses the app's own boxes.", file=sys.stderr if why else sys.stdout)
        return 1 if why else 0
    try:
        host, port = parse(argv[1] if len(argv) > 1 else "")
    except ValueError as e:
        print(f"connect HOST[:PORT]: {e}", file=sys.stderr)
        return 2
    why = check(host, port) or save(host if port == PORT else f"{host}:{port}")
    if why:
        print(why, file=sys.stderr)
    return 1 if why else 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main(sys.argv[1:]))
