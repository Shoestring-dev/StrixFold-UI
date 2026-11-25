"""Where a Private chat's words may go.

The tunnel: a Private turn never uses the app's ordinary tunnels (they follow ~/.ssh/config, which any agent running
as the user can change). It uses its own: /usr/bin/ssh with no config file (-F /dev/null), the box's address, user and
key named here, StrictHostKeyChecking=yes against the box's host key pinned here (handed to ssh by KnownHostsCommand: no
file to edit), first straight over the tailnet, else through the local SOCKS proxy with /usr/bin/nc (the pinned host
key makes that hop unable to read or fake the session). Before each Private turn the process listening on the private
port must be that ssh child (/usr/sbin/lsof), else the turn is refused.

The engine: before a Private turn the Mac checks, read-only over that same ssh, five facts about the boxes that
serve the engine, and keeps the answer for the engine's run:
  1. no prompt or body logging (the canary probe, Q2 with the lead's limits: one request through the private tunnel
     holding a random marker, max_tokens 1; then each box, as its user with no sudo, names the files changed in the
     last 3 minutes that hold it, under ~, /tmp and /var/tmp but not ~/models, files under 200 MB, names only, and
     counts its journal lines that hold it; 20 s, and a search that runs out is "not checked", which refuses);
  2. no KV or prompt-cache disk tier (the engine's own arguments);
  3. core dumps off for the engine's processes (their soft core limit is 0);
  4. swap only zram or encrypted;
  5. kernel.yama.ptrace_scope >= 1.
An engine that fails one gets no Private turn, said in words. Changing the boxes' settings needs the lead's OK; this
module only reads them.
"""
import json
import os
import re
import subprocess
import threading
import time

SSH = "/usr/bin/ssh"
NC = "/usr/bin/nc"
LSOF = "/usr/sbin/lsof"
SOCKS = "localhost:1055"  # the Mac's userspace tailscaled
# a key in the Secure Enclave is a pointer file that ssh can use only through macOS's provider, which
# ~/.ssh/config may name (ssh_argv reads no config, so it names it itself)
SK_PROVIDER = "/usr/lib/ssh-keychain.dylib"
KEY = os.path.expanduser("~/.ssh/id_ed25519")
HOSTKEY = None  # the boxes' pinned host key ("ssh-ed25519 AAAA..."): with none, no Private turn
BOXES = {}  # {box: (address, user[, its own host key])}, rank 0 first
try:
    # this setup's boxes, from checked code only (private_code holds site_defaults.py to git like the rest), never
    # config.json, which any agent running as the user could change (the open-source export leaves it out)
    from site_defaults import PRIVATE_BOXES as _PB
    KEY = os.path.expanduser(_PB.get("key") or KEY)
    HOSTKEY = _PB.get("hostkey") or HOSTKEY
    BOXES = {b: tuple(v) for b, v in (_PB.get("boxes") or {}).items()}
except ImportError:
    pass
# each chat engine: its boxes (all of a tensor-parallel run hold the prompt), its port on the first box, the private
# tunnel's local port (never 18080, the ordinary tunnel's), and the process names that serve it
ENGINES = {
    "dsv41": {"name": "DeepSeek V4.1", "boxes": ("box1", "box2"), "port": 18080, "local": 18580, "comm": ("tensorfold-nati", "tf-dsv41-lanes")},
    "dsv41-unc": {"name": "DeepSeek uncensored", "boxes": ("box1", "box2"), "port": 18080, "local": 18580, "comm": ("tensorfold-nati", "tf-dsv41-lanes")},
    "qwen38": {"name": "Qwen 3.8", "boxes": ("box1",), "port": 18090, "local": 18590, "comm": ()},
    "qwen38-kyojin": {"name": "Qwen 3.8 (Kyojin)", "boxes": ("box1",), "port": 18090, "local": 18590, "comm": ()},
}


def engine_boxes(engine):
    """An engine's boxes on this setup: ENGINES names them by rank as the main boxes were named (rank 0 "box1"), and the
    pinned boxes (BOXES, rank 0 first) take those ranks (the extra pairs)."""
    n = len(ENGINES[engine]["boxes"])
    return tuple(list(BOXES)[:n]) if len(BOXES) >= 2 else ENGINES[engine]["boxes"]


DISK_FLAGS = re.compile(r"--(slot-save-path|snapshot[\w-]*|spill[\w-]*|disk[\w-]*cache[\w-]*|cache-dir|kv-dir|log-prompts?|"
                        r"log-requests?|verbose-prompt|dump[\w-]*)\b", re.I)
CANARY_PROBE = True  # fact 1 by a canary request (Q2: the lead's OK, with the limits in CANARY_SCRIPT)
CANARY_WAIT_S = 20
FACTS_TTL_S = 15 * 60
WARM_BEFORE_S = 2 * 60  # a kept answer this close to its end is checked again in the background

# Read-only on a box, as the box user. The marker is in this text, sent on stdin, and in the environment; grep reads it
# from a file descriptor (-f <(...)), so it is on no command line. File names only, never content: every file changed
# in the last 3 minutes under ~, /tmp and /var/tmp (not ~/models), a file over 200 MB by its last 64 MB (logs grow at
# the end). The journal is the box user's own (journalctl --user): halod runs as a systemd user unit there, its output
# goes to that journal, and the engines write their run logs under ~/halo-control/runs (read-only; no system
# unit is involved, so no group is needed). Its count only. Exit 124: the 20 s ran out.
CANARY_SCRIPT = r"""
export m='%(marker)s'
timeout %(wait)d bash -c '
  pat() { printf "%%s\\n" "$m"; }
  find "$HOME" /tmp /var/tmp -path "$HOME/models" -prune -o -type f -mmin -3 -print0 2>/dev/null |
    while IFS= read -r -d "" f; do
      n=$(wc -c < "$f" 2>/dev/null); n=$(( ${n:-0} + 0 ))
      if [ "$n" -gt 209715200 ]; then
        tail -c 67108864 "$f" 2>/dev/null | grep -qF -f <(pat) && printf "file %%s\\n" "$f"
      else
        grep -qF -f <(pat) "$f" 2>/dev/null && printf "file %%s\\n" "$f"
      fi
    done | head -20
  echo "journal $(journalctl --user --since -3min --no-pager 2>/dev/null | grep -cF -f <(pat))"
'
"""

# Read-only on a box: the settings the five facts need, as one JSON line (no log or prompt content is read)
FACTS_SCRIPT = r"""
ports="%(ports)s"; comms="%(comms)s"
procs=""
add() { pid=$1; port=$2; [ -r /proc/$pid/limits ] || return
  core=$(awk '/Max core file size/ {print $5}' /proc/$pid/limits)
  args=$(tr '\0' ' ' < /proc/$pid/cmdline | sed 's/"/ /g' | cut -c1-2000)
  procs="$procs{\"pid\":$pid,\"port\":\"$port\",\"comm\":\"$(cat /proc/$pid/comm)\",\"core_soft\":\"$core\",\"args\":\"$args\"},"; }
for p in $ports; do for pid in $(ss -ltnpH "sport = :$p" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); do add $pid $p; done; done
for c in $comms; do for pid in $(pgrep -x "$c"); do add $pid ""; done; done
swaps=""
for dev in $(tail -n +2 /proc/swaps | awk '{print $1}'); do
  t=$(lsblk -no TYPE "$dev" 2>/dev/null | head -1); swaps="$swaps{\"dev\":\"$dev\",\"type\":\"$t\"},"; done
printf '{"host":"%%s","ptrace_scope":"%%s","core_pattern":"%%s","procs":[%%s],"swaps":[%%s]}\n' "$(hostname)" \
  "$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo none)" "$(head -c 200 /proc/sys/kernel/core_pattern | tr -d '"\\')" \
  "${procs%%,}" "${swaps%%,}"
"""


def ssh_argv(box, *more, direct=True):
    """/usr/bin/ssh to a box with nothing taken from ~/.ssh: no config file, the box's host key pinned here."""
    v = tuple(BOXES.get(box) or ())
    ip, user = (v + (None, None))[:2]
    hostkey = v[2] if len(v) > 2 else HOSTKEY  # a box's own key, else the one the main boxes shared
    if not ip or not hostkey:
        raise RuntimeError(f"{box} has no pinned address and host key for Private chats (mac/site_defaults.py PRIVATE_BOXES)")
    return [SSH, "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
            "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=/dev/null", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"KnownHostsCommand=/bin/echo {ip} {hostkey}", "-o", "HostKeyAlgorithms=ssh-ed25519",
            "-o", "IdentitiesOnly=yes", "-i", KEY, "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=no",
            *(["-o", f"SecurityKeyProvider={SK_PROVIDER}"] if os.path.exists(SK_PROVIDER) else []),
            "-o", "ProxyCommand=none" if direct else f"ProxyCommand={NC} -X 5 -x {SOCKS} %h %p",
            "-l", user, *more, ip]


def facts(box, ports=(18080, 18090), comms=("tensorfold-nati", "tf-dsv41-lanes"), run=subprocess.run):
    """One box's settings (FACTS_SCRIPT's JSON), read over the private ssh; raises RuntimeError when it can't be read."""
    script = FACTS_SCRIPT % {"ports": " ".join(map(str, ports)), "comms": " ".join(comms)}
    last = None
    for direct in (True, False):
        try:
            r = run(ssh_argv(box, direct=direct) + ["bash", "-s"], input=script.encode(), capture_output=True, timeout=40)
        except (OSError, subprocess.TimeoutExpired) as e:
            last = str(e)
            continue
        if r.returncode == 0:
            try:
                return json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
            except (ValueError, IndexError):
                last = "no answer"
                continue
        last = r.stderr.decode("utf-8", "replace").strip()[-200:]
    raise RuntimeError(f"{box} couldn't be checked ({last})")


def probe(endpoint, served_model, timeout=60):
    """Fact 1's request through the private tunnel: one tiny chat request holding a random marker (max_tokens 1).
    Returns the marker."""
    import http.client
    import secrets
    from urllib.parse import urlparse
    marker = "hc-canary-" + secrets.token_hex(12)
    u = urlparse(endpoint)
    body = json.dumps({"model": served_model or "dsv41", "stream": False, "max_tokens": 1, "temperature": 0,
                       "messages": [{"role": "user", "content": f"Reply with one word. {marker}"}]}).encode()
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        conn.request("POST", u.path.rstrip("/") + "/chat/completions", body, {"Content-Type": "application/json"})
        conn.getresponse().read()
    finally:
        conn.close()
    return marker


def canary_found(box, marker, run=subprocess.run):
    """[] when the box holds the marker nowhere it looked, else the problems in words (file names only)."""
    if not re.fullmatch(r"hc-canary-[0-9a-f]{24}", marker or ""):
        raise ValueError("not a canary marker")
    script = CANARY_SCRIPT % {"marker": marker, "wait": CANARY_WAIT_S}
    last = None
    for direct in (True, False):
        try:
            r = run(ssh_argv(box, direct=direct) + ["bash", "-s"], input=script.encode(), capture_output=True,
                    timeout=CANARY_WAIT_S + 25)
        except (OSError, subprocess.TimeoutExpired) as e:
            last = type(e).__name__
            continue
        if r.returncode == 124:
            return [f"{box}: whether its engine logs prompts isn't checked (the search took over {CANARY_WAIT_S} s)"]
        if r.returncode != 0:
            last = r.stderr.decode("utf-8", "replace").strip()[-200:]
            continue
        lines = r.stdout.decode("utf-8", "replace").splitlines()
        files = [x[5:] for x in lines if x.startswith("file ")]
        journal = next((x[8:].strip() for x in lines if x.startswith("journal ")), None)
        if journal is None:
            return [f"{box}: whether its engine logs prompts isn't checked (no answer)"]
        out = []
        if files:
            out.append(f"{box} keeps prompts in files ({len(files)}{'+' if len(files) >= 20 else ''}, e.g. {files[0]})")
        if journal not in ("0", ""):
            out.append(f"{box} keeps prompts in its journal ({journal} lines)")
        return out
    return [f"{box}: whether its engine logs prompts isn't checked ({last})"]


def problems(engine, by_box):
    """The engine's facts that fail, in plain words ([] when all five hold or can hold; fact 1 is the canary's)."""
    e = ENGINES[engine]
    out = []
    boxes = engine_boxes(engine)
    for box in boxes:
        f = by_box.get(box)
        if not f:
            out.append(f"{box} couldn't be checked")
            continue
        cp = str(f.get("core_pattern") or "")
        if cp.startswith("|") and "systemd-coredump" not in cp.split()[0]:  # a pipe ignores the core limit; systemd-coredump keeps it
            out.append(f"{box} sends core dumps to {cp.split()[0][1:][:80]}")
        try:
            if int(f.get("ptrace_scope")) < 1:
                out.append(f"{box} lets any process read another's memory (ptrace_scope {f.get('ptrace_scope')})")
        except (TypeError, ValueError):
            out.append(f"{box}'s ptrace setting couldn't be read")
        for s in f.get("swaps") or []:
            if not (str(s.get("dev", "")).startswith("/dev/zram") or s.get("type") == "crypt"):
                out.append(f"{box} swaps to {s.get('dev')}, which isn't encrypted")
        mine = [p for p in f.get("procs") or [] if (box == boxes[0] and str(p.get("port")) == str(e["port"]))
                or (p.get("comm") in e["comm"])]
        if not mine:
            out.append(f"{box} has no {e['name']} running")
        for p in mine:
            if str(p.get("core_soft")) != "0":
                out.append(f"{box} keeps core dumps of {p.get('comm')} (its core limit is {p.get('core_soft')})")
            m = DISK_FLAGS.search(str(p.get("args") or ""))
            if m:
                out.append(f"{box}'s {p.get('comm')} writes to disk ({m.group(0)})")
    return out


class Gate:
    """The app's Private gate: the facts per engine run (kept FACTS_TTL_S), the private tunnels, the listener check."""

    def __init__(self, fake=False, read=facts, run=subprocess.run, probe=probe, found=canary_found):
        self.fake, self.read, self.run, self.probe, self.found = fake, read, run, probe, found
        self.mu = threading.Lock()
        self.cache = {}  # (engine, run key) -> (time, problems)
        self.tunnels = {}  # local port -> Popen: engines on the same box and port (DeepSeek and uncensored) share one
        self.warming = set()  # (engine, run key) being checked in the background

    def check(self, engine, run_key, served_model=None, fresh=False):
        """[] when the engine may take a Private turn, else the problems in words."""
        if engine not in ENGINES:
            return [f"Private chats run only on DeepSeek or Qwen 3.8 on the main boxes (not {engine or 'this model'})"]
        if self.fake:
            return []
        k = (engine, str(run_key))
        with self.mu:
            hit = self.cache.get(k)
        if hit and not fresh and time.time() - hit[0] < FACTS_TTL_S:
            if time.time() - hit[0] > FACTS_TTL_S - WARM_BEFORE_S:
                self.warm(engine, run_key, served_model)  # near its end: the next answer is ready before it's needed
            return hit[1]
        by_box = {}
        for box in engine_boxes(engine):
            try:
                by_box[box] = self.read(box)
            except RuntimeError:
                by_box[box] = None
        out = problems(engine, by_box)
        if not CANARY_PROBE:
            out.append("whether its engine logs prompts isn't checked yet")
        elif not out:  # fact 1 last, and only on an engine the other four let through (its request needs the tunnel)
            try:
                marker = self.probe(self.endpoint(engine), served_model)
            except (RuntimeError, OSError, ValueError) as e:
                out.append(f"whether its engine logs prompts isn't checked ({type(e).__name__})")
            else:
                for box in engine_boxes(engine):
                    out += self.found(box, marker)
        with self.mu:
            self.cache[k] = (time.time(), out)
        return out

    def warm(self, engine, run_key, served_model=None):
        """The check in the background (on Unlock, and as a kept answer nears its end), so a Private turn rarely waits
        for the boxes. One at a time per engine run."""
        if self.fake or engine not in ENGINES:
            return
        k = (engine, str(run_key))
        with self.mu:
            if k in self.warming:
                return
            self.warming.add(k)

        def run():
            try:
                self.check(engine, run_key, served_model, fresh=True)
            except Exception:  # noqa: BLE001 - a turn checks again itself; nothing to tell here
                pass
            finally:
                with self.mu:
                    self.warming.discard(k)
        threading.Thread(target=run, name="private-gate-warm", daemon=True).start()

    # ---------------------------------------------------------------- the private tunnel
    def endpoint(self, engine):
        """The private tunnel's endpoint for an engine, started if needed; RuntimeError when it can't be. None on the
        fake app (its engine is a stand-in on this Mac: the caller uses the ordinary endpoint)."""
        if self.fake:
            return None
        e = ENGINES[engine]
        # kept by its local port, not the engine: DeepSeek V4.1's tunnel held 18580 and uncensored's own ssh found it
        # taken, so its Private turns were refused with RuntimeError
        with self.mu:
            p = self.tunnels.get(e["local"])
            if p is None or p.poll() is not None:
                p = None
                for direct in (True, False):
                    q = subprocess.Popen(ssh_argv(engine_boxes(engine)[0], "-N", "-o", "ExitOnForwardFailure=yes",
                                                  "-L", f"127.0.0.1:{e['local']}:127.0.0.1:{e['port']}", direct=direct),
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    for _ in range(50):
                        if q.poll() is not None or self.listener(e["local"]) == {q.pid}:
                            break
                        time.sleep(0.2)
                    if q.poll() is None and self.listener(e["local"]) == {q.pid}:
                        p = q
                        break
                    q.kill()
                if p is None:
                    raise RuntimeError(f"the private tunnel to {e['boxes'][0]} didn't open")
                self.tunnels[e["local"]] = p
        if self.listener(e["local"]) != {p.pid}:  # someone else listening, or the tunnel gone
            raise RuntimeError("the private model port isn't the app's own tunnel")
        return f"http://127.0.0.1:{e['local']}/v1"

    def listener(self, port):
        """The pids listening on 127.0.0.1:port (/usr/sbin/lsof, by its full path)."""
        try:
            r = self.run([LSOF, "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-Fp"], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return set()
        return {int(x[1:]) for x in r.stdout.decode().split() if x.startswith("p") and x[1:].isdigit()}

    def close(self):
        """Private locked: the private tunnels stop (their facts are kept for the engine's run)."""
        with self.mu:
            for p in self.tunnels.values():
                try:
                    p.terminate()
                except OSError:
                    pass
            self.tunnels = {}
