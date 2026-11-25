#!/usr/bin/env python3
"""probe.py: one box's eyes for halod. Read-only: it never writes sysfs, never signals a process it did not start.

  probe.py stream [--interval S] [--lock PATH] [--quiet-dir DIR] [--fake NAME]
      JSON lines on stdout until stdin closes: a "tick" every S seconds (telemetry, the GPU lock's holder and waiters,
      quiet-window requests, the watched rank pids, /health), "log" lines appended to the watched rank log, and a
      "done" record per finished request with the server's /metrics right after it. Commands arrive as JSON lines on
      stdin: {"cmd": "watch", "run_dir": D, "rank": R, "pidfiles": [...], "log": F, "offset": N, "health": URL}
      {"cmd": "unwatch"}, {"cmd": "chat", "id": X, "url": U, "prompt": P, "max_tokens": N}, {"cmd": "ping"}.
  probe.py once [--fake NAME]   one tick, printed (for checks by hand).

halod runs it locally on box2 and over ssh on box1. Python 3.9+ standard library only.
"""
import glob
import json
import os
import random
import re
import sys
import threading
import time
import urllib.request

LOCK = "/var/tmp/halo-gpu.lock"
QUIET = "/var/tmp/halo-quiet.d"
ENGINE_COMMS = ("tensorfold-nati", "tf-dsv41-lanes", "tf-dsv41-layer", "ds4", "ds4-server", "ds4-bench", "gufo", "flash_serve")
DONE = re.compile(r"done (\S+) prompt=(\d+) cached=(\d+) thinking=(\S+) effort=(\S+) tokens=(\d+) sha=(\S+) "
                  r"finish=(\S+?)(?:\s.*?)? rounds=(\d+) accepted=(\d+)/(\d+)")


def read(p, default=None):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return default


def num(p, scale=1.0):
    v = read(p)
    try:
        return int(v.strip()) / scale
    except (AttributeError, ValueError):
        return None


# ---------------------------------------------------------------- telemetry

def telemetry():
    out = {"host": os.uname().nodename, "t": time.time()}
    mem = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0]) * 1024
    out["mem_total_gib"] = mem.get("MemTotal", 0) / (1 << 30) if mem else None
    out["mem_available_gib"] = mem.get("MemAvailable", 0) / (1 << 30) if mem else None
    dev = None
    for d in sorted(glob.glob("/sys/class/drm/card*/device/mem_info_gtt_used")):
        dev = os.path.dirname(d)
        break
    if dev:
        out["gtt_used_gib"] = (num(os.path.join(dev, "mem_info_gtt_used")) or 0) / (1 << 30)
        out["gtt_total_gib"] = (num(os.path.join(dev, "mem_info_gtt_total")) or 0) / (1 << 30)
        out["gpu_busy_pct"] = num(os.path.join(dev, "gpu_busy_percent"))
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        name = (read(os.path.join(h, "name")) or "").strip()
        if name == "amdgpu":
            out["gpu_temp_c"] = num(os.path.join(h, "temp1_input"), 1000)
            out["ppt_w"] = num(os.path.join(h, "power1_average"), 1e6) or num(os.path.join(h, "power1_input"), 1e6)
            out["sclk_mhz"] = num(os.path.join(h, "freq1_input"), 1e6)
        elif name == "k10temp":
            out["cpu_temp_c"] = num(os.path.join(h, "temp1_input"), 1000)
    out["power_profile"] = (read("/sys/firmware/acpi/platform_profile") or "").strip() or None
    if dev:
        out["gpu_clock_policy"] = (read(os.path.join(dev, "power_dpm_force_performance_level")) or "").strip() or None
    links = []
    for n in ("thunderbolt0", "thunderbolt1"):
        base = f"/sys/class/net/{n}"
        if not os.path.isdir(base):
            continue
        links.append({"iface": n, "up": (read(f"{base}/operstate") or "").strip() == "up",
                      "carrier": (read(f"{base}/carrier") or "0").strip() == "1", "mtu": num(f"{base}/mtu"),
                      "rx_bytes": num(f"{base}/statistics/rx_bytes"), "tx_bytes": num(f"{base}/statistics/tx_bytes")})
    peers = []
    for d in sorted(glob.glob("/sys/bus/thunderbolt/devices/*-*")):
        name = (read(f"{d}/device_name") or "").strip()
        if name and ":" not in os.path.basename(d) and not os.path.basename(d).endswith("-0"):
            peers.append({"dev": os.path.basename(d), "name": name, "rx": (read(f"{d}/rx_speed") or "").strip(),
                          "rx_lanes": num(f"{d}/rx_lanes"), "tx": (read(f"{d}/tx_speed") or "").strip(), "tx_lanes": num(f"{d}/tx_lanes")})
    out["usb4"] = {"links": links, "peers": peers}
    out["load"] = [float(x) for x in (read("/proc/loadavg") or "0 0 0").split()[:3]]
    return out


def fake_telemetry(name, busy):
    """Plausible numbers for the fake backend (no hardware read)."""
    r = random.random
    load = 1.0 if busy else 0.0
    return {"host": name, "t": time.time(), "mem_total_gib": 123.3, "mem_available_gib": 117 - 100 * load + r(),
            "gtt_used_gib": 0.6 + 98.2 * load, "gtt_total_gib": 124.0, "gpu_busy_pct": int(70 * load + 5 * r()),
            "gpu_temp_c": 41 + 22 * load + 2 * r(), "ppt_w": 18 + 82 * load + 6 * r(), "sclk_mhz": 600 + 2300 * load,
            "cpu_temp_c": 44 + 12 * load, "power_profile": (read(os.path.expanduser("~/platform_profile")) or "balanced").strip(),
            "gpu_clock_policy": "auto",
            "usb4": {"links": [{"iface": f"thunderbolt{i}", "up": True, "carrier": True, "mtu": 1500, "rx_bytes": 0, "tx_bytes": 0} for i in (0, 1)],
                     "peers": [{"dev": f"{i}-2", "name": "box2" if name == "box1" else "box1", "rx": "20.0 Gb/s", "rx_lanes": 2, "tx": "20.0 Gb/s", "tx_lanes": 2} for i in (0, 1)]},
            "load": [round(0.3 + 2 * load, 2)] * 3}


# ---------------------------------------------------------------- the GPU lock and quiet windows

def proc_info(pid):
    cmd = (read(f"/proc/{pid}/cmdline") or "").replace("\0", " ").strip()
    st = read(f"/proc/{pid}/stat")
    start = None
    if st:
        try:
            ticks = int(st.rsplit(")", 1)[1].split()[19])
            btime = next(int(l.split()[1]) for l in open("/proc/stat") if l.startswith("btime"))
            start = btime + ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError, StopIteration):
            pass
    ppid = None
    if st:
        try:
            ppid = int(st.rsplit(")", 1)[1].split()[1])
        except (ValueError, IndexError):
            pass
    return {"pid": pid, "cmd": cmd, "start": start, "ppid": ppid}


def children(pid):
    out = []
    for t in glob.glob(f"/proc/{pid}/task/*/children"):
        out += [int(x) for x in (read(t) or "").split()]
    return out


# whose a lock usually is, from where its pid file lives or its tag (a hint, shown as such)
OWNERS = (("/halo-control/", "StrixFold UI"),)


def owner(cmd):
    for key, name in OWNERS:
        if key in cmd:
            return name
    return None


def who(cmd, lock=LOCK):
    w = _who(cmd, lock)
    o = owner(cmd)
    if o:
        w["owner"] = o
    return w


def _who(cmd, lock=LOCK):
    """A short, plain label for a lock holder or waiter: the run directory its lock.pid names, a tag=NAME in its
    script, or the command flock runs."""
    m = re.search(r"(/[^\s']+)/lock\.pid", cmd)
    if m:
        run = m.group(1)
        return {"label": os.path.basename(run), "run_dir": run, "kind": "halo-control" if "/halo-control/" in run else "run",
                "pidfile": run + "/lock.pid"}
    m = re.search(r"> ?'?([$~\w/.-]*/[\w.-]+)\.pid", cmd)
    if m:
        path = m.group(1).replace("$HOME", os.path.expanduser("~")).replace("~", os.path.expanduser("~"), 1)
        return {"label": os.path.basename(m.group(1)), "kind": "job", "pidfile": path + ".pid"}
    m = re.search(r"tag=([A-Za-z][A-Za-z0-9]*)", cmd)
    if m:
        return {"label": m.group(1), "kind": "job"}
    words = cmd.split()
    rest = words[words.index(lock) + 1:] if lock in words else words
    while len(rest) > 1 and os.path.basename(rest[0]) in ("bash", "sh", "python3", "python", "env", "nohup", "setsid", "-c"):
        rest = rest[1:]  # an interpreter: name the script it runs
    return {"label": os.path.basename(rest[0]) if rest else "?", "kind": "job"}


def lock_state(path=LOCK):
    """The GPU lock's holder(s) and waiters from /proc/locks (flock on the lock file's inode). Without /proc (the
    fake backend on a Mac) the fake holder's PATH.who file stands in."""
    try:
        st = os.stat(path)
    except OSError:
        return {"path": path, "exists": False, "holders": [], "waiters": []}
    if not os.path.exists("/proc/locks"):
        try:
            w = json.load(open(path + ".who"))
            os.kill(w["pid"], 0)
            return {"path": path, "exists": True, "waiters": [],
                    "holders": [{"pid": w["pid"], "since": w.get("since"), "who": who(w.get("cmd", ""), path), "cmd": w.get("cmd", "")}]}
        except (OSError, ValueError, KeyError):
            return {"path": path, "exists": True, "holders": [], "waiters": []}
    # match on the inode: on btrfs stat's st_dev (the subvolume's anonymous device) differs from /proc/locks' device
    ino = str(st.st_ino)
    holders, waiters = [], []
    for line in (read("/proc/locks") or "").splitlines():
        f = line.split()
        if len(f) < 6:
            continue
        blocked = f[1] == "->"
        g = f[2:] if blocked else f[1:]
        if len(g) < 5 or g[0] != "FLOCK" or g[4].rsplit(":", 1)[-1] != ino:
            continue
        pid = int(g[3])
        info = proc_info(pid)
        kids = children(pid)
        # flock FILE CMD forks CMD once it holds the lock: the child's start is when the lock was taken
        since = proc_info(kids[0])["start"] if kids and not blocked else info["start"]
        if not info["cmd"] and not blocked:
            # the process that took the lock has ended, but a child that inherited the descriptor holds it: name that
            heir = fd_holder(path, st.st_ino, pid)
            if heir:
                info = proc_info(heir)
        w = who(info["cmd"], path) if info["cmd"] else {"label": f"process {pid} (ended; its descriptor is still open)", "kind": "job"}
        if w.get("pidfile") and not blocked:
            try:
                since = os.stat(w["pidfile"]).st_mtime
            except OSError:
                pass
        rec = {"pid": pid, "since": since, "who": w, "cmd": info["cmd"][-160:]}
        (waiters if blocked else holders).append(rec)
    return {"path": path, "exists": True, "holders": holders, "waiters": waiters}


def fd_holder(path, ino, gone_pid):
    """The oldest process with the lock file open (a child holding an inherited descriptor), or None."""
    found = []
    for d in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(d) == path and os.stat(d).st_ino == ino:
                found.append(int(d.split("/")[2]))
        except OSError:
            continue
    found = [p for p in found if p != gone_pid]
    return min(found) if found else None


def quiet_state(d=QUIET):
    out = []
    for p in glob.glob(f"{d}/want.*"):
        t = (read(p) or "").split()
        out.append({"tag": os.path.basename(p)[5:], "since": float(t[0]) if t else None, "user": t[1] if len(t) > 1 else None})
    return {"requests": out, "paused": (read(f"{d}/paused") or "").strip() or None}


def engines():
    out = []
    for d in glob.glob("/proc/[0-9]*"):
        comm = (read(f"{d}/comm") or "").strip()
        if comm in ENGINE_COMMS:
            out.append({"pid": int(os.path.basename(d)), "comm": comm})
    return out


def pid_state(pidfile):
    v = (read(pidfile) or "").strip()
    if not v.isdigit():  # not started yet (or already cleaned up): unknown, not dead
        return {"pidfile": pidfile, "pid": None, "alive": None}
    pid = int(v)
    if not os.path.isdir("/proc/self"):  # no /proc (macOS, the fake backend)
        try:
            os.kill(pid, 0)
            return {"pidfile": pidfile, "pid": pid, "alive": True, "comm": None}
        except OSError:
            return {"pidfile": pidfile, "pid": pid, "alive": False, "comm": None}
    comm = (read(f"/proc/{pid}/comm") or "").strip()
    alive = bool(comm) and (read(f"/proc/{pid}/stat") or " Z ").split(")")[-1].split()[0] != "Z"
    return {"pidfile": pidfile, "pid": pid, "alive": alive, "comm": comm or None}


# ---------------------------------------------------------------- the server

def http_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def metrics(base, timeout=5):
    text = urllib.request.urlopen(base + "/metrics", timeout=timeout).read().decode()
    m = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        k, _, v = line.rpartition(" ")
        if "{" in k:
            continue
        try:
            m[k.replace("tensorfold:", "")] = float(v)
        except ValueError:
            pass
    return m


def parse_done(line):
    m = DONE.search(line)
    if not m:
        return None
    g = m.groups()
    return {"id": g[0], "prompt": int(g[1]), "cached": int(g[2]), "thinking": g[3] == "True", "effort": g[4],
            "tokens": int(g[5]), "sha": g[6], "finish": g[7], "rounds": int(g[8]), "accepted": int(g[9]), "drafted": int(g[10])}


def chat(url, prompt, max_tokens, timeout=120):
    body = json.dumps({"model": "probe", "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
                       "temperature": 0}).encode()
    t0 = time.monotonic()
    req = urllib.request.Request(url + "/v1/chat/completions", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        reply = json.load(r)
    return {"id": reply.get("id"), "text": (reply.get("choices") or [{}])[0].get("message", {}).get("content") or "",
            "usage": reply.get("usage"), "wall_s": round(time.monotonic() - t0, 3)}


# ---------------------------------------------------------------- the stream

class Stream:
    def __init__(self, interval, lock, quiet, fake):
        self.interval, self.lock, self.quiet, self.fake = interval, lock, quiet, fake
        self.watch = None
        self.out_lock = threading.Lock()
        self.alive = True
        self.health_fail = 0

    def emit(self, rec):
        rec.setdefault("t", time.time())
        with self.out_lock:
            try:
                sys.stdout.write(json.dumps(rec) + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                self.alive = False

    def commands(self):
        for line in sys.stdin:
            try:
                c = json.loads(line)
            except ValueError:
                continue
            if c.get("cmd") == "watch":
                w = dict(c)
                w["pos"] = int(c.get("offset") or 0)
                w["done"] = int(c.get("done") or 0)
                w["buf"] = b""
                self.watch = w
                self.emit({"type": "watching", "run_dir": c.get("run_dir"), "offset": w["pos"]})
            elif c.get("cmd") == "unwatch":
                self.watch = None
            elif c.get("cmd") == "chat":
                threading.Thread(target=self.do_chat, args=(c,), daemon=True).start()
            elif c.get("cmd") == "ping":
                self.emit({"type": "pong", "id": c.get("id")})
        self.alive = False  # stdin closed: halod (or its ssh) is gone

    def do_chat(self, c):
        try:
            r = chat(c["url"], c["prompt"], int(c.get("max_tokens", 8)), float(c.get("timeout", 120)))
            self.emit({"type": "chat", "id": c.get("id"), "ok": True, "result": r})
        except Exception as e:  # noqa: BLE001 - reported to halod as text
            self.emit({"type": "chat", "id": c.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"})

    def follow(self, w):
        """New complete lines of the watched log since w['pos']; 'done' lines with a /metrics snapshot."""
        path = w.get("log")
        if not path:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < w["pos"]:  # a new file under the same name
            w["pos"], w["buf"] = 0, b""
        if size == w["pos"]:
            return
        with open(path, "rb") as f:
            f.seek(w["pos"])
            data = f.read(min(size - w["pos"], 4 << 20))
        w["pos"] += len(data)
        buf = w["buf"] + data
        *lines, w["buf"] = buf.split(b"\n")
        text = [l.decode("utf-8", "replace").rstrip("\r") for l in lines]
        keep = [l for l in text if '"GET /health' not in l and '"GET /metrics' not in l]
        if keep:
            self.emit({"type": "log", "rank": w.get("rank"), "lines": keep[-400:], "offset": w["pos"] - len(w["buf"])})
        for l in text:
            d = parse_done(l)
            if not d:
                continue
            w["done"] += 1
            snap = None
            if w.get("health"):
                # the request's metrics land with or just after its line; never hold the ticks up for long (a
                # stalled server must still show up as a failing /health)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    try:
                        snap = metrics(w["health"], timeout=max(0.2, min(1.0, deadline - time.monotonic())))
                    except Exception:  # noqa: BLE001
                        snap = None
                    if snap and snap.get("request_latency_seconds_count", 0) >= w["done"]:
                        break
                    time.sleep(0.05)
            self.emit({"type": "done", "req": d, "n": w["done"], "metrics": snap, "offset": w["pos"] - len(w["buf"])})

    def tick(self):
        w = self.watch
        busy = bool(w)
        rec = {"type": "tick", "telemetry": fake_telemetry(self.fake, busy) if self.fake else telemetry(),
               "lock": lock_state(self.lock), "quiet": quiet_state(self.quiet), "engines": [] if self.fake else engines()}
        if w:
            rec["run_dir"] = w.get("run_dir")
            rec["pids"] = {os.path.basename(p): pid_state(p) for p in w.get("pidfiles", [])}
            if w.get("health"):
                try:
                    rec["health"] = http_json(w["health"] + "/health", timeout=4)
                except Exception as e:  # noqa: BLE001
                    rec["health"] = None
                    rec["health_error"] = f"{type(e).__name__}: {e}"[:200]
        self.emit(rec)

    def run(self):
        threading.Thread(target=self.commands, daemon=True).start()
        nxt = 0.0
        while self.alive:
            now = time.monotonic()
            w = self.watch
            if w:
                try:
                    self.follow(w)
                except Exception as e:  # noqa: BLE001
                    self.emit({"type": "error", "error": f"follow: {type(e).__name__}: {e}"})
            if now >= nxt:
                nxt = now + self.interval
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001
                    self.emit({"type": "error", "error": f"tick: {type(e).__name__}: {e}"})
            time.sleep(0.2)


def release(run_dir):
    """Kill the lock waiters and holders of one halo-control run (flock processes naming RUN_DIR/lock.pid, and the
    holder each one runs): what a cancelled or cut-short lock attempt can leave behind. Only paths under a
    /halo-control/runs/ directory are accepted, so nothing of anyone else's can be named."""
    import signal
    run_dir = os.path.abspath(run_dir)
    if "/halo-control/runs/" not in run_dir + "/":
        print(f"refusing {run_dir}: not a halo-control run directory")
        return 2
    want = run_dir + "/lock.pid"
    killed = []
    if not os.path.isdir("/proc/self"):  # macOS (the fake backend): the holder's command line names the path
        import subprocess
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        for line in out.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if want in cmd and pid.isdigit() and int(pid) != os.getpid():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    killed.append(int(pid))
                except OSError:
                    pass
        print(f"released {run_dir}: {killed or 'nothing left'}")
        return 0
    for d in glob.glob("/proc/[0-9]*"):
        cmd = (read(f"{d}/cmdline") or "").split("\0")
        pid = int(os.path.basename(d))
        if pid == os.getpid() or not any(want in a for a in cmd):
            continue
        for c in children(pid):
            try:
                os.kill(c, signal.SIGTERM)
                killed.append(c)
            except OSError:
                pass
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            pass
    print(f"released {run_dir}: {killed or 'nothing left'}")
    return 0


def main(argv):
    try:  # a stack dump on SIGUSR1 (stderr) for diagnosing a stuck probe
        import faulthandler
        import signal
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    except (ImportError, AttributeError, ValueError):
        pass
    args = {"--interval": "2", "--lock": LOCK, "--quiet-dir": QUIET, "--fake": ""}
    mode = argv[1] if len(argv) > 1 else "once"
    it = iter(argv[2:])
    for a in it:
        args[a] = next(it, "")
    if mode == "release":
        sys.exit(release(argv[2]))
    if mode == "stream":
        Stream(float(args["--interval"]), args["--lock"], args["--quiet-dir"], args["--fake"]).run()
    else:
        print(json.dumps({"telemetry": fake_telemetry(args["--fake"], False) if args["--fake"] else telemetry(),
                          "lock": lock_state(args["--lock"]), "quiet": quiet_state(args["--quiet-dir"]), "engines": engines()}, indent=1))


if __name__ == "__main__":
    main(sys.argv)
