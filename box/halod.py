#!/usr/bin/env python3
"""halod: the StrixFold UI box agent. It runs on box2 as a systemd --user service, listens on 127.0.0.1 only, and
serves one model at a time on the two boxes through the deploy's own rank scripts and the team's GPU lock protocol.

State machine: idle -> waiting (for the GPU locks) -> loading -> linking -> ready -> stopping -> idle, plus failed.
Every step has a timeout. Start and stop are idempotent and single-flight (one worker thread runs them). Every
transition goes into the audit log (SQLite events). A dead rank, a stall or a lost USB4 link stops both ranks,
releases both locks and restarts the profile, at most 3 times in 30 minutes; then it stays stopped with the reason.
After a halod restart it re-adopts running ranks from their pid files. Idle auto-stop frees the GPUs for the team.
halod never starts a model by itself at boot. Python 3.9+ standard library only.

Usage: halod.py [--config FILE]   (defaults: ~/halo-control/halod.json if present)
"""
import collections
import json
import os
import queue
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe  # noqa: E402
import profiles  # noqa: E402

VERSION = "1.1"
STATES = ("idle", "waiting", "loading", "linking", "ready", "stopping", "failed")
ACTIVE = ("waiting", "loading", "linking", "ready")
LEGACY_BOXES = ("box1", "box2")  # the built-in profiles' names for rank 0 and rank 1 (HaloD.on_boxes)
RESTART_MAX, RESTART_WINDOW_S = 3, 1800


def now():
    return time.time()


def default_config():
    home = os.path.expanduser("~")
    return {
        "listen_host": "127.0.0.1", "listen_port": 8710,
        "home": os.path.join(home, "halo-control"),
        "deploy_root": "$HOME/tfp/deploy",          # on each box
        "run_root": "$HOME/halo-control/runs",      # on each box (the conf's RUN_ROOT)
        "probe": "$HOME/halo-control/box/probe.py",  # on each box
        "lock": "/var/tmp/halo-gpu.lock",
        "quiet_dir": "/var/tmp/halo-quiet.d",
        "boxes": [  # rank order: rank 0 (the server) first
            {"name": "box1", "rank": 0, "exec": [["ssh", "box1"], ["ssh", "box1-a"]]},
            {"name": "box2", "rank": 1, "exec": [[]]},
        ],
        "ssh_opts": ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"],
        "endpoint": "http://127.0.0.1:18080",       # rank 0's server, on rank 0's box
        "fake": None,                               # the fake backend's name (probe telemetry), tests and UI work
        "timeouts": {"waiting": 3 * 3600, "loading": 900, "linking": 300, "verify": 240, "stopping": 300},
        # the lock protocol: wait in the kernel's queue for box1 like the team's scripts (flock -w), then box2 within
        # lock_pair_s, else release box1 and retry the pair after lock_backoff_s (serve.conf LOCK_PAIR_S: 60, 30 s)
        "lock_try_s": 1800, "lock_pair_s": 60, "lock_backoff_s": [30],
        "health_fail_s": 60, "stall_s": 240, "link_loss_s": 10,
        "probe_every_s": 600, "probe_after_idle_s": 300,
        "tick_s": 2.0,
        "minute_s": 60,                             # a profile's idle minutes, in seconds (tests shorten it)
        # Max speed (a profile's power "max"): the platform profile written on both boxes while it serves
        "power_path": "/sys/firmware/acpi/platform_profile",
        "power_write": "echo {value} | sudo -n tee {path} >/dev/null",
        # sf-disk: this pair's disk for kept prompt states (each box; 0: off) and where (profiles.prompt_disk)
        "prompt_disk_gib": 0, "prompt_disk_dir": "$HOME/strixfold-cache/prefix",
    }


# ---------------------------------------------------------------- boxes

class Box:
    """One machine: shell commands on it (ssh or local) and its probe stream (a long-lived subprocess)."""

    def __init__(self, cfg, spec, on_event, log):
        self.cfg, self.name, self.rank = cfg, spec["name"], spec["rank"]
        self.lock_path = spec.get("lock") or cfg["lock"]
        self.execs = spec["exec"]
        self.on_event, self.log = on_event, log
        self.proc = None
        self.tick = None            # the latest tick record
        self.tick_t = 0.0
        self.connected = False
        self.last_error = None
        self.watch = None           # the watch command to (re)send
        self.lock = threading.Lock()
        self.history = collections.deque(maxlen=450)  # (t, telemetry) every tick: 15 minutes at 2 s
        self.exec_i = 0
        self.closed = False
        threading.Thread(target=self._stream_loop, name=f"probe-{self.name}", daemon=True).start()

    def argv(self, cmd, i=None):
        ex = self.execs[self.exec_i if i is None else i]
        if ex and ex[0] == "ssh":
            return ["ssh", "-n"] + self.cfg["ssh_opts"] + ex[1:] + [cmd]
        return list(ex) + ["bash", "-c", cmd]

    def run(self, cmd, timeout=60, stdin=None, cancel=None):
        """(returncode, output) of a shell command on this box; 255/124 when unreachable or timed out, 130 when
        cancel (an Event) was set. Tries each exec route (ssh alias) in turn when the first cannot connect. The
        command runs in its own process group, which a timeout or a cancel kills whole (a local flock waiter goes
        with it; a remote one ends at its own -w, and halod's sweeper releases any holder it leaves)."""
        last = (255, "")
        for i in range(len(self.execs)):
            idx = (self.exec_i + i) % len(self.execs)
            argv = self.argv(cmd, idx)
            if stdin is not None and argv[:2] == ["ssh", "-n"]:
                argv = ["ssh"] + argv[2:]
            p = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            chunks = []
            reader = threading.Thread(target=lambda: chunks.append(p.stdout.read()), daemon=True)
            reader.start()
            if stdin is not None:
                try:
                    p.stdin.write(stdin)
                    p.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            t0, why = time.monotonic(), None
            while p.poll() is None:
                if time.monotonic() - t0 > timeout:
                    why = (124, f"(timed out after {timeout}s)")
                elif cancel is not None and cancel.is_set():
                    why = (130, "(cancelled)")
                if why:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    p.wait()
                    break
                time.sleep(0.1)
            reader.join(5)
            try:
                p.stdout.close()
            except OSError:
                pass
            out = b"".join(c for c in chunks if c).decode("utf-8", "replace")
            if why:
                return why[0], out + "\n" + why[1]
            p.returncode = p.wait()
            if p.returncode == 255 and argv[0] == "ssh":
                last = (255, out)
                continue
            self.exec_i = idx
            return p.returncode, out
        return last

    def write(self, path, data, timeout=30):
        d = os.path.dirname(path)
        rc, out = self.run(f"mkdir -p {shlex.quote(d)} && cat > {shlex.quote(path)}.tmp && mv {shlex.quote(path)}.tmp {shlex.quote(path)}",
                           timeout=timeout, stdin=data if isinstance(data, bytes) else data.encode())
        if rc != 0:
            raise RuntimeError(f"cannot write {path} on {self.name}: {out.strip()[-300:]}")

    # -- the probe stream
    def send(self, rec):
        with self.lock:
            p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.write((json.dumps(rec) + "\n").encode())
                p.stdin.flush()
                return True
            except (BrokenPipeError, OSError):
                return False
        return False

    def set_watch(self, w):
        self.watch = w
        self.send(w if w else {"cmd": "unwatch"})

    def close(self):
        self.closed = True
        with self.lock:
            p = self.proc
        if p and p.poll() is None:
            p.kill()

    def _stream_loop(self):
        backoff = 1
        while not self.closed:
            pr = self.cfg["probe"]
            args = f"exec python3 {pr} stream --interval {self.cfg['tick_s']} --lock {shlex.quote(self.lock_path)} --quiet-dir {shlex.quote(self.cfg['quiet_dir'])}"
            if self.cfg.get("fake"):
                args += f" --fake {self.name}"
            argv = self.argv(args)
            if argv[:2] == ["ssh", "-n"]:
                argv = ["ssh"] + argv[2:]
            t0 = now()
            try:
                p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as e:
                self.last_error = str(e)
                time.sleep(backoff)
                backoff = min(30, backoff * 2)
                continue
            with self.lock:
                self.proc = p
            if self.watch:
                self.send(self.watch)
            err_lines = collections.deque(maxlen=5)
            def errs(p=p, err_lines=err_lines):
                for line in p.stderr:
                    err_lines.append(line.decode("utf-8", "replace").strip())
                    if os.environ.get("HALOD_DEBUG"):
                        sys.stderr.write(f"[{self.name} probe] " + line.decode("utf-8", "replace"))
            threading.Thread(target=errs, daemon=True).start()
            for raw in p.stdout:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                w = self.watch
                if w and rec.get("type") in ("log", "done") and rec.get("offset") is not None:
                    w["offset"] = rec["offset"]  # a reconnect resumes the log where this left it
                    if rec.get("type") == "done":
                        w["done"] = rec.get("n", w.get("done", 0))
                if rec.get("type") == "tick":
                    self.tick, self.tick_t = rec, now()
                    if not self.connected:
                        self.connected = True
                        self.on_event(self, {"type": "connected"})
                    tel = rec.get("telemetry") or {}
                    self.history.append((rec.get("t", now()), {k: tel.get(k) for k in ("ppt_w", "gpu_temp_c", "sclk_mhz", "mem_available_gib", "gtt_used_gib", "gpu_busy_pct")}))
                    backoff = 1
                self.on_event(self, rec)
            p.wait()
            with self.lock:
                self.proc = None
            if self.connected:
                self.connected = False
                self.on_event(self, {"type": "disconnected"})
            self.last_error = " | ".join(err_lines) or f"probe exited ({p.returncode})"
            if now() - t0 > 60:
                backoff = 1
            if len(self.execs) > 1 and now() - t0 < 10:
                self.exec_i = (self.exec_i + 1) % len(self.execs)
            time.sleep(backoff)
            backoff = min(30, backoff * 2)

    def fresh(self, max_age=10):
        return self.connected and self.tick is not None and now() - self.tick_t < max_age


# ---------------------------------------------------------------- engine adapters

class TensorFoldAdapter:
    """TensorFold's TP2 DeepSeek-V4.1 port: the deploy's own rank scripts (serve-rank0.sh on rank 0's box, the
    server; serve-rank1.sh on rank 1's box), its serve.conf, its lock subcommands, and the log lines that mark each
    phase. Another engine (Gufo for Qwen3.8 Flash-Next) slots in as another adapter with these members; the
    profiles, the state machine and the dashboard stay as they are."""
    name = "tensorfold-dsv41"
    installed = True
    # (rank, text) in a rank's log that ends a phase: weights read (loading -> linking), rank 1 linked, serving
    loaded = (0, "rank cache:")
    linked = (1, '"loaded_s"')
    serving = (0, "serving ")

    def base_conf(self, root, profile, deploy):
        rel = (profiles.model(profile.get("model_id") or "") or {}).get("conf_rel") or "tools/dsv41-hip/serve.conf"
        return profile.get("base_conf") or f"{root}/{deploy}/{rel}"

    def command(self, root, rank, args, run_id, run_dir, deploy):
        tools = f"{root}/{deploy}/tools/dsv41-hip"
        return f"RUN_ID={shlex.quote(run_id)} TF_SERVE_CONF={run_dir}/serve.conf {tools}/serve-rank{rank}.sh {args}"


class ExternalAdapter:
    """An engine with its own start and stop commands that takes the GPU locks itself (Gufo through the team's
    halo-qwen, Kyojin through halo-qwen-kyojin, Halogen through the same launcher's halo-qwen-halogen). The model's "external" entry names the commands: up, down,
    the box it serves on and its port, its state file, its systemd --user unit and its log; optionally "boxes" (one
    server per box: every one must answer) and "ready_path" (default /ready). halod starts `up` in the background on
    its own box, follows the state file and the server's ready path, checks one greedy request on each serving box,
    and runs `down` to stop, cancel or clean up."""
    external = True
    installed = True

    def __init__(self, name):
        self.name = name


class NotInstalledAdapter:
    installed = False

    def __init__(self, name):
        self.name = name


ADAPTERS = {"tensorfold-dsv41": TensorFoldAdapter(), "gufo": ExternalAdapter("gufo"), "kyojin": ExternalAdapter("kyojin"),
            "gufo21": ExternalAdapter("gufo21"), "halogen": ExternalAdapter("halogen")}


def adapter(engine):
    return ADAPTERS.get(engine) or NotInstalledAdapter(engine)


# ---------------------------------------------------------------- the store

class DB:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, t REAL, kind TEXT, state_from TEXT, state_to TEXT,
                run_id TEXT, profile TEXT, actor TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, t REAL, run_id TEXT, profile TEXT, prompt INT,
                cached INT, tokens INT, thinking INT, finish TEXT, rounds INT, accepted INT, drafted INT, ttft_s REAL,
                prefill_s REAL, decode_s REAL, latency_s REAL, approx INT, probe INT DEFAULT 0, sha TEXT);
            CREATE INDEX IF NOT EXISTS requests_t ON requests(t);
            CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, profile TEXT, deploy TEXT, started REAL,
                ready REAL, ended REAL, end_reason TEXT, settings TEXT, steps TEXT);
        """)
        for col in ("deploy_root TEXT", "model_id TEXT"):  # added after 1.0
            try:
                self.c.execute(f"ALTER TABLE runs ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.c.commit()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur

    def q(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------- the controller

class Cancelled(Exception):
    pass


class Failure(Exception):
    """A run failed: both ranks stop and the locks go; it may be restarted."""


class Refused(Exception):
    """A start refused before anything was taken (an invalid profile, too little memory): no restart."""


class Halod:
    def __init__(self, cfg):
        self.cfg = cfg
        os.makedirs(cfg["home"], exist_ok=True)
        self.state_path = os.path.join(cfg["home"], "state.json")
        self.db = DB(os.path.join(cfg["home"], "halod.sqlite"))
        self.store = profiles.Store(os.path.join(cfg["home"], "profiles.json"))
        self.token = self._token()
        self.mu = threading.RLock()
        self.cmdq = queue.Queue()
        self.cancel = threading.Event()
        self.chat_waiters = {}
        self.s = self._load_state()
        # reply ids of halod's own requests (first request, idle probes), kept across restarts
        self.probe_ids = collections.deque(self.s.get("probe_ids") or [], maxlen=64)
        self.boxes = [Box(cfg, spec, self._on_box_event, self.event) for spec in cfg["boxes"]]
        self.health = {"ok": None, "since": now(), "last_ok": None, "data": None, "error": None}
        self.live = {"connections": 0, "busy_since": None, "last_busy": None}
        self.link_down_since = None
        self.closed = False
        self.installs = {}     # {model id: {"ok", "why"}} from check_installs
        self._kit = {}         # {(model id, deploy): (time, longest window)}: kit_limit's cache
        self.installs_mu = threading.Lock()
        self.ext_checked, self.ext_ok, self.ext_count, self.ext_box_ok = 0.0, None, None, {}
        self.conf_cache = {}   # {path: (t, text)} base confs read for the Advanced section
        self._after_lock0 = None  # StrixFold UI: called once a start holds box1's lock (its box2 holder lets go)
        self.worker = threading.Thread(target=self._worker, name="worker", daemon=True)
        self.monitor = threading.Thread(target=self._monitor, name="monitor", daemon=True)

    # -- persistence
    def _token(self):
        p = os.path.join(self.cfg["home"], "token")
        if not os.path.exists(p):
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        return open(p).read().strip()

    def _load_state(self):
        base = {"state": "idle", "since": now(), "profile": None, "run_id": None, "settings": None, "deploy": None,
                "reason": None, "hold": False, "restarts": [], "steps": {}, "started": None, "ready": None,
                "last_activity": None, "offsets": {}, "done": 0, "metrics_prev": None, "intent": None, "attempt": 0}
        try:
            with open(self.state_path) as f:
                base.update(json.load(f))
        except (OSError, ValueError):
            pass
        return base

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.s, f, indent=1)
        os.replace(tmp, self.state_path)

    def event(self, kind, detail="", actor="halod", frm=None, to=None):
        with self.mu:
            self.db.x("INSERT INTO events (t, kind, state_from, state_to, run_id, profile, actor, detail) VALUES (?,?,?,?,?,?,?,?)",
                      (now(), kind, frm, to, self.s.get("run_id"), self.s.get("profile"), actor, detail))

    def set_state(self, st, reason=None, actor="halod"):
        with self.mu:
            old = self.s["state"]
            self.s["state"], self.s["since"] = st, now()
            if reason is not None:
                self.s["reason"] = reason
            if st in ("waiting", "loading", "linking", "ready", "stopping"):
                self.s["steps"].setdefault(st, now())
            self._save()
        self.event("state", reason or "", actor, old, st)

    # -- box events (probe stream threads)
    def _on_box_event(self, box, rec):
        t = rec.get("type")
        if t == "connected":
            self.event("box", f"{box.name}: probe connected")
        elif t == "disconnected":
            self.event("box", f"{box.name}: probe lost ({box.last_error or 'no reason'}); reconnecting")
        elif t == "tick" and box.rank == 0 and "health" in rec:
            self._on_health(rec)
        elif t == "done":
            self._on_done(box, rec)
        elif t == "log":
            with self.mu:
                if rec.get("offset") is not None:
                    self.s["offsets"][box.name] = rec["offset"]
        elif t == "chat":
            w = self.chat_waiters.get(rec.get("id"))
            if w:
                if w.get("probe") and rec.get("ok") and rec["result"].get("id"):
                    with self.mu:  # before the waiter goes: its done line may be next
                        self.probe_ids.append(rec["result"]["id"])
                        self.s["probe_ids"] = list(self.probe_ids)
                        self._save()
                self.chat_waiters.pop(rec.get("id"), None)
                w["rec"] = rec
                w["ev"].set()

    def _on_health(self, rec):
        h = rec.get("health")
        with self.mu:
            ok = h is not None and h.get("status") == "ok"
            if ok != self.health["ok"]:
                self.health["since"] = now()
            self.health.update(ok=ok, data=h, error=rec.get("health_error"))
            if ok:
                self.health["last_ok"] = now()
                live = h.get("live") or {}
                c = live.get("connections") or 0
                self.live["connections"] = c
                if c > 0:
                    self.live["last_busy"] = now()
                    self.live["busy_since"] = self.live["busy_since"] or now()
                    if not self.probe_inflight():
                        self.s["last_activity"] = now()
                else:
                    self.live["busy_since"] = None
                self.live["decode_tps"] = live.get("decode_tokens_per_second")
                self.live["prefill_tps"] = live.get("prefill_tokens_per_second")
                if c > 0 and (self.live["decode_tps"] or self.live["prefill_tps"]):
                    self.live["progress_at"] = now()  # the stall rule's last sign of progress

    def probe_inflight(self):
        return any(w.get("probe") for w in self.chat_waiters.values())

    def _on_done(self, box, rec):
        d, m = rec["req"], rec.get("metrics") or {}
        with self.mu:
            self.live["done_at"] = now()  # a request ended: the next one starts with a first read of its own
            prev = self.s.get("metrics_prev") or {}
            approx = 0
            vals = {}
            for key, name in (("time_to_first_token_seconds", "ttft_s"), ("request_prefill_seconds", "prefill_s"),
                              ("request_decode_seconds", "decode_s"), ("request_latency_seconds", "latency_s")):
                dn = m.get(key + "_count", 0) - prev.get(key + "_count", 0)
                ds = m.get(key + "_sum", 0) - prev.get(key + "_sum", 0)
                vals[name] = ds / dn if dn >= 1 else None
                if dn > 1:
                    approx = 1
            if m:
                self.s["metrics_prev"] = {k: v for k, v in m.items() if k.endswith(("_sum", "_count"))}
            self.s["done"] = rec.get("n", self.s.get("done", 0))
            if rec.get("offset") is not None:
                self.s["offsets"][box.name] = rec["offset"]
            probe_req = d["id"] in self.probe_ids or (self.probe_inflight() and d["prompt"] <= 64)
            if not probe_req:
                self.s["last_activity"] = now()
            run_id, prof = self.s.get("run_id"), self.s.get("profile")
            self._save()
        self.db.x("INSERT OR REPLACE INTO requests (id, t, run_id, profile, prompt, cached, tokens, thinking, finish, rounds, "
                  "accepted, drafted, ttft_s, prefill_s, decode_s, latency_s, approx, probe, sha) VALUES "
                  "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (d["id"], rec.get("t", now()), run_id, prof, d["prompt"], d["cached"], d["tokens"], int(d["thinking"]),
                   d["finish"], d["rounds"], d["accepted"], d["drafted"], vals["ttft_s"], vals["prefill_s"],
                   vals["decode_s"], vals["latency_s"], approx, int(probe_req), d["sha"]))

    # -- paths and commands of a run
    def box_home_path(self, p):
        return p  # paths are written with $HOME and expanded by each box's shell

    def run_dir(self, run_id=None):
        return f"{self.cfg['run_root']}/{run_id or self.s['run_id']}"

    def adapter(self, pid=None):
        p = self.store.get(pid or self.s.get("profile")) or {}
        return adapter(p.get("engine", "tensorfold-dsv41"))

    def deploy_root(self, mid):
        """Where a model's deploys live on each box (the official one in cfg deploy_root, the others their own)."""
        roots = self.cfg.get("deploy_roots") or {}
        if mid in roots:
            return roots[mid]
        m = profiles.model(mid) or {}
        return self.cfg["deploy_root"] if mid == "dsv41" or not m.get("deploy_root") else m["deploy_root"]

    def rank_cmd(self, rank, args, timeout=120, run_id=None, deploy=None, cancel=None, root=None):
        box = self.boxes[rank]
        run_id = run_id or self.s["run_id"]
        deploy = deploy or self.s["deploy"]
        root = root or self.s.get("deploy_root") or self.cfg["deploy_root"]
        cmd = self.adapter().command(root, rank, args, run_id, self.run_dir(run_id), deploy)
        rc, out = box.run(cmd, timeout=timeout, cancel=cancel)
        if rc == 1 and not out.strip() and args.startswith("lock "):
            # serve_lib's serve_lock_hold: `pkill -P` finds no child under set -e and exits 1 before its die message
            out = f"(no message: the GPU lock on {box.name} was not free within {args.split()[1]} s)"
        self.event("cmd", f"{box.name}: serve-rank{rank}.sh {args} -> {rc}: {out.strip()[-300:]}")
        return rc, out

    def deploys(self, mid="dsv41"):
        """{name: commit dir} of a model's deploys present on every box ('current' resolved per box; it must agree)."""
        found = []
        root = self.deploy_root(mid)
        for b in self.boxes:
            rc, out = b.run(f"cd {root} 2>/dev/null && for d in *; do [ -f \"$d/DEPLOY.json\" ] && "
                            f"echo \"$d $(readlink \"$d\" || echo -)\"; done; true", timeout=20)
            if rc != 0:
                raise RuntimeError(f"cannot list deploys on {b.name}: {out.strip()[-200:]}")
            found.append(dict(l.split(" ", 1) for l in out.split("\n") if " " in l))
        common = set(found[0])
        for f in found[1:]:
            common &= set(f)
        allow = (profiles.model(mid) or {}).get("deploys")
        if allow:  # a deploy root shared with other models: only the deploys this model can run
            common &= set(allow)
        out = {}
        for d in sorted(common):
            targets = {f[d] for f in found}
            if len(targets) == 1:
                t = targets.pop()
                out[d] = d if t == "-" else os.path.basename(t)
        return out

    # -- API commands (any thread): they queue work for the single worker
    def start(self, pid, replace=False, actor="api"):
        p = self.store.get(pid)
        if not p:
            raise KeyError(pid)
        if not p.get("installed", True):
            raise profiles.Invalid({"profile": p.get("missing") or "not installed"})
        inst = self.installs.get(p["model_id"])
        if not inst or not inst["ok"]:  # a stale result (a deploy switching `current` mid-check): look again now
            inst = self.check_installs().get(p["model_id"])
        if inst and not inst["ok"]:
            raise profiles.Invalid({"profile": f"{p['name']}: {inst['why']}"})
        # a window longer than its deploy's RoPE kit (DeepSeek) or its engine (a community Qwen engine) serves:
        # refused here, before anything running is stopped
        ctx, dep = int((p.get("settings") or {}).get("context") or 0), (p.get("settings") or {}).get("deploy")
        own = profiles.model_window(p["model_id"])
        if own is not None:
            if ctx > own:
                raise profiles.Invalid({"context": f"{p['name']} needs a {ctx:,}-token window; its engine serves at most "
                                                   f"{own:,}. Nothing was stopped."})
        elif ctx > profiles.ROPE_ROWS:
            lim = self.kit_limit(p["model_id"], dep, fresh=True)
            if ctx > lim:
                raise profiles.Invalid({"context": f"{p['name']} needs a {ctx:,}-token window; its deploy ({dep}) serves at most "
                                                   f"{lim // profiles.ALIGN * profiles.ALIGN:,} (its RoPE kit). Nothing was stopped."})
        with self.mu:
            st = self.s["state"]
            if st in ACTIVE and self.s["profile"] == pid and self.s.get("intent") != "stop":
                return {"ok": True, "note": f"already {st}"}
            if self.s.get("pending") == pid and self.s.get("intent") == "run":
                return {"ok": True, "note": "already starting"}
            if self.s.get("pending") and self.s.get("intent") == "run" and not replace:
                raise profiles.Invalid({"state": f"{self.s['pending']} is starting; stop it first or switch"})
            if st in ACTIVE + ("stopping",) and not replace:
                cur = self.store.get(self.s["profile"])
                raise profiles.Invalid({"state": f"{cur['name'] if cur else self.s['profile']} is {st}; stop it first or switch"})
            self.s["intent"] = "run"
            self.s["pending"] = pid
            self.s["restarts"] = []  # a start by hand begins a fresh restart budget
            self._save()
        if st in ACTIVE:
            self.cancel.set()
        self.cmdq.put(("start", pid, actor))
        self.event("api", f"start {pid}{' (switch)' if replace and st in ACTIVE else ''}", actor)
        return {"ok": True}

    def stop(self, reason="stopped from the dashboard", actor="api"):
        with self.mu:
            st = self.s["state"]
            self.s["intent"] = "stop"
            self.s["pending"] = None
            self._save()
        self.event("api", f"stop ({reason})", actor)
        if st in ("idle", "failed") and not self._strays_possible():
            return {"ok": True, "note": f"already {st}"}
        self.cancel.set()
        self.cmdq.put(("stop", reason, actor))
        return {"ok": True}

    def restart(self, actor="api"):
        with self.mu:
            pid = self.s["profile"]
        if not pid:
            raise profiles.Invalid({"state": "nothing to restart"})
        self.cancel.set()
        with self.mu:
            self.s["intent"] = "run"
            self._save()
        self.cmdq.put(("restart", pid, actor))
        self.event("api", f"restart {pid}", actor)
        return {"ok": True}

    def hold(self, on, actor="api"):
        with self.mu:
            self.s["hold"] = bool(on)
            self._save()
        self.event("hold", "keep running: on" if on else "keep running: off (idle stop applies)", actor)
        if self.is_external():
            self._apply_hold(self.ext(), on)
        return {"ok": True, "hold": bool(on)}

    def _apply_hold(self, ext, on):
        """An engine that stops itself when idle reads keep running from hold_file on each of its boxes. Its node removes
        the file when it takes the lock, so a hold set before the start or during the lock wait is written again once
        the run is ready (_start_external)."""
        if not (ext and ext.get("hold_file")):
            return
        for name in ext.get("boxes") or [ext["box"]]:
            rc, out = self.box_named(name).run((f"touch {ext['hold_file']}" if on else f"rm -f {ext['hold_file']}"), timeout=20)
            if rc != 0:
                self.event("error", f"{name}: keep running not passed to the engine: {out.strip()[-200:]}")

    def _strays_possible(self):
        return bool(self.s.get("run_id"))

    # -- the worker
    def _worker(self):
        self._adopt()
        while not self.closed:
            try:
                cmd = self.cmdq.get(timeout=1)
            except queue.Empty:
                if self.closed:
                    break
                continue
            self.cancel.clear()
            kind = cmd[0]
            # a burst of commands: only the latest intent counts
            while True:
                try:
                    nxt = self.cmdq.get_nowait()
                    cmd, kind = nxt, nxt[0]
                except queue.Empty:
                    break
            try:
                if kind in ("start", "restart"):
                    if self.s["state"] in ACTIVE or kind == "restart":
                        self._stop_run("switching profile" if kind == "start" else "restart requested")
                    if self.s.get("intent") == "run":
                        self._run_profile(cmd[1], actor=cmd[2])
                elif kind == "stop":
                    if self.s.get("run_id") or self.s["state"] not in ("idle", "failed"):
                        self._stop_run(cmd[1], final_state="idle")
                elif kind == "fail":
                    self._handle_failure(cmd[1])
            except Exception:  # noqa: BLE001 - the worker must survive anything
                self.event("error", traceback.format_exc()[-1500:])
                try:
                    self._stop_run("internal error", final_state="failed")
                except Exception:  # noqa: BLE001
                    self.event("error", traceback.format_exc()[-1500:])

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _sleep(self, s):
        if self.cancel.wait(s):
            raise Cancelled()

    def _run_profile(self, pid, actor="api", attempt=0):
        """Start pid; on failure restart within the limit. Returns when ready, stopped or failed."""
        while True:
            try:
                self._start_once(pid, actor)
                return
            except Cancelled:
                self._stop_run("cancelled", final_state="idle" if self.s.get("intent") == "stop" else None)
                return
            except Refused as e:
                self._stop_run(str(e), final_state="failed")
                return
            except Failure as e:
                if not self._handle_failure(str(e), restart_inline=True):
                    return

    def _handle_failure(self, reason, restart_inline=False):
        """Stop both, release the locks; restart within the limit (returns True when the caller should start again)."""
        pid = self.s["profile"]
        external = self.is_external()  # halo-qwen's failures repeat; its starts are not retried automatically
        self._stop_run(reason, final_state=None)
        with self.mu:
            t = now()
            self.s["restarts"] = [r for r in self.s["restarts"] if t - r < RESTART_WINDOW_S]
            if self.s.get("intent") == "run" and len(self.s["restarts"]) < RESTART_MAX and not external:
                self.s["restarts"].append(t)
                n = len(self.s["restarts"])
                self._save()
                self.event("restart", f"auto-restart {n} of {RESTART_MAX} after: {reason}")
                self.s["reason"] = f"restarting ({n} of {RESTART_MAX}) after: {reason}"
                if restart_inline:
                    return True
                self.cmdq.put(("start", pid, "auto-restart"))
                return False
            self.s["intent"] = "stop"
            self._save()
        msg = (f"{reason} (stopped; this engine is not restarted automatically)" if external else
               f"stopped after {RESTART_MAX} restarts in 30 minutes; last failure: {reason}" if self.s["restarts"] else reason)
        self.set_state("failed", msg)
        return False

    def _new_run(self, p, settings, deploy, root=None):
        rid = time.strftime("hc-%Y%m%d-%H%M%S", time.localtime()) + "-" + p["id"][:24]
        with self.mu:
            self.s.update(pending=None, profile=p["id"], run_id=rid, settings=settings, deploy=deploy, steps={}, started=now(),
                          ready=None, last_activity=None, offsets={}, done=0, metrics_prev=None, model_id=p["model_id"],
                          deploy_root=root or self.cfg["deploy_root"])
            self._save()
        self.db.x("INSERT OR REPLACE INTO runs (run_id, profile, deploy, started, settings, deploy_root, model_id) VALUES (?,?,?,?,?,?,?)",
                  (rid, p["id"], deploy, now(), json.dumps(settings), root or self.cfg["deploy_root"], p["model_id"]))
        return rid

    def _start_once(self, pid, actor):
        p = self.store.get(pid)
        if not p:
            raise Refused(f"no profile {pid}")
        if getattr(adapter(p["engine"]), "external", False):
            return self._start_external(p, actor)
        # resolve: deploy and conf (Refused: nothing taken yet)
        try:
            deploys = self.deploys(p["model_id"])
        except RuntimeError as e:
            raise Failure(str(e))
        settings = dict(p["settings"])
        try:
            settings = profiles.validate_settings(settings, set(deploys) | ({"conf"} if p.get("base_conf") else set()),
                                                  model_id=p["model_id"], max_rows=self.kit_limit(p["model_id"], settings.get("deploy")))
        except profiles.Invalid as e:
            raise Refused(f"profile {p['name']}: {e}")
        ad = adapter(p["engine"])
        if not ad.installed:
            raise Refused(f"{p['name']}: the {p['engine']} engine is not installed on the boxes")
        root = self.deploy_root(p["model_id"])
        try:
            base_path, base, deploy = self.base_conf(p, settings["deploy"], deploys)
        except RuntimeError as e:
            raise Refused(str(e))
        try:  # the Advanced values against this base conf (its keys and the cross-key rules)
            profiles.validate_advanced(p.get("advanced", {}), set(profiles.parse_conf(base)), p.get("expert", False),
                                       profiles.parse_conf(base))
        except profiles.Invalid as e:
            raise Refused(f"profile {p['name']}, Advanced: {e}")
        text, vals = profiles.resolve(p, settings, base, deploy, self.cfg["run_root"], root)
        try:
            text = profiles.prompt_disk(text, self.cfg.get("prompt_disk_gib"), self.cfg.get("prompt_disk_dir"))
        except ValueError as e:
            raise Refused(f"halod.json: {e}")
        vals = profiles.parse_conf(text)
        for k, want in (("SERVE_HOST", "127.0.0.1"), ("SERVE_PORT", self.cfg["endpoint"].rsplit(":", 1)[-1])):
            if vals.get(k) != want:
                raise Refused(f"{base_path}: {k}={vals.get(k)}; StrixFold UI needs {want}")
        rid = self._new_run(p, settings, deploy, root)
        self.set_state("waiting", f"{p['name']}: waiting for the GPU locks (box1, then box2)", actor)
        for b in self.boxes:
            b.write(f"{self.run_dir(rid)}/serve.conf".replace("$HOME", self._home(b)), text)
        # the locks: rank 0's box first, then rank 1's within lock_pair_s, all or nothing (a profile's Advanced
        # LOCK_WAIT_S / LOCK_PAIR_S replace halod's lock_try_s / lock_pair_s)
        adv = p.get("advanced", {})
        self._take_locks(int(adv.get("LOCK_WAIT_S") or self.cfg["lock_try_s"]), int(adv.get("LOCK_PAIR_S") or self.cfg["lock_pair_s"]))
        # pre-checks with the locks held: memory and no other engine on either box
        need = int(vals.get("PEAK_GIB", 101)) + profiles.MEM_FLOOR_GIB
        for b in self.boxes:
            mem_cmd = ("echo ${HALO_FAKE_MEM_GIB:-117}" if self.cfg.get("fake") else
                       "awk '/^MemAvailable:/ { print int($2 / 1048576) }' /proc/meminfo 2>/dev/null || echo 999")
            rc, out = b.run(mem_cmd, timeout=20)
            try:
                avail = int(out.strip().split()[-1])
            except (ValueError, IndexError):
                avail = None
            if avail is not None and avail < need:
                raise Refused(f"{b.name} has {avail} GiB free; {p['name']} needs {need} GiB (PEAK_GIB + 8). Something else is using memory.")
            tick = b.tick or {}
            others = [e for e in tick.get("engines", [])]
            if others and b.fresh():
                raise Refused(f"{b.name} is running another engine ({', '.join(e['comm'] + ' ' + str(e['pid']) for e in others)})")
        # start: rank 1, then rank 0 (as std_eval.sh), each under its box's lock
        self._check_cancel()
        self.set_state("loading", f"{p['name']}: loading the weights on both boxes")
        self._watch(rid)
        rc, out = self.rank_cmd(1, "start --locked", timeout=180)
        if rc != 0:
            raise Failure(f"rank 1 did not start: {out.strip()[-300:]}")
        self._check_cancel()
        rc, out = self.rank_cmd(0, "start --locked", timeout=180)
        if rc != 0:
            raise Failure(f"rank 0 did not start: {out.strip()[-300:]}")
        self._wait_phase(p)
        self._verify(p, vals)
        if settings.get("power") == "max":  # both locks held and the ranks up: only now
            self._power("performance", f"{p['name']}: Max speed")
        with self.mu:
            self.s["ready"] = now()
            self.s["last_activity"] = now()
            self.s["reason"] = None
            self._save()
        self.db.x("UPDATE runs SET ready=? WHERE run_id=?", (now(), rid))
        self.set_state("ready", f"{p['name']} is serving at {self.cfg['endpoint']}/v1")

    # -- engines with their own start/stop (ExternalAdapter)
    def ext(self, mid=None):
        """The external spec of a model (cfg "external" overrides it: tests and the fake backend), or None."""
        mid = mid or self.s.get("model_id")
        over = (self.cfg.get("external") or {}).get(mid)
        return self.on_boxes(over or (profiles.model(mid) or {}).get("external"))

    def on_boxes(self, spec):
        """A spec with its box names on this pair: the built-in profiles name rank 0 "box1" and rank 1 "box2" (the
        first pair's boxes), so another pair (the extra pairs) runs them as its own."""
        if not spec:
            return spec
        names = {old: b.name for old, b in zip(LEGACY_BOXES, sorted(self.boxes, key=lambda b: b.rank))}
        out = dict(spec)
        if out.get("box") in names:
            out["box"] = names[out["box"]]
        if out.get("boxes"):
            out["boxes"] = [names.get(n, n) for n in out["boxes"]]
        return out

    def is_external(self):
        return bool(self.s.get("run_id") and self.s.get("deploy_root") == "external")

    def box_named(self, name):
        return next(b for b in self.boxes if b.name == name)

    def _ext_each(self, ext):
        """{box: (state text, unit active, ready path answers)} for every serving box; a box out of reach is
        (None, None, False): its copy is not known to be down, and only the grace in _ext_degrade decides."""
        out = {}
        for name in ext.get("boxes") or [ext["box"]]:
            rc, txt = self.box_named(name).run(
                f"cat {ext['state_file']} 2>/dev/null | head -1; echo '--'; {self._ext_alive_cmd(ext)}; "
                f"curl -sf -o /dev/null --max-time 4 http://127.0.0.1:{ext['port']}{ext.get('ready_path', '/ready')} && echo READY; true",
                timeout=30)
            if rc != 0:
                out[name] = (None, None, False)
                continue
            state, _, rest = txt.partition("--")
            act = "\nactive" in "\n" + rest
            out[name] = (state.strip(), act, act and "READY" in rest)  # a copy serves when its unit runs and it answers
        return out

    def _ext_status(self, ext):
        """(node state text, unit active, ready path answers) on the serving box; with "boxes" (one server per box),
        the first box's state text, and active / ready only when they hold on every box."""
        states, active, ready = [], True, True
        for name in ext.get("boxes") or [ext["box"]]:
            rc, out = self.box_named(name).run(
                f"cat {ext['state_file']} 2>/dev/null | head -1; echo '--'; {self._ext_alive_cmd(ext)}; "
                f"curl -sf -o /dev/null --max-time 4 http://127.0.0.1:{ext['port']}{ext.get('ready_path', '/ready')} && echo READY; true",
                timeout=30)
            if rc != 0:
                return None, None, None
            state, _, rest = out.partition("--")
            states.append(state.strip())
            act = "\nactive" in "\n" + rest
            active, ready = active and act, ready and act and "READY" in rest
        return states[0], active, ready

    def _ext_activity(self, t):
        """An external engine's request counters (its Prometheus /metrics): a request running or more tokens than
        last time is activity, for the idle stop and the live view."""
        mt = (profiles.model(self.s.get("model_id")) or {}).get("metrics")
        ext = self.ext()
        if not mt or not ext:
            return
        rc, out = self.box_named(ext["box"]).run(f"curl -sf --max-time 4 http://127.0.0.1:{ext['port']}{mt['path']}", timeout=20)
        if rc != 0:
            return
        vals = {}
        for line in out.splitlines():
            if line.startswith("#") or " " not in line:
                continue
            k, _, v = line.rpartition(" ")
            try:
                vals[k.split("{")[0]] = float(v)
            except ValueError:
                pass
        busy, count = vals.get(mt["busy"], 0), vals.get(mt["count"])
        with self.mu:
            self.live["connections"] = int(busy)
            if busy > 0 or (count is not None and count != self.ext_count and self.ext_count is not None):
                self.s["last_activity"] = t
                self._save()
            self.ext_count = count

    def _ext_degrade(self, ext, t):
        """One-copy-per-box engines: a copy whose unit stopped, or that has not answered for health_fail_s, is dropped
        from s["copies"] (the Mac sends new conversations to the copies left, and moves a pinned one) while another
        still answers. True while at least one copy serves. Not when the engine is stopping itself for idleness. The
        dropped copy is not restarted in this pass; a copy that answers again comes back."""
        per = self._ext_each(ext)
        for name, (_, _, rdy) in per.items():
            if rdy:
                self.ext_box_ok[name] = t
        start = self.s.get("ready") or t
        # a copy stays while it answers, and through health_fail_s of silence while its unit runs or its box is out of
        # reach (act None): an unreachable box drops its own copy after the grace, never the healthy one's
        live = [n for n, (_, act, rdy) in per.items()
                if rdy or (act is not False and t - self.ext_box_ok.get(n, start) <= self.cfg["health_fail_s"])]
        if not live or not any(per[n][2] for n in live):
            return False  # nothing answers now: the plain rule (fail after health_fail_s) decides
        if self._ext_idle_stopped(ext, next((st for st, _, _ in per.values() if st), "")):
            return False
        with self.mu:
            before = self.s.get("copies")
            self.s["copies"] = live
            if live != before:
                down = [n for n in per if n not in live]
                self.s["reason"] = f"one copy ({', '.join(down)} down)" if down else None
                self._save()
        if live != before:
            self.event("copies", (f"serving from {', '.join(live)}; {', '.join(n for n in per if n not in live)} dropped "
                                  "(not restarted; stop and start the model to bring it back)") if len(live) < len(per)
                       else f"serving from {', '.join(live)} again")
            self._ext_mark_dropped(ext, live, [n for n in per if n not in live])
        return True

    def _ext_mark_dropped(self, ext, live, dropped):
        """Tell each serving copy's node which peers were dropped (dropped_file): its idle timer then stops waiting for a
        box that no longer serves, so the survivor can still stop itself when idle. Removed when every copy serves."""
        f = ext.get("dropped_file")
        if not f:
            return
        for name in live:
            cmd = f"printf '%s\\n' {' '.join(dropped)} > {f}" if dropped else f"rm -f {f}"
            rc, out = self.box_named(name).run(cmd, timeout=20)
            if rc != 0:
                self.event("error", f"{name}: the dropped copies were not passed to the engine: {out.strip()[-200:]}")

    def _ext_idle_stopped(self, ext, state):
        """True when an engine with its own idle timer stopped itself: its state says so, or its node left the idle
        marker (written on every box of the mode before its `down`, so a check that lands while one box is still
        stopping, or after a node killed before its exit trap, reads it the same)."""
        if (state or "").startswith("released (idle"):
            return True
        mk = ext.get("idle_marker")
        if not mk:
            return False
        for name in ext.get("boxes") or [ext["box"]]:  # the node that stopped may not have reached the other box
            rc, out = self.box_named(name).run(f"test -e {mk} && echo IDLE; true", timeout=20)
            if rc == 0 and "IDLE" in out:
                return True
        return False

    def _ext_alive_cmd(self, ext):
        return ext.get("alive") or f"systemctl --user is-active {ext['unit']} 2>/dev/null"

    def _start_external(self, p, actor):
        ext = self.ext(p["model_id"])
        if not ext:
            raise Refused(f"{p['name']}: no start command known for {p['model_id']}")
        for b in self.boxes:  # someone else's halo-qwen (the team measuring): leave it alone, never `down` it
            rc, out = b.run(f"{self._ext_alive_cmd(ext)}; true", timeout=20)
            if out.strip() == "active":
                raise Refused(f"{p['name']}: {ext['unit']} is already running on {b.name} (not started by StrixFold UI); "
                              f"it is left alone")
        rid = self._new_run(p, dict(p["settings"]), "external", "external")
        ctl = self.boxes[-1]  # the command runs on halod's own box; it reaches the other box itself
        rd = self.run_dir(rid)
        # a community Qwen engine takes the profile's window, Kyojin's pool, sessions and idle minutes (ext: the model's
        # spec, or cfg "external" in tests and the fake backend)
        up = profiles.fill_up(ext["up"], p["settings"])
        self.set_state("waiting", f"{p['name']}: {up} (it waits for the GPU lock itself)", actor)
        # in a session of its own (setsid where there is one: Linux), so a stop can take its whole group down
        rc, out = ctl.run(f"mkdir -p {rd} && cd {rd} && S=$(command -v setsid || true) && "
                          f"{{ $S nohup {up} > up.log 2>&1 < /dev/null & echo $! > up.pid; }}", timeout=30)
        if rc != 0:
            raise Failure(f"could not run {up}: {out.strip()[-300:]}")
        t = self.cfg["timeouts"]
        deadline = now() + t["waiting"]
        while True:
            self._sleep(2.0)
            state, active, ready = self._ext_status(ext)
            if ready:
                break
            if state and self.s["state"] == "waiting" and not state.startswith(("waiting", "released")):
                self.set_state("loading", f"{p['name']}: {state}")
                deadline = now() + t["loading"] + t["linking"]
            rc, out = ctl.run(f"kill -0 $(cat {rd}/up.pid) 2>/dev/null && echo running; tail -n 4 {rd}/up.log", timeout=20)
            if "running" not in out.split("\n", 1)[0]:
                rc2, _ = ctl.run(f"sleep 1; true", timeout=10)
                state, active, ready = self._ext_status(ext)
                if ready:
                    break
                raise Failure(f"{up} ended before the server was ready: {out.strip()[-300:]}")
            if now() > deadline:
                raise Failure(f"{self.s['state']} took too long (node state: {state})")
        self._verify_external(p, ext)
        with self.mu:
            self.s["ready"], self.s["last_activity"], self.s["reason"] = now(), now(), None
            self.s["copies"] = list(ext.get("boxes") or [ext["box"]])
            hold = self.s.get("hold")
            self._save()
        self.ext_box_ok = {}
        if hold:
            self._apply_hold(ext, True)
        self.db.x("UPDATE runs SET ready=? WHERE run_id=?", (now(), rid))
        self.set_state("ready", f"{p['name']} is serving at http://127.0.0.1:{ext['port']}/v1 (model {self.s.get('served_model')})")

    def _verify_external(self, p, ext):
        for name in ext.get("boxes") or [ext["box"]]:  # every serving box answers one greedy request
            self._verify_external_on(self.box_named(name), ext)

    def _verify_external_on(self, srv, ext):
        base = f"http://127.0.0.1:{ext['port']}"
        rc, out = srv.run(f"curl -sf --max-time 20 {base}/v1/models", timeout=40)
        try:
            mid = json.loads(out.strip().splitlines()[-1])["data"][0]["id"]
        except (ValueError, KeyError, IndexError):
            raise Failure(f"{base}/v1/models answered {out.strip()[-200:]!r}")
        # thinking off: Gufo and Kyojin think by default and would spend a short reply cap before answering
        body = json.dumps({"model": mid, "messages": [{"role": "user", "content": "Answer in one sentence: what is the capital of France?"}],
                           "max_tokens": 64, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}})
        t0 = now()
        rc, out = srv.run(f"curl -sf --max-time 240 -H 'Content-Type: application/json' -d {shlex.quote(body)} {base}/v1/chat/completions",
                          timeout=260)
        try:
            text = json.loads(out.strip().splitlines()[-1])["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError):
            raise Failure(f"the first request failed: {out.strip()[-200:]!r}")
        if "paris" not in text.lower():
            raise Failure(f"the first request on {srv.name} answered {text[:120]!r}, expected it to contain 'Paris'")
        with self.mu:
            self.s["served_model"] = mid
            self._save()
        self.event("probe", f"first request on {srv.name} ok in {now() - t0:.1f} s: {text[:80]!r} (model {mid})")

    def _stop_external(self, reason, rid):
        ext = self.ext()
        problems = []
        ctl = self.boxes[-1]
        rd = self.run_dir(rid)
        rc, out = ctl.run(f"test -s {rd}/up.pid && echo STARTED", timeout=20)
        if "STARTED" not in out:
            return []  # refused before `up` ran: nothing of ours to stop
        rc, out = ctl.run(f"p=$(cat {rd}/up.pid 2>/dev/null); [ -n \"$p\" ] && {{ kill -- -$p 2>/dev/null || kill $p 2>/dev/null; }}; "
                          f"{ext['down']} >> {rd}/down.log 2>&1; rc=$?; tail -n 3 {rd}/down.log; exit $rc", timeout=900)
        self.event("cmd", f"{ctl.name}: {ext['down']} -> {rc}: {out.strip()[-300:]}")
        if rc != 0:
            problems.append(f"{ext['down']} -> {rc}: {out.strip()[-200:]}")
        for b in self.boxes:
            rc, out = b.run(f"{self._ext_alive_cmd(ext)}; true", timeout=20)
            if out.strip() == "active":
                problems.append(f"{ext['unit']} still active on {b.name}")
        return problems

    def base_conf(self, p, deploy_setting, deploys=None):
        """(path, text, deploy dir name) of a profile's base conf: its deploy's serve.conf, or the conf it names
        (historical/custom profiles may name a conf whose DEPLOY_DIR must be on both boxes).
        RuntimeError when it cannot be read."""
        deploys = deploys if deploys is not None else self.deploys(p["model_id"])
        ad = adapter(p["engine"])
        root = self.deploy_root(p["model_id"])
        if deploy_setting == "conf":
            path = p["base_conf"]
        else:
            if deploy_setting not in deploys:
                raise RuntimeError(f"deploy {deploy_setting} is not on both boxes")
            path = ad.base_conf(root, p, deploys[deploy_setting])
        cached = self.conf_cache.get(path)
        if cached and now() - cached[0] < 30:
            text = cached[1]
        else:
            rc, text = self.boxes[-1].run(f"cat {path}", timeout=20)
            if rc != 0:
                raise RuntimeError(f"cannot read {path} on {self.boxes[-1].name}: {text.strip()[-200:]}")
            self.conf_cache[path] = (now(), text)
        if deploy_setting == "conf":
            deploy = os.path.basename(profiles.parse_conf(text).get("DEPLOY_DIR", "").rstrip("/"))
            if deploy not in deploys.values():
                raise RuntimeError(f"{path} names deploy {deploy!r}, which is not on both boxes")
        else:
            deploy = deploys[deploy_setting]
        return path, text, deploy

    # -- Max speed: the boxes' platform profile while a profile with power "max" serves
    def _power(self, value, why):
        """Write the platform profile on both boxes and read it back; every write is an event. Never fatal: a box
        that refuses stays as it is and the event says so."""
        ok = True
        for b in self.boxes:
            cmd = self.cfg["power_write"].format(value=shlex.quote(value), path=self.cfg["power_path"])
            rc, out = b.run(f"{cmd}; cat {self.cfg['power_path']}", timeout=30)
            got = (out.strip().splitlines() or [""])[-1]
            good = rc == 0 and got == value
            ok = ok and good
            self.event("power", f"{b.name}: platform profile {value} ({why}): {'ok' if good else 'FAILED: ' + out.strip()[-200:]}")
        with self.mu:
            if value != "balanced":
                self.s["power_set"] = True
            elif ok:
                self.s["power_set"] = False
            self._save()
        return ok

    def _power_check_at_start(self):
        """No run adopted: put back balanced where halod left performance, or where a box is not balanced and nobody
        holds its GPU lock (someone measuring power keeps their setting)."""
        if self.s.get("power_set"):
            self._power("balanced", "halod started with no run; restoring what it set")
            return
        for b in self.boxes:
            rc, out = b.run(f"cat {self.cfg['power_path']} 2>/dev/null; python3 {self.cfg['probe']} once --lock {shlex.quote(b.lock_path)}"
                            + (f" --fake {b.name}" if self.cfg.get("fake") else "") + " | python3 -c \"import json,sys; "
                            "print('HOLDERS', len(json.load(sys.stdin)['lock']['holders']))\"", timeout=40)
            lines = out.strip().splitlines()
            if rc != 0 or not lines:
                continue
            cur = lines[0].strip()
            held = next((l for l in lines if l.startswith("HOLDERS ")), "HOLDERS 1").split()[1] != "0"
            if cur and cur != "balanced":
                if held:
                    self.event("power", f"{b.name}: platform profile is {cur}; its GPU lock is held, so it is left as it is")
                else:
                    cmd = self.cfg["power_write"].format(value="balanced", path=self.cfg["power_path"])
                    rc, out = b.run(cmd, timeout=30)
                    self.event("power", f"{b.name}: platform profile was {cur} with nobody holding its GPU lock; set balanced -> {rc}")

    def _home(self, box):
        if not hasattr(box, "home"):
            rc, out = box.run("echo $HOME", timeout=20)
            if rc != 0 or not out.strip():
                raise Failure(f"cannot reach {box.name}: {out.strip()[-200:]}")
            box.home = out.strip().splitlines()[-1]
        return box.home

    def _take_locks(self, try_s=None, pair_s=None):
        deadline = now() + self.cfg["timeouts"]["waiting"]
        tries = 0
        try_s, pair_s = try_s or self.cfg["lock_try_s"], pair_s or self.cfg["lock_pair_s"]
        while True:
            self._check_cancel()
            if now() > deadline:
                raise Refused(f"the GPUs did not come free within {self.cfg['timeouts']['waiting'] // 60} minutes")
            wait = int(max(5, min(try_s, deadline - now())))
            rc0, out0 = self.rank_cmd(0, f"lock {wait}", timeout=wait + 60, cancel=self.cancel)
            if rc0 == 0:
                hook, self._after_lock0 = self._after_lock0, None
                if hook:
                    hook()  # what holds box2's lock until then steps aside for this run's own
                rc1, out1 = self.rank_cmd(1, f"lock {pair_s}", timeout=pair_s + 60, cancel=self.cancel)
                if rc1 == 0:
                    self.event("locks", "both GPU locks held (box1, then box2)")
                    return
                if rc1 in (124, 130, 255):
                    self._release_waiters(1)
                self.rank_cmd(1, "unlock", timeout=60)
                self.rank_cmd(0, "unlock", timeout=60)
            elif rc0 in (124, 130, 255):
                self._release_waiters(0)  # an attempt cut short leaves its flock waiting: take it down
                self.rank_cmd(0, "unlock", timeout=60)
            b = self.cfg["lock_backoff_s"]
            self._sleep(b[min(tries, len(b) - 1)])
            tries += 1

    def _release_waiters(self, rank):
        b = self.boxes[rank]
        rc, out = b.run(f"python3 {self.cfg['probe']} release {self.run_dir()}", timeout=30)
        self.event("cmd", f"{b.name}: release this run's lock waiters -> {rc}: {out.strip()[-200:]}")

    def _watch(self, rid):
        for b in self.boxes:
            rd = f"{self.run_dir(rid)}".replace("$HOME", self._home(b))
            w = {"cmd": "watch", "run_dir": rd, "rank": b.rank, "pidfiles": [f"{rd}/rank{b.rank}.pid", f"{rd}/lock.pid"],
                 "log": f"{rd}/rank{b.rank}.log", "offset": self.s["offsets"].get(b.name, 0),
                 "done": self.s.get("done", 0) if b.rank == 0 else 0}
            if b.rank == 0:
                w["health"] = self.cfg["endpoint"]
            b.set_watch(w)

    def _rank_alive(self, rank):
        """True / False from the latest tick of this run's watch; None when unknown (no fresh tick, a tick from
        before the watch, or no pid file yet)."""
        b = self.boxes[rank]
        tick = b.tick or {}
        if not b.fresh() or not self.s.get("run_id") or not (tick.get("run_dir") or "").endswith("/" + self.s["run_id"]):
            return None
        st = (tick.get("pids") or {}).get(f"rank{rank}.pid")
        return None if st is None else st["alive"]

    def _log_has(self, rank, needle):
        b = self.boxes[rank]
        rc, out = b.run(f"grep -c -F {shlex.quote(needle)} {self.run_dir()}/rank{rank}.log 2>/dev/null || true", timeout=20)
        try:
            return int(out.strip().splitlines()[-1]) > 0
        except (ValueError, IndexError):
            return False

    def _tail(self, rank, n=5):
        rc, out = self.boxes[rank].run(f"tail -n {n} {self.run_dir()}/rank{rank}.log 2>/dev/null", timeout=20)
        return out.strip()[-600:]

    def _wait_phase(self, p):
        """loading -> linking (rank 0 read its rank cache) -> rank 1 linked and rank 0 serving."""
        t = self.cfg["timeouts"]
        start, linked_seen = now(), False
        phase_deadline = start + t["loading"]
        while True:
            self._check_cancel()
            for r in (0, 1):
                if self._rank_alive(r) is False:
                    raise Failure(f"rank {r} exited while {self.s['state']}: {self._tail(r)}")
            st = self.s["state"]
            ad = self.adapter()
            if st == "loading" and self._log_has(*ad.loaded):
                self.set_state("linking", f"{p['name']}: weights loaded; linking the ranks over USB4")
                phase_deadline = now() + t["linking"]
            if self.s["state"] == "linking":
                linked_seen = linked_seen or self._log_has(*ad.linked)
                if linked_seen and self._log_has(*ad.serving):
                    return
            if now() > phase_deadline:
                raise Failure(f"{self.s['state']} took longer than {int(phase_deadline - (start if st == 'loading' else self.s['since']))} s")
            self._sleep(1.0)

    def chat_probe(self, prompt, max_tokens, timeout=120, probe_flag=True):
        b = self.boxes[0]
        cid = secrets.token_hex(6)
        w = {"ev": threading.Event(), "rec": None, "probe": probe_flag}
        self.chat_waiters[cid] = w
        if not b.send({"cmd": "chat", "id": cid, "url": self.cfg["endpoint"], "prompt": prompt, "max_tokens": max_tokens, "timeout": timeout}):
            self.chat_waiters.pop(cid, None)
            return {"ok": False, "error": f"no contact with {b.name}"}
        if not w["ev"].wait(timeout + 15):
            self.chat_waiters.pop(cid, None)
            return {"ok": False, "error": f"no reply within {timeout} s"}
        rec = w["rec"]
        if rec.get("ok") and rec["result"].get("id"):
            self.db.x("UPDATE requests SET probe=1 WHERE id=?", (rec["result"]["id"],))
        return rec

    def _verify(self, p, vals):
        """/health answers, then the probe chat request (greedy) answers with PROBE_EXPECT."""
        deadline = now() + self.cfg["timeouts"]["verify"]
        while not self.health.get("ok"):
            self._check_cancel()
            for r in (0, 1):
                if self._rank_alive(r) is False:
                    raise Failure(f"rank {r} exited before the server answered: {self._tail(r)}")
            if now() > deadline:
                raise Failure(f"the server did not answer /health within {self.cfg['timeouts']['verify']} s")
            self._sleep(1.0)
        prompt = vals.get("PROBE_PROMPT") or "Answer in one sentence: what is the capital of France?"
        expect = (vals.get("PROBE_EXPECT") or "Paris").lower()
        r = self.chat_probe(prompt, 64, timeout=max(30, int(deadline - now())))
        if not r.get("ok"):
            raise Failure(f"the first request failed: {r.get('error')}")
        text = r["result"].get("text", "")
        if expect not in text.lower():
            raise Failure(f"the first request answered {text[:120]!r}, expected it to contain {expect!r}")
        self.event("probe", f"first request ok in {r['result'].get('wall_s')} s: {text[:80]!r}")

    def _stop_run(self, reason, final_state="idle"):
        """Stop both ranks (rank 0 first: rank 1 hears done), release both locks (box2 then box1, the reverse of
        taking them), check nothing of this run is left. final_state None: leave the state to the caller."""
        rid = self.s.get("run_id")
        if rid:
            with self.mu:
                st = self.s["state"]
            if st != "stopping":
                self.set_state("stopping", reason)
            for b in self.boxes:
                b.set_watch(None)
            problems = []
            if self.is_external():
                problems = self._stop_external(reason, rid)
                if self.s.get("power_set"):
                    self._power("balanced", reason)
            else:
                if not self.s.get("steps", {}).get("loading"):
                    for r in (0, 1):  # stopped while waiting: no queued flock of this run may take a lock later
                        self._release_waiters(r)
                for r in (0, 1):
                    rc, out = self.rank_cmd(r, "stop", timeout=self.cfg["timeouts"]["stopping"])
                    if rc != 0:
                        problems.append(f"rank {r} stop: {out.strip()[-200:]}")
                if self.s.get("power_set") and not self._power("balanced", reason):  # before the locks go to the team
                    problems.append("could not put the platform profile back to balanced on every box")
                for r in (1, 0):
                    rc, out = self.rank_cmd(r, "unlock", timeout=60)
                    if rc != 0:
                        problems.append(f"unlock {self.boxes[r].name}: {out.strip()[-200:]}")
                left = self._leftovers(rid)
                if left:
                    problems.append("left behind: " + "; ".join(left))
            self.db.x("UPDATE runs SET ended=?, end_reason=?, steps=? WHERE run_id=?", (now(), reason, json.dumps(self.s.get("steps")), rid))
            if problems:
                self.event("stop-problem", " | ".join(problems))
                if final_state == "idle":
                    final_state, reason = "failed", f"{reason}; but: {' | '.join(problems)}"
        with self.mu:
            self.health.update(ok=None, data=None)
            self.s["run_id"] = None if final_state else self.s["run_id"]
            if final_state:
                self.s["served_model"] = None
            self._save()
        if final_state:
            self.set_state(final_state, reason)

    def _leftovers(self, rid):
        """Anything of run rid still alive on either box: a rank or a lock holder (from its pid files)."""
        out = []
        for b in self.boxes:
            rd = self.run_dir(rid)
            rc, txt = b.run(f"for f in {rd}/rank{b.rank}.pid {rd}/lock.pid; do [ -f $f ] && p=$(cat $f) && kill -0 $p 2>/dev/null "
                            f"&& echo \"$(basename $f) $p\"; done; true", timeout=20)
            if rc == 0:
                out += [f"{b.name} {l}" for l in txt.split("\n") if l.strip()]
            else:
                out.append(f"{b.name}: cannot check ({txt.strip()[-100:]})")
        return out

    # -- re-adoption after a halod restart
    def _adopt(self):
        st, rid = self.s["state"], self.s.get("run_id")
        if not rid:
            if st not in ("idle", "failed"):
                self.set_state("idle", "halod restarted; no run recorded")
            self._power_check_at_start()
            return
        self.event("adopt", f"halod {VERSION} started; found run {rid} in state {st}")
        if self.s.get("deploy_root") == "external":
            ext = self.ext()
            state, active, ready = self._ext_status(ext) if ext else (None, None, None)
            if st == "ready" and ready:
                self.set_state("ready", "re-adopted the running engine after a halod restart")
            else:
                self._stop_run(f"halod restarted while the engine was {st}; stopped it ({state})", final_state="failed" if st != "idle" else "idle")
            return
        with self.mu:
            self.s["reason"] = f"halod restarted: checking the ranks of run {rid}"
            self._save()
        for _ in range(12):  # a box out of reach: try for two minutes before deciding anything
            alive = []
            for r in (0, 1):
                b = self.boxes[r]
                rc, out = b.run(f"f={self.run_dir(rid)}/rank{r}.pid; [ -f $f ] && kill -0 $(cat $f) 2>/dev/null && echo alive || echo gone", timeout=30)
                alive.append(("alive" in out) if rc == 0 else None)
            if None not in alive:
                break
            time.sleep(10)
        if alive == [True, True] and st in ("loading", "linking"):
            self._watch(rid)
            p = self.store.get(self.s["profile"]) or {"name": self.s["profile"], "id": self.s["profile"]}
            rc, conf = self.boxes[-1].run(f"cat {self.run_dir(rid)}/serve.conf", timeout=20)
            try:
                self.event("adopt", f"resuming a run that was {st}")
                self._wait_phase(p)
                self._verify(p, profiles.parse_conf(conf))
                with self.mu:
                    self.s["ready"], self.s["last_activity"], self.s["reason"] = now(), now(), None
                    self._save()
                self.set_state("ready", f"{p['name']} is serving at {self.cfg['endpoint']}/v1 (re-adopted while loading)")
            except (Failure, Cancelled) as e:
                self._handle_failure(f"re-adopted run: {e}")
            return
        if alive == [True, True]:
            self._watch(rid)
            t0 = now()
            while now() - t0 < 30 and not self.health.get("ok"):
                time.sleep(1)
            if self.health.get("ok"):
                with self.mu:
                    self.s["ready"] = self.s.get("ready") or now()
                    self.s["last_activity"] = now()
                    self._save()
                self.set_state("ready", "re-adopted the running ranks after a halod restart")
                if (self.s.get("settings") or {}).get("power") == "max":  # halod's stop put balanced back
                    self._power("performance", "Max speed, re-adopted run")
                if self.s.get("intent") == "stop":
                    self.cmdq.put(("stop", "a stop was pending when halod restarted", "halod"))
                return
            self._stop_run("re-adopted ranks did not answer /health after a halod restart", final_state="failed")
            return
        if None in alive:
            # a box cannot be reached: leave everything alone and say so
            self.set_state("failed", "after a halod restart one box could not be checked; nothing was changed. Stop to clean up.")
            return
        if any(alive):
            dead = [str(r) for r, a in enumerate(alive) if not a]
            self._stop_run(f"after a halod restart rank {', '.join(dead)} was gone; stopped the other", final_state="failed")
            return
        # nothing runs: release this run's locks if a holder is left
        self._stop_run(f"halod restarted; run {rid} was not running", final_state="idle" if st != "failed" else "failed")

    # -- the monitor
    def _sweep(self):
        """Release any GPU lock still held for a halo-control run that is not the current one (a lock attempt cut
        short by a dropped ssh can leave its holder behind). Only our own runs' holders are touched, through the
        deploy's own unlock; another team member's lock is never touched."""
        cur = self.s.get("run_id")
        for b in self.boxes:
            if not b.fresh():
                continue
            for h in ((b.tick or {}).get("lock") or {}).get("holders", []):
                w = h.get("who") or {}
                rd = w.get("run_dir") or ""
                if w.get("kind") != "halo-control" or "/halo-control/runs/" not in rd:
                    continue
                rid = os.path.basename(rd)
                if rid == cur or now() - (h.get("since") or now()) < self.cfg.get("sweep_after_s", 30):
                    continue
                row = self.db.q("SELECT deploy, ended, deploy_root FROM runs WHERE run_id=?", (rid,))
                if not row:
                    continue
                if row[0]["ended"] and b.tick_t < row[0]["ended"] + 2 * self.cfg["tick_s"]:
                    continue  # a tick from before that run's own unlock
                rc, out = self.rank_cmd(b.rank, "unlock", timeout=60, run_id=rid, deploy=row[0]["deploy"],
                                        root=row[0]["deploy_root"] or self.cfg["deploy_root"])
                self.event("sweep", f"released a stale lock of run {rid} on {b.name} (holder {h.get('pid')}): {rc}")

    def _monitor(self):
        last_probe = now()
        last_sweep = 0.0
        last_install = 0.0
        while not self.closed:
            time.sleep(self.cfg["tick_s"])
            if now() - last_install > 120:  # in a thread of its own: a box out of reach must not hold up the checks below
                last_install = now()
                threading.Thread(target=self._check_installs_quietly, name="installs", daemon=True).start()
            if now() - last_sweep > 15:
                last_sweep = now()
                try:
                    self._sweep()
                except Exception:  # noqa: BLE001
                    self.event("error", "sweep: " + traceback.format_exc()[-800:])
            try:
                with self.mu:
                    st, s = self.s["state"], dict(self.s)
                if st != "ready" or not self.cmdq.empty():
                    continue
                t = now()
                if self.is_external():
                    if t - self.ext_checked > 10:
                        self.ext_checked = t
                        ext = self.ext()
                        state, active, ready = self._ext_status(ext)
                        boxes = list(ext.get("boxes") or [])
                        if not ready and len(boxes) > 1 and self._ext_degrade(ext, t):
                            ready = True  # one copy still serves (the other dropped from the copies list)
                        elif ready and len(boxes) > 1 and s.get("copies") not in (None, boxes):
                            with self.mu:  # a dropped copy answers again
                                self.s["copies"], self.s["reason"] = boxes, None
                                self._save()
                            self.event("copies", f"serving from {', '.join(boxes)} again")
                            self._ext_mark_dropped(ext, boxes, [])
                        if ready:
                            self.ext_ok = t
                            self._ext_activity(t)
                        elif active is False and self._ext_idle_stopped(self.ext(), state):
                            # a community Qwen engine stopped itself after its idle minutes (its node's own timer)
                            self.stop(f"idle for {(s.get('settings') or {}).get('idle_min', 45)} minutes with no requests "
                                      "(the engine stopped itself; the GPU is free)", actor="idle")
                            continue
                        elif active is False or t - (self.ext_ok or s["ready"] or t) > self.cfg["health_fail_s"]:
                            self.cmdq.put(("fail", f"the engine stopped answering {self.ext().get('ready_path', '/ready')} ({state}; unit {'active' if active else 'not active'})", "monitor"))
                            continue
                    mt = (profiles.model(s.get("model_id")) or {}).get("metrics")
                    idle_min = (s.get("settings") or {}).get("idle_min", 45)
                    last = s.get("last_activity") or s.get("ready") or t
                    if mt and idle_min and not s.get("hold") and t - last > idle_min * self.cfg["minute_s"] and not self.live.get("connections"):
                        self.stop(f"idle for {idle_min} minutes with no requests (the GPU is free)", actor="idle")
                    continue
                # a dead rank
                for r in (0, 1):
                    if self._rank_alive(r) is False:
                        self.cmdq.put(("fail", f"rank {r} ({self.boxes[r].name}) exited: {self._tail(r, 3)}", "monitor"))
                        raise StopIteration
                # the link: a USB4 cable down on either box
                down = []
                for b in self.boxes:
                    if b.fresh():
                        for l in ((b.tick or {}).get("telemetry") or {}).get("usb4", {}).get("links", []):
                            if not (l.get("up") and l.get("carrier")):
                                down.append(f"{l['iface']} on {b.name}")
                if down:
                    self.link_down_since = self.link_down_since or t
                    if t - self.link_down_since > self.cfg["link_loss_s"]:
                        self.cmdq.put(("fail", f"USB4 link lost: {', '.join(down)}", "monitor"))
                        raise StopIteration
                else:
                    self.link_down_since = None
                # a stall: /health silent while rank 0's box is reachable, or a request making no progress
                b0 = self.boxes[0]
                if b0.fresh() and self.health.get("ok") is False and t - (self.health.get("last_ok") or s["ready"] or t) > self.cfg["health_fail_s"]:
                    self.cmdq.put(("fail", f"stall: /health has not answered for {int(t - (self.health.get('last_ok') or t))} s ({self.health.get('error')})", "monitor"))
                    raise StopIteration
                busy = self.live.get("busy_since")
                if busy and not (self.live.get("decode_tps") or self.live.get("prefill_tps")):
                    since, allow = self.stall_allowance(busy, s)
                    if t - since > allow:
                        self.cmdq.put(("fail", f"stall: a request has made no progress for {int(t - since)} s", "monitor"))
                        raise StopIteration
                # idle auto-stop
                prof = self.store.get(s["profile"]) or {}
                idle_min = (s.get("settings") or prof.get("settings") or {}).get("idle_min", 45)
                last = s.get("last_activity") or s.get("ready") or t
                if idle_min and not s.get("hold") and t - last > idle_min * self.cfg["minute_s"] and not self.live.get("connections"):
                    self.stop(f"idle for {idle_min} minutes with no requests (the GPUs are free)", actor="idle")
                    continue
                # the tiny probe while idle
                if t - last > self.cfg["probe_after_idle_s"] and t - last_probe > self.cfg["probe_every_s"] and not self.live.get("connections"):
                    last_probe = t
                    threading.Thread(target=self._idle_probe, daemon=True).start()
            except StopIteration:
                pass
            except Exception:  # noqa: BLE001
                self.event("error", "monitor: " + traceback.format_exc()[-1200:])

    def _idle_probe(self):
        r = self.chat_probe("Reply with the single word OK.", 4, timeout=60)
        if r.get("ok"):
            self.event("probe", f"idle probe ok in {r['result'].get('wall_s')} s")
        elif self.s["state"] == "ready":
            self.cmdq.put(("fail", f"the idle probe failed: {r.get('error')}", "monitor"))

    def close(self):
        """Stop this instance's threads and probe streams (tests: a 'restart' of halod). Ranks are untouched."""
        self.closed = True
        for b in self.boxes:
            b.close()

    # -- views for the API
    def view_state(self):
        with self.mu:
            s = dict(self.s)
            health = dict(self.health)
            live = dict(self.live)
        p = self.store.get(s["profile"]) if s.get("profile") else None
        t = now()
        idle = None
        if s["state"] == "ready" and (not self.is_external() or (profiles.model(s.get("model_id")) or {}).get("metrics")):
            idle_min = (s.get("settings") or {}).get("idle_min", 45)
            last = s.get("last_activity") or s.get("ready") or t
            idle = {"idle_min": idle_min, "last_activity": last, "hold": s.get("hold"),
                    "stop_at": None if (s.get("hold") or not idle_min) else last + idle_min * self.cfg["minute_s"]}
        boxes = {}
        mine = s.get("run_id")
        for b in self.boxes:
            tick = b.tick or {}
            for rec in ((tick.get("lock") or {}).get("holders", []) + (tick.get("lock") or {}).get("waiters", [])):
                rec["mine"] = bool(mine and ((rec.get("who") or {}).get("run_dir") or "").endswith("/" + mine))
            boxes[b.name] = {"rank": b.rank, "connected": b.connected, "fresh": b.fresh(), "age_s": round(t - b.tick_t, 1) if b.tick_t else None,
                             "error": None if b.connected else b.last_error, "telemetry": tick.get("telemetry"),
                             "lock": tick.get("lock"), "quiet": tick.get("quiet"), "engines": tick.get("engines"),
                             "pids": tick.get("pids")}
        restarts = [r for r in s.get("restarts", []) if t - r < RESTART_WINDOW_S]
        return {"version": VERSION, "fake": bool(self.cfg.get("fake")), "t": t, "state": s["state"], "since": s["since"],
                "reason": s.get("reason"), "intent": s.get("intent"), "profile": p, "run_id": s.get("run_id"),
                "settings": s.get("settings"), "deploy": s.get("deploy"), "steps": s.get("steps"), "started": s.get("started"),
                "ready": s.get("ready"), "hold": s.get("hold"), "idle": idle, "restarts": restarts,
                "copies": s.get("copies") if self.is_external() else None,
                "restart_max": RESTART_MAX, "endpoint": (f"http://127.0.0.1:{self.ext()['port']}/v1" if self.is_external() and self.ext()
                                                          else self.cfg["endpoint"] + "/v1"),
                "served_model": s.get("served_model") or ("dsv41" if s.get("run_id") else None), "health": {"ok": health.get("ok"),
                "since": health.get("since"), "error": health.get("error"), "data": health.get("data")}, "live": live,
                "boxes": boxes, "timeouts": self.cfg["timeouts"],
                "limits": self.limits()}


    def stall_allowance(self, busy, s):
        """(since, seconds) for the no-progress rule. A request that has shown progress (decode) is a stall stall_s after
        its last sign of it. One that has not yet is reading its prompt, and the engine reports nothing until that ends
        (its prefill speed comes only when a prefill completes): it gets time by its run's window, max(stall_s,
        context / 900 + 180) s (64K about 4 min, 512K about 13, 1M about 22; a 1M first read takes about 16)."""
        progress, done = self.live.get("progress_at") or 0, self.live.get("done_at") or 0
        if progress > max(busy, done):
            return progress, self.cfg["stall_s"]
        ctx = int(((s.get("settings") or {}).get("context")) or 0)
        return max(busy, done), max(self.cfg["stall_s"], ctx / 900 + 180)

    def check_installs(self):
        """{model id: {"ok", "why"}}: a model is installed when its deploy is on both boxes and, for a model with
        its own bundle, each box's verify_inputs.sh report passed for the bundle the model pins."""
        found = {}
        for m in profiles.MODELS:
            if not m.get("installed", True) or not profiles.ENGINES.get(m["engine"], {}).get("installed"):
                found[m["id"]] = {"ok": False, "why": m.get("missing") or "not installed"}
                continue
            if m.get("pending") and m["id"] not in (self.cfg.get("models_ready") or []):
                found[m["id"]] = {"ok": False, "why": m["pending"]}
                continue
            ext = self.ext(m["id"])
            if ext:  # its own start command, binary and model on every box
                why = None
                for b in self.boxes:
                    rc, out = b.run(f"( {ext['check']} ) && echo HAVE || echo MISSING", timeout=30)
                    if "HAVE" not in out:
                        why = f"{m.get('missing') or 'not installed'} ({b.name}: missing)"
                        break
                found[m["id"]] = {"ok": why is None, "why": why}
                continue
            try:
                deploys = self.deploys(m["id"])
            except RuntimeError as e:
                found[m["id"]] = {"ok": False, "why": str(e)}
                continue
            if m["deploy_default"] not in deploys:
                found[m["id"]] = {"ok": False, "why": f"deploy {m['deploy_default']} is not on both boxes"}
                continue
            why = None
            if m.get("bundle"):
                sha = m["conf"]["BUNDLE_SHA256"]
                for b in self.boxes:
                    rc, out = b.run(f"python3 -c \"import json,sys; r=json.load(open(sys.argv[1])); "
                                    f"print(r.get('status'), r.get('manifest_sha256'))\" "
                                    f"{m['bundle']}.verify-rank{b.rank}-$(hostname).json 2>/dev/null || echo missing", timeout=20)
                    words = out.split()
                    if rc != 0 or words[-2:] != ["pass", sha]:
                        why = f"the bundle's verify report on {b.name} is {' '.join(words[-2:]) or 'missing'}, not a pass for the pinned manifest"
                        break
            found[m["id"]] = {"ok": why is None, "why": why}
        with self.mu:
            changed = found != self.installs
            self.installs = found
        if changed:
            self.event("models", "install check: " + "; ".join(f"{k} {'installed' if v['ok'] else 'not installed (' + str(v['why']) + ')'}"
                                                                 for k, v in found.items()))
        return found

    @staticmethod
    def deploy_of(settings, profile):
        """The deploy a create, edit or check will run: the one sent, else the profile's own."""
        return (settings or {}).get("deploy") or ((profile or {}).get("settings") or {}).get("deploy")

    def kit_limit(self, mid, deploy, fresh=False):
        """The longest window a model's deploy serves, by serve_lib.sh's own rule: its ds-kit's rows (the smallest of the
        four RoPE tables' sizes / 128 bytes) - 49, on every box (the smallest wins); a box without the tables, or a deploy
        without a ds-kit: the bundle kit's 65,487. Cached ten minutes per (model, deploy)."""
        own = profiles.model_window(mid)  # a community Qwen engine: its own longest window, no RoPE kit
        if own is not None:
            return own
        if not deploy or deploy == "conf":
            return profiles.ROPE_ROWS
        key = (mid, deploy)
        hit = self._kit.get(key)
        if hit and not fresh and now() - hit[0] < 600:
            return hit[1]
        root = self.deploy_root(mid)
        rows = []
        for b in self.boxes:
            rc, out = b.run(profiles.kit_rows_cmd(f"{root}/{shlex.quote(deploy)}/ds-kit"), timeout=20)
            w = (out.strip().split() or ["none"])[-1]
            rows.append(int(w) if rc == 0 and w.isdigit() and int(w) > profiles.KIT_PAD else None)
        limit = profiles.ROPE_ROWS if not rows or any(r is None for r in rows) else min(rows) - profiles.KIT_PAD
        self._kit[key] = (now(), limit)
        return limit

    def limits(self):
        """{profile id: the longest window its deploy serves}, from kit_limit's cache (refreshed with the install
        check): the Mac offers a chat size only where this is at least its window."""
        out = {}
        for p in self.store.all():
            dep = (p.get("settings") or {}).get("deploy")
            hit = self._kit.get((p.get("model_id"), dep))
            out[p["id"]] = profiles.model_window(p.get("model_id")) or (hit[1] if hit else profiles.ROPE_ROWS)
        return out

    def _refresh_limits(self):
        seen = set()
        for p in self.store.all():
            key = (p.get("model_id"), (p.get("settings") or {}).get("deploy"))
            if key in seen or not key[1] or key[1] == "conf" or (self.installs.get(key[0]) or {}).get("ok") is False \
                    or profiles.model_window(key[0]) is not None:
                continue
            seen.add(key)
            try:
                self.kit_limit(*key, fresh=True)
            except Exception:  # noqa: BLE001 - the fallback stands
                pass

    def _check_installs_quietly(self):
        if not self.installs_mu.acquire(blocking=False):
            return  # the previous check is still running
        try:
            self.check_installs()
            self._refresh_limits()
        except Exception:  # noqa: BLE001
            self.event("error", "install check: " + traceback.format_exc()[-600:])
        finally:
            self.installs_mu.release()

    def view_profiles(self):
        installs = self.installs or self.check_installs()
        models = []
        for m in profiles.MODELS:
            q = {k: m.get(k) for k in ("id", "name", "variant", "engine", "model", "quant", "about", "warning", "facts",
                                       "deploy_default", "missing")}
            inst = installs.get(m["id"]) or {"ok": None, "why": None}
            q["installed"], q["why"] = inst["ok"], inst["why"]
            q["engine_name"] = profiles.ENGINES.get(m["engine"], {}).get("name", m["engine"])
            models.append(q)
        out = []
        for p in self.store.all():
            if p.get("chat_size"):  # Chat's long windows are chosen in its composer, not on the Models page
                continue
            q = dict(p)
            inst = installs.get(p["model_id"]) or {}
            if q.get("installed", True) and inst.get("ok") is False:
                q["installed"], q["missing"] = False, f"Not startable: {inst['why']}"
            q["limits"] = profiles.describe_limits(p["settings"], p.get("advanced"), p["model_id"], p.get("peak_gib"))
            q["engine_name"] = profiles.ENGINES.get(p["engine"], {}).get("name", p["engine"])
            out.append(q)
        return {"models": models, "profiles": out, "fields": list(profiles.EDITABLE), "max_context": profiles.MAX_CONTEXT,
                "align": profiles.ALIGN, "cache_max_gib": profiles.CACHE_MAX_GIB, "streams_max": profiles.STREAMS_MAX,
                "streams_note": profiles.STREAMS_NOTE, "power_note": profiles.POWER_NOTE}


    def view_conf(self, pid, deploy=None, advanced=None, expert=None):
        """The Advanced section of a profile: every key of its base conf (for deploy, default the profile's)."""
        p = self.store.get(pid)
        if not p:
            raise KeyError(pid)
        path, text, d = self.base_conf(p, deploy or p["settings"]["deploy"])
        adv = p.get("advanced", {}) if advanced is None else advanced
        exp = p.get("expert", False) if expert is None else expert
        return {"profile": pid, "path": path, "deploy": d, "expert": exp,
                "knobs": profiles.conf_schema(text, adv, exp)}

    def base_keys_for(self, p, deploy=None):
        """(keys, values) of a profile's base conf, for validating Advanced values; (None, None) when unreadable."""
        try:
            _, text, _ = self.base_conf(p, deploy or p["settings"]["deploy"])
        except (RuntimeError, KeyError):
            return None, None
        vals = profiles.parse_conf(text)
        return set(vals), vals

    def view_requests(self, limit=50, run_id=None, probes=False):
        sql = "SELECT * FROM requests WHERE 1=1" + ("" if probes else " AND probe=0") + (" AND run_id=?" if run_id else "") + " ORDER BY t DESC LIMIT ?"
        rows = self.db.q(sql, ((run_id,) if run_id else ()) + (int(limit),))
        for r in rows:
            r["decode_tps"] = round(r["tokens"] / r["decode_s"], 1) if r.get("decode_s") and r["tokens"] > 1 else None
            r["prefill_tps"] = round((r["prompt"] - r["cached"]) / r["prefill_s"], 1) if r.get("prefill_s") and r["prompt"] > r["cached"] else None
            r["acceptance"] = round(r["accepted"] / r["drafted"], 3) if r.get("drafted") else None
        return {"requests": rows}

    def view_metrics(self):
        rows = self.view_requests(200)["requests"]
        recent = rows[:20]

        def med(key):
            v = sorted(r[key] for r in recent if r.get(key) is not None)
            return v[len(v) // 2] if v else None
        hits = [r for r in recent if r["prompt"]]
        series = {b.name: [[round(t, 1), v] for t, v in list(b.history)[::3]] for b in self.boxes}
        with self.mu:
            live = dict(self.live)
        return {"recent_n": len(recent), "decode_tps": med("decode_tps"), "prefill_tps": med("prefill_tps"), "ttft_s": med("ttft_s"),
                "cache_hit": round(sum(r["cached"] for r in hits) / max(1, sum(r["prompt"] for r in hits)), 3) if hits else None,
                "acceptance": round(sum(r["accepted"] for r in recent) / max(1, sum(r["drafted"] for r in recent)), 3) if any(r["drafted"] for r in recent) else None,
                "live": live, "requests": [{k: r[k] for k in ("t", "decode_tps", "prefill_tps", "ttft_s", "prompt", "cached", "tokens", "acceptance")} for r in rows[:60]][::-1],
                "boxes": series}

    def view_locks(self):
        out = {}
        empty = {"holders": [], "waiters": []}
        mine = self.s.get("run_id")
        for b in self.boxes:
            lk = (b.tick or {}).get("lock") or dict(empty)
            for rec in lk.get("holders", []) + lk.get("waiters", []):
                w = rec.get("who") or {}
                rec["mine"] = bool(mine and w.get("run_dir", "").endswith("/" + mine))
            out[b.name] = {"fresh": b.fresh(), "lock": lk, "quiet": (b.tick or {}).get("quiet")}
        return out

    def view_logs(self, rank, n):
        rid = self.s.get("run_id")
        if not rid:
            last = self.db.q("SELECT run_id FROM runs ORDER BY started DESC LIMIT 1")
            rid = last[0]["run_id"] if last else None
        if not rid:
            return {"run_id": None, "lines": []}
        row = self.db.q("SELECT deploy_root, model_id FROM runs WHERE run_id=?", (rid,))
        if row and row[0]["deploy_root"] == "external" and self.ext(row[0]["model_id"]):
            ext = self.ext(row[0]["model_id"])
            extra = f"; echo; echo '--- {self.run_dir(rid)}/up.log'; tail -n 40 {self.run_dir(rid)}/up.log {self.run_dir(rid)}/down.log 2>/dev/null" \
                if self.boxes[rank] is self.boxes[-1] else ""
            rc, out = self.boxes[rank].run(f"tail -n {int(n)} {ext['log']} 2>/dev/null{extra}", timeout=20)
            return {"run_id": rid, "rank": rank, "box": self.boxes[rank].name, "lines": out.splitlines()[-int(n) - 45:]}
        rc, out = self.boxes[rank].run(f"tail -n {int(n)} {self.run_dir(rid)}/rank{rank}.log 2>/dev/null | grep -v '\"GET /health\\|\"GET /metrics'", timeout=20)
        return {"run_id": rid, "rank": rank, "box": self.boxes[rank].name, "lines": out.splitlines()[-int(n):]}

    def view_events(self, limit=100):
        return {"events": self.db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),))}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "halod/" + VERSION
    H = None  # the Halod

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "host not allowed"})
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        H = self.H
        try:
            if u.path == "/api/ping":
                return self._send(200, {"ok": True, "version": VERSION})
            if u.path == "/api/state":
                return self._send(200, H.view_state())
            if u.path == "/api/profiles":
                return self._send(200, H.view_profiles())
            if u.path == "/api/requests":
                return self._send(200, H.view_requests(int(q.get("limit", 50)), q.get("run"), q.get("probes") == "1"))
            if u.path == "/api/metrics":
                return self._send(200, H.view_metrics())
            if u.path == "/api/locks":
                return self._send(200, H.view_locks())
            if u.path == "/api/logs":
                return self._send(200, H.view_logs(int(q.get("rank", 0)) % 2, min(1000, int(q.get("n", 200)))))
            if u.path == "/api/events":
                return self._send(200, H.view_events(min(1000, int(q.get("limit", 100)))))
            if u.path == "/api/deploys":
                return self._send(200, {"deploys": H.deploys(q.get("model") or "dsv41")})
            if u.path == "/api/conf":
                return self._send(200, H.view_conf(q.get("profile"), q.get("deploy")))
            if u.path == "/api/profiles/export":
                return self._send(200, H.store.export())
            return self._send(404, {"error": "not found"})
        except KeyError as e:
            return self._send(404, {"error": f"no such profile {e}"})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _write(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "host not allowed"})
        if not secrets.compare_digest(self.headers.get("X-Halo-Token") or "", self.H.token):
            return self._send(403, {"error": "missing or wrong X-Halo-Token"})
        u = urlparse(self.path)
        H = self.H
        actor = (self.headers.get("X-Halo-Actor") or "api")[:40]
        try:
            b = self._body()
            if method == "POST" and u.path == "/api/start":
                return self._send(200, H.start(b.get("profile"), bool(b.get("replace")), actor))
            if method == "POST" and u.path == "/api/stop":
                return self._send(200, H.stop(b.get("reason") or "stopped from the dashboard", actor))
            if method == "POST" and u.path == "/api/restart":
                return self._send(200, H.restart(actor))
            if method == "POST" and u.path == "/api/hold":
                return self._send(200, H.hold(bool(b.get("on")), actor))
            if method == "POST" and u.path == "/api/profiles/validate":
                # live validation for the editor: the main settings, and the Advanced values against the base
                # profile's conf (base: the profile being edited or duplicated)
                errors, out = {}, {}
                base = H.store.get(b.get("base") or "")
                deploys = set(b["deploys"]) if b.get("deploys") else None
                try:
                    mid = base["model_id"] if base else "dsv41"
                    out["settings"] = profiles.validate_settings(b.get("settings") or {}, deploys, model_id=mid,
                                                                 max_rows=H.kit_limit(mid, H.deploy_of(b.get("settings"), base)))
                    out["limits"] = profiles.describe_limits(out["settings"], b.get("advanced"), base["model_id"] if base else None)
                except profiles.Invalid as e:
                    errors.update(e.errors)
                if base is not None and b.get("advanced") is not None:
                    keys, vals = H.base_keys_for(base, (b.get("settings") or {}).get("deploy"))
                    try:
                        profiles.validate_advanced(b["advanced"], keys, bool(b.get("expert")), vals)
                    except profiles.Invalid as e:
                        errors.update({"adv:" + k: v for k, v in e.errors.items()})
                return self._send(200, dict(out, ok=not errors, errors=errors))
            if method == "POST" and u.path == "/api/profiles":
                base = H.store.get(b.get("base") or "")
                if base is None:
                    raise profiles.Invalid({"base": f"no profile {b.get('base')}"})
                keys, vals = H.base_keys_for(base, (b.get("settings") or {}).get("deploy"))
                p = H.store.create(b.get("name"), b.get("base"), b.get("settings") or {}, set(H.deploys(base["model_id"])),
                                   b.get("advanced"), b.get("expert"), keys, vals,
                                   H.kit_limit(base["model_id"], H.deploy_of(b.get("settings"), base)))
                H.event("profile", f"created {p['id']} ({p['name']}) from {base['id']}: " +
                        profiles.diff({"settings": base["settings"], "advanced": base.get("advanced", {}), "expert": base.get("expert")}, p), actor)
                return self._send(200, {"ok": True, "profile": p})
            if method == "POST" and u.path == "/api/profiles/import":
                made, errs = H.store.import_(b.get("data") or {}, None, lambda base: H.base_keys_for(base), H.kit_limit)
                H.event("profile", f"imported {len(made)} profile(s): {', '.join(p['id'] for p in made) or 'none'}"
                        + (f"; refused: {errs}" if errs else ""), actor)
                return self._send(200, {"ok": not errs, "created": [p["id"] for p in made], "errors": errs})
            if u.path.startswith("/api/profiles/"):
                pid = u.path.rsplit("/", 1)[1]
                if method == "PUT":
                    cur = H.store.get(pid)
                    if cur is None:
                        raise KeyError(pid)
                    keys, vals = H.base_keys_for(cur, (b.get("settings") or {}).get("deploy"))
                    old, p = H.store.update(pid, b.get("name"), b.get("settings"), set(H.deploys(cur["model_id"])),
                                            b.get("advanced"), b.get("expert"), keys, vals,
                                            H.kit_limit(cur["model_id"], H.deploy_of(b.get("settings"), cur)))
                    running = H.s.get("profile") == pid and H.s["state"] in ACTIVE
                    H.event("profile", f"updated {pid}: {profiles.diff(old, p)}" + (" (applies at its next start)" if running else ""), actor)
                    return self._send(200, {"ok": True, "profile": p, "running": running})
                if method == "DELETE":
                    if H.s.get("profile") == pid and H.s["state"] in ACTIVE:
                        raise profiles.Invalid({"profile": "this profile is running; stop it first"})
                    H.store.delete(pid)
                    H.event("profile", f"deleted {pid}", actor)
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except profiles.Invalid as e:
            return self._send(400, {"ok": False, "errors": e.errors, "error": str(e)})
        except KeyError as e:
            return self._send(404, {"ok": False, "error": f"no such profile {e}"})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        self._write("POST")

    def do_PUT(self):
        self._write("PUT")

    def do_DELETE(self):
        self._write("DELETE")


def load_config(over):
    cfg = default_config()
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def serve(H, port=None):
    Handler.H = H
    srv = ThreadingHTTPServer((H.cfg["listen_host"], H.cfg["listen_port"] if port is None else port), Handler)
    srv.daemon_threads = True
    return srv


def restore_power(cfg):
    """halod.service's ExecStopPost: put balanced back on both boxes if halod's state says it set performance
    (halod stopped or crashed). Plain subprocesses; no probe streams."""
    path = os.path.join(cfg["home"], "state.json")
    try:
        s = json.load(open(path))
    except (OSError, ValueError):
        return 0
    if not s.get("power_set"):
        return 0
    cmd = cfg["power_write"].format(value="balanced", path=cfg["power_path"])
    ok = True
    for spec in cfg["boxes"]:
        ex = spec["exec"][0]
        argv = (["ssh", "-n"] + cfg["ssh_opts"] + ex[1:] + [cmd]) if ex and ex[0] == "ssh" else list(ex) + ["bash", "-c", cmd]
        try:
            ok = subprocess.run(argv, timeout=40, capture_output=True).returncode == 0 and ok
        except (OSError, subprocess.TimeoutExpired):
            ok = False
    if ok:
        s["power_set"] = False
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, path)
    print(f"halod: platform profile balanced on both boxes: {'ok' if ok else 'FAILED on one'}")
    return 0 if ok else 1


def main(argv):
    path = os.path.join(default_config()["home"], "halod.json")
    if "--config" in argv:
        path = argv[argv.index("--config") + 1]
    over = {}
    if os.path.exists(path):
        with open(path) as f:
            over = json.load(f)
    cfg = load_config(over)
    if "--restore-power" in argv:
        sys.exit(restore_power(cfg))
    if cfg["listen_host"] not in ("127.0.0.1", "::1", "localhost"):
        sys.exit("halod listens on 127.0.0.1 only")
    H = Halod(cfg)

    def bye(*_):  # systemctl stop: the ranks keep running (re-adoption), the platform profile goes back
        if H.s.get("power_set"):
            H._power("balanced", "halod stopping")
        os._exit(0)
    signal.signal(signal.SIGTERM, bye)
    srv = serve(H)
    H.worker.start()
    H.monitor.start()
    H.event("halod", f"halod {VERSION} listening on {cfg['listen_host']}:{cfg['listen_port']}{' (fake backend)' if cfg.get('fake') else ''}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(sys.argv)
