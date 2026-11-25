#!/usr/bin/env python3
"""The fake backend: stand-ins for the deploy's serve-rank0.sh / serve-rank1.sh and for the two ranks, so halod's
real code paths (lock protocol, start, link, health, per-request metrics, failures, stop) run without GPUs.

  fake_serve.py RANK lock [WAIT] | unlock | start [--locked] | stop | wait-linked | status
  fake_serve.py holder LOCK RUN_DIR      (the lock holder process: flock held until killed)
  fake_serve.py rank RANK RUN_DIR        (a fake rank: rank 0 serves an OpenAI-style API on SERVE_PORT)

It reads the run's conf from TF_SERVE_CONF and RUN_ID like the real scripts. Environment:
  HALO_FAKE_LOCK    the box's lock file (each fake box has its own)
  HALO_FAKE_SHARED  a directory both fake boxes share (the "link"); control files in it:
                    die-rank0 / die-rank1 (that rank exits), stall (rank 0's HTTP hangs), fail-load (rank 0 exits
                    while loading)
  HALO_FAKE_LOAD_S  seconds to "load" (default 6); HALO_FAKE_SPEED a time divisor (default 1)
Python 3.9+ standard library only. macOS and Linux.
"""
import fcntl
import hashlib
import json
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import profiles  # noqa: E402
import fake_research  # noqa: E402  (Agents' research loop on the fake)

SPEED = float(os.environ.get("HALO_FAKE_SPEED", "1"))
FAKE_MD = """Here is a small example, from the fake backend (no model ran).

## An LRU cache

`OrderedDict` keeps the order of use, so both operations are O(1):

```python
from collections import OrderedDict


class LRU:
    def __init__(self, size: int):
        self.size, self.d = size, OrderedDict()

    def get(self, key):
        if key not in self.d:
            return None
        self.d.move_to_end(key)
        return self.d[key]

    def put(self, key, value):
        self.d[key] = value
        self.d.move_to_end(key)
        if len(self.d) > self.size:
            self.d.popitem(last=False)
```

| Operation | Cost |
|---|---|
| get | O(1) |
| put | O(1) |

- **get** moves the key to the end.
- **put** drops the oldest key when full.
"""
SHARED = os.environ.get("HALO_FAKE_SHARED", "/tmp/halo-fake-shared")


def die(msg):
    print(f"fake_serve: {msg}", file=sys.stderr)
    sys.exit(1)


def conf():
    path = os.environ.get("TF_SERVE_CONF")
    if not path or not os.path.exists(path):
        die(f"no config {path}")
    c = profiles.parse_conf(open(path).read())
    for k, v in list(c.items()):
        c[k] = v.replace("$HOME", os.environ["HOME"])
    return c


def run_dir(c):
    rid = os.environ.get("RUN_ID") or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return os.path.join(c["RUN_ROOT"], rid)


def pid_of(path):
    try:
        pid = int(open(path).read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def spawn(argv, log=os.devnull):
    with open(log, "ab") as out:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True).pid


def sleep(s):
    time.sleep(s / SPEED)


# ---------------------------------------------------------------- the lock holder

def holder(lock, rd):
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX)  # blocks: the lock command decides how long to wait
    with open(os.path.join(rd, "lock.pid") + ".tmp", "w") as f:
        f.write(str(os.getpid()))
    os.replace(os.path.join(rd, "lock.pid") + ".tmp", os.path.join(rd, "lock.pid"))
    who = lock + ".who"
    with open(who, "w") as f:
        json.dump({"pid": os.getpid(), "run_dir": rd, "since": time.time(), "cmd": f"flock {lock} sh -c 'echo $$ > {rd}/lock.pid; exec sleep 86400'"}, f)

    def bye(*_):
        try:
            if json.load(open(who)).get("pid") == os.getpid():
                os.unlink(who)
        except (OSError, ValueError):
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, bye)
    while True:
        time.sleep(3600)


# ---------------------------------------------------------------- the commands

def command(rank, args):
    c = conf()
    rd = run_dir(c)
    cmd = args[0] if args else ""
    lock = os.environ.get("HALO_FAKE_LOCK", "/tmp/halo-fake.lock")
    if cmd == "lock":
        os.makedirs(rd, exist_ok=True)
        wait = float(args[1]) if len(args) > 1 and args[1] else float(c.get("LOCK_WAIT_S", 1800))
        try:
            os.unlink(os.path.join(rd, "lock.pid"))
        except OSError:
            pass
        pid = spawn([sys.executable, __file__, "holder", lock, os.path.join(rd, "lock.pid")])
        t0 = time.time()
        while time.time() - t0 < wait:
            if pid_of(os.path.join(rd, "lock.pid")):
                print(f"GPU lock held on fake rank {rank} ({pid})")
                return 0
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
        die(f"the GPU lock on fake {rank} was not free within {wait:g}s")
    if cmd == "unlock":
        pid = pid_of(os.path.join(rd, "lock.pid"))
        if pid:
            os.kill(pid, signal.SIGTERM)
        try:
            os.unlink(os.path.join(rd, "lock.pid"))
        except OSError:
            pass
        print("GPU lock released")
        return 0
    if cmd == "start":
        os.makedirs(rd, exist_ok=True)
        pf = os.path.join(rd, f"rank{rank}.pid")
        if pid_of(pf):
            die(f"rank {rank} already runs")
        avail = float(os.environ.get("HALO_FAKE_MEM_GIB", "117"))
        if avail < int(c.get("PEAK_GIB", 101)) + 8:
            die(f"rank {rank}: MemAvailable {avail:.0f} GiB, a rank needs {c.get('PEAK_GIB')} + 8 GiB")
        log = os.path.join(rd, f"rank{rank}.log")
        open(log, "w").close()
        spawn([sys.executable, __file__, "rank", str(rank), rd], log)
        for _ in range(100):
            if pid_of(pf):
                print(f"rank {rank} started ({open(pf).read().strip()}): {rd}")
                return 0
            time.sleep(0.05)
        die("the rank did not start")
    if cmd == "stop":
        pf = os.path.join(rd, f"rank{rank}.pid")
        pid = pid_of(pf)
        if not pid:
            print(f"rank {rank}: not running")
            return 0
        os.kill(pid, signal.SIGTERM)
        for _ in range(200):
            if not pid_of(pf):
                print(f"rank {rank}: stopped ({pid})")
                return 0
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
        print(f"rank {rank}: killed ({pid})")
        return 0
    if cmd == "wait-linked":
        for _ in range(600):
            if '"loaded_s"' in open(os.path.join(rd, "rank1.log")).read():
                print("rank 1 linked")
                return 0
            time.sleep(0.1)
        die("not linked")
    if cmd == "status":
        print(f"rank {rank} {'running' if pid_of(os.path.join(rd, f'rank{rank}.pid')) else 'not running'}")
        return 0
    die(f"unknown command {cmd}")


# ---------------------------------------------------------------- the fake ranks

class Engine:
    def __init__(self, c):
        self.c = c
        self.mu = threading.Lock()
        self.m = {k: 0.0 for k in ("ttft", "prefill", "decode", "latency")}
        self.n = {k: 0 for k in self.m}
        self.prompt = self.gen = self.drafted = self.accepted = 0
        self.running = 0
        self.last_prompt = []
        self.decode_rate = self.prefill_rate = 0.0
        self.cache = float(c.get("PROMPT_CACHE_GIB", "0") or 0) > 0
        self.context = int(c.get("CONTEXT", 8192))

    def metrics_text(self):
        lines = []
        names = {"ttft": "time_to_first_token_seconds", "prefill": "request_prefill_seconds",
                 "decode": "request_decode_seconds", "latency": "request_latency_seconds"}
        with self.mu:
            for k, name in names.items():
                lines += [f"# TYPE tensorfold:{name} histogram", f'tensorfold:{name}_bucket{{le="+Inf"}} {self.n[k]}',
                          f"tensorfold:{name}_sum {self.m[k]:.6f}", f"tensorfold:{name}_count {self.n[k]}"]
            lines += [f"tensorfold:requests_running {self.running}", f"tensorfold:prompt_tokens_total {self.prompt}",
                      f"tensorfold:generation_tokens_total {self.gen}", f"tensorfold:mtp_drafted_total {self.drafted}",
                      f"tensorfold:mtp_accepted_total {self.accepted}"]
        return "\n".join(lines) + "\n"

    def fit_error(self, body):
        """The engine's refusal of a request that does not fit its window (the live build's words), else None. A fake
        token is 4 characters of the request's JSON."""
        text = json.dumps(body.get("messages") or []) + json.dumps(body.get("tools") or [])
        n = max(5, (len(text) + 3) // 4)
        mt = int(body.get("max_tokens") or self.c.get("MAX_TOKENS") or 512)
        if n + mt + profiles.EXTENT_PAD > self.context:
            return {"error": {"message": f"maximum context length is {self.context - profiles.EXTENT_PAD} tokens, but the rendered "
                                         f"prompt has {n} tokens and requests {mt} reply tokens", "type": "invalid_request_error"}}
        return None

    def chat(self, body, emit=None):
        msgs = body.get("messages") or []
        text = json.dumps(msgs) + json.dumps(body.get("tools") or [])
        toks = [hashlib.md5(text[i:i + 4].encode()).hexdigest()[:4] for i in range(0, len(text), 4)]
        n = max(5, len(toks))
        mt = int(body.get("max_tokens") or self.c.get("MAX_TOKENS") or 512)
        if float(body.get("temperature") or 0) > 0 and not body.get("top_k"):  # the live build's U49
            return 500, {"error": {"message": "sampling without top_k (fake U49)"}}
        err = self.fit_error(body)
        if err:
            with open(os.path.join(SHARED, "context-refusals"), "a") as f:
                f.write(f"{n} {mt}\n")
            return 400, err
        cached = 0
        if self.cache:
            for a, b in zip(self.last_prompt, toks):
                if a != b:
                    break
                cached += 1
            cached = cached // 2048 * 2048
        self.last_prompt = toks
        with self.mu:
            self.running += 1
        t0 = time.time()
        rid = "chatcmpl-" + os.urandom(16).hex()
        try:
            prefill_s = (n - cached) / 1000.0 / SPEED + 0.05
            time.sleep(prefill_s)
            self.prefill_rate = (n - cached) / prefill_s
            ttft = time.time() - t0
            last = str((msgs[-1] if msgs else {}).get("content") or "")
            if fake_research.wants(body):  # Agents' research loop: an answer of the kind it asks for
                msg = fake_research.answer(body)
                want = 1 if body.get("max_tokens") == 1 else max(1, len(msg.get("content") or json.dumps(msg.get("tool_calls"))) // 4)
                time.sleep(min(2.0, want / 45.0) / SPEED)
                lat = time.time() - t0
                with self.mu:
                    for k, v in (("ttft", ttft), ("prefill", prefill_s), ("decode", lat - ttft), ("latency", lat)):
                        self.m[k] += v
                        self.n[k] += 1
                    self.prompt += n
                    self.gen += want
                fin = "tool_calls" if msg.get("tool_calls") else "stop"
                print(f"[tensorfold] done {rid} prompt={n} cached={cached} thinking=False effort=none tokens={want} "
                      f"sha=000000000000 finish={fin} rounds={max(1, want // 4)} accepted=0/0", flush=True)
                return 200, {"id": rid, "object": "chat.completion", "model": self.c.get("SERVE_NAME", "dsv41"),
                             "choices": [{"index": 0, "message": msg, "finish_reason": fin}],
                             "usage": {"prompt_tokens": n, "completion_tokens": want, "total_tokens": n + want,
                                       "prompt_tokens_details": {"cached_tokens": cached}}}
            tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
            done_tools = sum(1 for m in msgs if m.get("role") == "tool")
            if "list_dir" in tools and done_tools < 2:  # an agent: look around twice, then answer
                call = ("list_dir", {"path": "."}) if done_tools == 0 else ("read_file", {"path": "README.md", "limit": 20})
                time.sleep(0.3 / SPEED)
                ttft = time.time() - t0
                with self.mu:
                    for k, v in (("ttft", ttft), ("prefill", prefill_s), ("decode", 0.2), ("latency", ttft + 0.2)):
                        self.m[k] += v
                        self.n[k] += 1
                    self.prompt += n
                    self.gen += 20
                print(f"[tensorfold] done {rid} prompt={n} cached={cached} thinking=False effort=none tokens=20 sha=000000000000 "
                      f"finish=tool_calls rounds=5 accepted=12/16", flush=True)
                return 200, {"id": rid, "object": "chat.completion", "model": self.c.get("SERVE_NAME", "dsv41"),
                             "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": "",
                                          "tool_calls": [{"id": f"call_{done_tools}", "type": "function",
                                                          "function": {"name": call[0], "arguments": json.dumps(call[1])}}]}}],
                             "usage": {"prompt_tokens": n, "completion_tokens": 20, "total_tokens": n + 20}}
            sysmsg = str((msgs[0] if msgs else {}).get("content") or "") if (msgs and msgs[0].get("role") == "system") else ""
            if sysmsg.startswith("Summarise the conversation below"):  # StrixFold UI's chat compacting: the facts it was given, kept
                with open(os.path.join(SHARED, "summaries"), "a") as f:
                    f.write(f"{n}\n")
                words = [w + " " for w in ["Summary:"] + sorted(set(re.findall(r"FACT-\w+", last))) + ["(fake summary)"]]
            elif sysmsg.startswith("You are the lead for one of the user's projects. Propose the metrics"):  # the Command Centre's Propose metrics
                words = [json.dumps([{"type": "ring", "label": "Steps done in the plan", "done": 3, "total": 8, "unit": "done"},
                                     {"type": "number", "label": "Open questions", "value": 4, "change": -2, "change_label": "this week", "good": "down"},
                                     {"type": "dates", "label": "Next dates", "rows": [{"label": "Review", "in_days": 3}]}])]
            elif "capital of France" in last:
                words = "The capital of France is Paris.".split()
            elif "single word OK" in last:
                words = ["OK"]
            elif re.search(r"code|python|function|example", last, re.I):  # a Markdown reply, for the chat page
                words = re.findall(r"\S+\s*|\s+", FAKE_MD)
            else:
                words = ("This reply comes from the fake backend: no model ran. StrixFold UI drives it exactly as it "
                         "drives the real ranks, so starts, stops, metrics and failures can be tried without the GPUs. ").split()
            md = words and words[0].startswith(("Here", "Summary:"))
            want = min(mt, len(words) if (len(words) < 10 or md) else 24 + (n * 7) % 120)
            out = []
            d0 = time.time()
            for i in range(want):
                if os.path.exists(os.path.join(SHARED, "stall")):
                    while os.path.exists(os.path.join(SHARED, "stall")):
                        time.sleep(0.2)
                time.sleep(1 / 45.0 / SPEED)
                w = words[i % len(words)] if md else words[i % len(words)] + " "
                out.append(w)
                if emit:
                    emit(w)
            decode_s = max(1e-3, time.time() - d0)
            self.decode_rate = want / decode_s
            drafted = int(want * 0.9)
            acc = int(drafted * (0.72 + 0.2 * random.random()))
            lat = time.time() - t0
            with self.mu:
                for k, v in (("ttft", ttft), ("prefill", prefill_s), ("decode", decode_s), ("latency", lat)):
                    self.m[k] += v
                    self.n[k] += 1
                self.prompt += n
                self.gen += want
                self.drafted += drafted
                self.accepted += acc
            fin = "length" if want >= mt else "stop"
            sha = hashlib.sha256("".join(out).encode()).hexdigest()[:12]
            print(f"[tensorfold] done {rid} prompt={n} cached={cached} thinking=False effort=none tokens={want} sha={sha} "
                  f"finish={fin} rounds={max(1, want // 4)} accepted={acc}/{drafted}", flush=True)
            return 200, {"id": rid, "object": "chat.completion", "model": self.c.get("SERVE_NAME", "dsv41"),
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(out).strip()}, "finish_reason": fin}],
                         "usage": {"prompt_tokens": n, "completion_tokens": want, "total_tokens": n + want,
                                   "prompt_tokens_details": {"cached_tokens": cached}}}
        finally:
            with self.mu:
                self.running -= 1


def rank_main(rank, rd):
    c = conf_from(rd)
    rid = os.path.basename(rd)
    os.makedirs(SHARED, exist_ok=True)
    pf = os.path.join(rd, f"rank{rank}.pid")
    open(pf, "w").write(str(os.getpid()))
    link = os.path.join(SHARED, f"{rid}.r1")
    up = os.path.join(SHARED, f"{rid}.r0")
    gone = os.path.join(SHARED, f"{rid}.r0-exit")
    load = float(os.environ.get("HALO_FAKE_LOAD_S", "6"))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def ctl(name):
        return os.path.exists(os.path.join(SHARED, name))
    t0 = time.time()
    print(f"info: fake rank {rank}: loading (pool {c.get('CONTEXT')}, drafts {c.get('DRAFTS')})", flush=True)
    for _ in range(int(load * 10)):
        if stop.is_set():
            return
        if rank == 0 and ctl("fail-load"):
            print("error: fake rank 0: load failed (fail-load)", flush=True)
            return
        time.sleep(0.1 / SPEED)
    if rank == 1:
        open(link, "w").close()
        while not os.path.exists(up):
            if stop.is_set() or ctl("die-rank1"):
                return
            time.sleep(0.05)
        print(json.dumps({"rank": 1, "loaded_s": round(time.time() - t0, 1), "pool": int(c.get("CONTEXT", 0)),
                          "drafts": c.get("DRAFTS") == "1", "fake": True}), flush=True)
        while not stop.is_set():
            if ctl("die-rank1") or os.path.exists(gone):
                print("fake rank 1: leaving", flush=True)
                return
            time.sleep(0.1)
        return
    print(f"info: rank cache: 95.1 GiB of expert entries read whole in {load:.1f} s (fake)", flush=True)
    for _ in range(600):
        if os.path.exists(link) or stop.is_set():
            break
        time.sleep(0.05)
    if stop.is_set():
        return
    time.sleep(1.5 / SPEED)
    print("info: collectives: tcp_proxy with 8 slots of 4352 KiB (fake)", flush=True)
    eng = Engine(c)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if ctl("stall"):
                time.sleep(30)
            if self.path.startswith("/health"):
                return self._json(200, {"status": "ok", "model": c.get("SERVE_NAME", "dsv41"), "warming": False,
                                        "live": {"connections": eng.running, "waiting": 0,
                                                 "decode_tokens_per_second": round(eng.decode_rate, 1) if eng.running else 0.0,
                                                 "prefill_tokens_per_second": round(eng.prefill_rate, 1) if eng.running else 0.0}})
            if self.path.startswith("/metrics"):
                b = eng.metrics_text().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if self.path.startswith("/v1/models"):
                return self._json(200, {"object": "list", "data": [{"id": c.get("SERVE_NAME", "dsv41"), "object": "model", "owned_by": "tensorfold"}]})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not self.path.startswith("/v1/chat/completions"):
                return self._json(404, {"error": "not found"})
            if not body.get("stream"):
                code, obj = eng.chat(body)
                return self._json(code, obj)
            err = eng.fit_error(body)
            if err:  # refused before any stream, as the live server does
                with open(os.path.join(SHARED, "context-refusals"), "a") as f:
                    f.write("stream\n")
                return self._json(400, err)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def emit(w):
                self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": w}}]}).encode() + b"\n\n")
                self.wfile.flush()
            if fake_research.wants(body):  # Agents streams its requests (so a drop for the user frees the engine)
                code, obj = eng.chat(dict(body, stream=False))
                m = (obj.get("choices") or [{}])[0].get("message") or {}
                if m.get("content"):
                    emit(m["content"])
                for k, tc in enumerate(m.get("tool_calls") or []):
                    self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [dict(tc, index=k)]}}]}).encode() + b"\n\n")
                fin = "tool_calls" if m.get("tool_calls") else "stop"
                self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": fin}], "usage": obj.get("usage")}).encode()
                                 + b"\n\ndata: [DONE]\n\n")
                return
            if {"run_command", "show_file"} <= {t.get("function", {}).get("name") for t in body.get("tools") or []}:
                return chat_tools_reply(self, body)  # a chat with the container (mac/chat_box.py): one command, then the answer
            if (body.get("chat_template_kwargs") or {}).get("enable_thinking"):  # thinking on: a few reasoning deltas first
                for w in "The question is short; a plain answer with one example will do. ".split():
                    time.sleep(1 / 45.0 / SPEED)
                    self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"reasoning_content": w + " "}}]}).encode() + b"\n\n")
                    self.wfile.flush()
            code, obj = eng.chat(body, emit)
            self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": obj.get("usage")}).encode() + b"\n\ndata: [DONE]\n\n")

    srv = ThreadingHTTPServer((c.get("SERVE_HOST", "127.0.0.1"), int(c.get("SERVE_PORT", 18080))), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    open(up, "w").close()
    print(f"[tensorfold] serving {c.get('SERVE_NAME', 'dsv41')} at http://127.0.0.1:{c.get('SERVE_PORT', 18080)}/v1 "
          f"(sampling: greedy; drafts: {'on' if c.get('DRAFTS') == '1' else 'off'}; context: {c.get('CONTEXT')}; loaded in {time.time() - t0:.1f}s; fake)", flush=True)
    try:
        while not stop.is_set():
            if ctl("die-rank0"):
                print("fake rank 0: dying (die-rank0)", flush=True)
                os._exit(3)
            time.sleep(0.1)
    finally:
        open(gone, "w").close()
        srv.shutdown()


def chat_tools_reply(h, body):
    """Chat's container tools (mac/chat_box.py) on the fake: the first request runs one command that lists the
    attachments and writes a file; after its result, a short answer that quotes the command's first line."""
    msgs = body.get("messages") or []
    out = lambda o: (h.wfile.write(b"data: " + json.dumps(o).encode() + b"\n\n"), h.wfile.flush())  # noqa: E731
    results = [m for m in msgs if m.get("role") == "tool"]
    last = msgs[-1] if msgs else {}
    if results and last.get("role") == "user":  # the user's message went in while it answered (halo_chat._take_steers)
        for w in f"Got your message: {str(last.get('content') or '')[:80]}".split(" "):
            out({"choices": [{"index": 0, "delta": {"content": w + " "}}]})
        out({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 520, "completion_tokens": 9}})
    elif not results:
        asked = next((str(m.get("content") or "") for m in reversed(msgs) if m.get("role") == "user"), "")
        slow = "sleep 6; " if "slowly" in asked else ""  # a step long enough to send a message during it
        args = json.dumps({"command": slow + "ls attachments 2>/dev/null; echo 'hello from the container' > hello.txt; cat hello.txt"})
        out({"choices": [{"index": 0, "delta": {"content": "I'll try it in the container. "}}]})
        out({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_" + os.urandom(4).hex(), "type": "function",
                                                                 "function": {"name": "run_command", "arguments": args}}]}}]})
        out({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 400, "completion_tokens": 30}})
    else:
        seen = str(results[-1].get("content") or "")
        line = next((x for x in seen.splitlines() if "hello" in x and "untrusted" not in x), "nothing")
        for w in f"Done. The command printed: {line.strip()}".split(" "):
            out({"choices": [{"index": 0, "delta": {"content": w + " "}}]})
        out({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 500, "completion_tokens": 12}})
    h.wfile.write(b"data: [DONE]\n\n")
    h.wfile.flush()


def conf_from(rd):
    path = os.path.join(rd, "serve.conf")
    c = profiles.parse_conf(open(path).read())
    return {k: v.replace("$HOME", os.environ["HOME"]) for k, v in c.items()}


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "holder":
        holder(a[1], os.path.dirname(a[2]))
    elif a and a[0] == "rank":
        rank_main(int(a[1]), a[2])
    else:
        sys.exit(command(int(a[0]), a[1:]) or 0)
