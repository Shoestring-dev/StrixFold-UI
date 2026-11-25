"""The agent runner: agent sessions on this Mac against the model's endpoint, at most PARALLEL at a time.

Each session has a work directory, a task, an adapter and the profile it wants serving:
  builtin  a small OpenAI-compatible tool-calling agent in this file. Its tools only read: list_dir, read_file, grep
           and find_files, confined to the work directory (paths resolve through symlinks and must stay inside it).
           It has no shell and cannot write. Its answer goes to result.md.
  command  any other CLI agent: a command (split like a shell would, never run by one) in the work directory with
           OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL set, plus the session's own KEY=VALUE lines. {task} in
           the command is replaced by the task, which is also in HALO_TASK. Its output is the session log.

Requests always send max_tokens (the reply cap) and temperature 0, or top_k with a higher temperature: the live build
answers HTTP 500 to a sampled request without top_k. A session waits while the model is not serving its profile
or every stream is busy, and retries while the pool has no room for its request. Sessions and logs live in Studio's
private store, <store>/agents/<id>/ (behind its passcode). A session without a work folder of
the user's choosing gets a private one, <store>/agents/<id>/work; a Claude Code command gets ~/HaloAgents/<id> instead
(Claude's own deny rule on the store would keep it out of a folder inside it). Python 3.9 standard library only.
"""
import fnmatch
import http.client
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse

DONE = ("done", "failed", "stopped", "interrupted")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox", "dist",
             "build", ".next", ".cache"}
TOOL_OUT_MAX = 16000
SYSTEM = (
    "You are a careful assistant working on a project in a read-only work directory, shown to you as '.'. Your tools "
    "only read: list_dir, read_file, grep and find_files. You cannot run commands or change files. Look at what you "
    "need with the tools, then answer the task in plain text, citing files and line numbers. When you are done, "
    "reply without calling a tool.")
TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "List a directory of the work directory: entries with sizes.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory, relative to the work directory. Default '.'"},
            "depth": {"type": "integer", "description": "1 to 3 levels. Default 1"}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read lines of a text file, numbered.",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string"}, "offset": {"type": "integer", "description": "First line, from 1. Default 1"},
            "limit": {"type": "integer", "description": "Lines to read, at most 800. Default 300"}}}}},
    {"type": "function", "function": {
        "name": "grep", "description": "Search text files for a regular expression (Python syntax). Returns path:line: text.",
        "parameters": {"type": "object", "required": ["pattern"], "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string", "description": "File or directory. Default '.'"},
            "glob": {"type": "string", "description": "Only files whose name matches, e.g. *.py"},
            "ignore_case": {"type": "boolean"}, "max_results": {"type": "integer", "description": "At most 200. Default 80"}}}}},
    {"type": "function", "function": {
        "name": "find_files", "description": "Find files whose path relative to the work directory matches a glob, e.g. **/*.zig or src/*.py.",
        "parameters": {"type": "object", "required": ["pattern"], "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string", "description": "Directory to search. Default '.'"}}}}},
]


class ToolError(Exception):
    pass


class Sandbox:
    """The builtin agent's read-only view of one work directory."""

    def __init__(self, root):
        self.root = os.path.realpath(root)

    def path(self, rel):
        rel = (rel or ".").strip() or "."
        full = os.path.realpath(rel if os.path.isabs(rel) else os.path.join(self.root, rel))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise ToolError(f"{rel}: outside the work directory")
        if not os.path.exists(full):
            raise ToolError(f"{rel}: no such file or directory")
        return full

    def rel(self, full):
        r = os.path.relpath(full, self.root)
        return "." if r == "." else r

    def walk(self, top):
        for d, dirs, files in os.walk(top):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not os.path.islink(os.path.join(d, x)))
            for f in sorted(files):
                full = os.path.join(d, f)
                if os.path.islink(full):
                    real = os.path.realpath(full)
                    if not real.startswith(self.root + os.sep):
                        continue  # a link out of the work directory: never read
                yield full

    def list_dir(self, path=".", depth=1):
        top = self.path(path)
        if not os.path.isdir(top):
            raise ToolError(f"{path}: not a directory")
        depth = max(1, min(3, int(depth or 1)))
        out = []

        def walk(d, lvl):
            for n in sorted(os.listdir(d)):
                if len(out) >= 400:
                    return
                p = os.path.join(d, n)
                if os.path.isdir(p):
                    out.append("  " * lvl + n + "/")
                    if lvl + 1 < depth and n not in SKIP_DIRS and not os.path.islink(p):
                        walk(p, lvl + 1)
                else:
                    out.append("  " * lvl + f"{n}  {_size(p)}")
        walk(top, 0)
        if len(out) >= 400:
            out.append("... (stopped at 400 entries)")
        return "\n".join(out) or "(empty)"

    def read_file(self, path, offset=1, limit=300):
        full = self.path(path)
        if os.path.isdir(full):
            raise ToolError(f"{path}: a directory (use list_dir)")
        offset, limit = max(1, int(offset or 1)), max(1, min(800, int(limit or 300)))
        with open(full, "rb") as f:
            if b"\0" in f.read(8192):
                raise ToolError(f"{path}: a binary file")
        out, n = [], 0
        with open(full, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if n >= offset + limit:
                    out.append(f"... (more lines after {n - 1}; read on with offset={n})")
                    break
                if n >= offset:
                    out.append(f"{n:6d}  {line.rstrip()[:600]}")
        if not out:
            return f"(the file has {n} lines; nothing at offset {offset})"
        return "\n".join(out)

    def grep(self, pattern, path=".", glob="", ignore_case=False, max_results=80):
        if not pattern or len(pattern) > 300:
            raise ToolError("pattern: 1 to 300 characters")
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"pattern: {e}")
        top = self.path(path)
        files = [top] if os.path.isfile(top) else self.walk(top)
        cap = max(1, min(200, int(max_results or 80)))
        out, seen = [], 0
        t0 = time.monotonic()
        for f in files:
            seen += 1
            if seen > 20000 or time.monotonic() - t0 > 20:
                out.append("... (search stopped: too many files or too slow; narrow path or glob)")
                break
            if glob and not fnmatch.fnmatch(os.path.basename(f), glob):
                continue
            try:
                if os.path.getsize(f) > 2_000_000:
                    continue
                with open(f, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{self.rel(f)}:{i}: {line.strip()[:300]}")
                    if len(out) >= cap:
                        out.append(f"... (stopped at {cap} matches)")
                        return "\n".join(out)
        return "\n".join(out) or "no matches"

    def find_files(self, pattern, path="."):
        if not pattern or len(pattern) > 200:
            raise ToolError("pattern: 1 to 200 characters")
        top = self.path(path)
        out = []
        pat = pattern[3:] if pattern.startswith("**/") else pattern
        for f in self.walk(top):
            r = self.rel(f)
            if fnmatch.fnmatch(r, pattern) or fnmatch.fnmatch(r, pat) or fnmatch.fnmatch(os.path.basename(f), pattern):
                out.append(r)
                if len(out) >= 500:
                    out.append("... (stopped at 500)")
                    break
        return "\n".join(out) or "no files match"

    def call(self, name, args):
        fn = {"list_dir": self.list_dir, "read_file": self.read_file, "grep": self.grep, "find_files": self.find_files}.get(name)
        if fn is None:
            raise ToolError(f"no tool {name!r}: the tools are list_dir, read_file, grep, find_files")
        if not isinstance(args, dict):
            raise ToolError("arguments must be a JSON object")
        allowed = TOOLS[[t["function"]["name"] for t in TOOLS].index(name)]["function"]["parameters"]["properties"]
        extra = set(args) - set(allowed)
        if extra:
            raise ToolError(f"unknown argument(s) {', '.join(sorted(extra))}")
        try:
            return fn(**args)
        except (TypeError, ValueError) as e:
            raise ToolError(str(e))
        except OSError as e:
            raise ToolError(f"{e.strerror or e}")


def _size(p):
    try:
        n = os.path.getsize(p)
    except OSError:
        return "?"
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KiB" if n < 1 << 20 else f"{n / (1 << 20):.1f} MiB"


class Stopped(Exception):
    pass


class Session:
    def __init__(self, runner, spec, sid=None):
        self.r = runner
        self.id = sid or time.strftime("%m%d-%H%M%S-") + secrets.token_hex(2)
        self.dir = os.path.join(runner.root, self.id)
        self.spec = spec
        self.status, self.reason = "queued", "waiting for its turn"
        self.created, self.started, self.ended = time.time(), None, None
        self.turns = self.prompt_tokens = self.completion_tokens = self.cached_tokens = 0
        self.last_prompt = 0
        self.requests = 0
        self.result = None
        self.pid = None
        self.stop_ev = threading.Event()
        self.conn = None
        self.mu = threading.Lock()

    # -- persistence
    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, "session.json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.view(), f, indent=1)
        os.replace(tmp, os.path.join(self.dir, "session.json"))

    @classmethod
    def load(cls, runner, d):
        v = json.load(open(os.path.join(d, "session.json")))
        s = cls(runner, v["spec"], v["id"])
        for k in ("status", "reason", "created", "started", "ended", "turns", "prompt_tokens", "completion_tokens",
                  "cached_tokens", "last_prompt", "requests", "result", "pid"):
            setattr(s, k, v.get(k, getattr(s, k)))
        return s

    def view(self):
        now = time.time()
        return {"id": self.id, "spec": self.spec, "status": self.status, "reason": self.reason, "created": self.created,
                "started": self.started, "ended": self.ended,
                "elapsed_s": round((self.ended or now) - self.started, 1) if self.started else None,
                "turns": self.turns, "requests": self.requests, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "cached_tokens": self.cached_tokens,
                "last_prompt": self.last_prompt, "result": self.result, "pid": self.pid}

    def log(self, text):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "log.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + text.rstrip("\n") + "\n")

    def set(self, status, reason=None):
        with self.mu:
            self.status = status
            if reason is not None:
                self.reason = reason
            if status in DONE and not self.ended:
                self.ended = time.time()
        self.save()
        if status in DONE:
            self.r.bring_home(self)

    def bring_home(self):
        """A Claude Code session's work folder ran outside the store (~/HaloAgents/<id>); once the session has ended it
        comes into <store>/agents/<id>/work: copied, checked file by file, then the outside copy removed. When the copy
        does not check out both stay, and the session's log says so. A folder the user chose stays where the user put
        it."""
        src, dst = self.spec.get("workdir"), os.path.join(self.dir, "work")
        if not self.spec.get("outside_default") or not src or not os.path.isdir(src):
            return
        if self.pid and _alive(self.pid):
            return  # its process is still at work there (a restart left it running): later
        import private_move
        import shutil
        part = dst + ".part"
        shutil.rmtree(part, ignore_errors=True)
        try:
            shutil.copytree(src, part, symlinks=True)
            same = private_move.file_digests(src) == private_move.file_digests(part)
        except OSError:
            same = False
        if not same or os.path.exists(dst):
            shutil.rmtree(part, ignore_errors=True)
            self.log("work folder: the copy into the private store did not check out; it stays outside the store, both kept")
            return
        os.chmod(part, 0o700)
        os.replace(part, dst)
        shutil.rmtree(src, ignore_errors=True)
        with self.mu:
            self.spec = dict(self.spec, workdir=os.path.realpath(dst), outside_default=False, ran_in=src)
        self.save()
        self.log("work folder: moved into the private store")

    # -- running
    def run(self):
        # the endpoint and model name of what serves now (DeepSeek: :18080 dsv41; Qwen on Gufo: :18090, its own id)
        self.endpoint, self.model = self.r.target(self.id)
        self.started = time.time()
        self.set("running", "starting")
        self.log(f"session {self.id}: {self.spec['adapter']} in {self.spec['workdir']} against {self.endpoint} ({self.model})")
        try:
            if self.spec["adapter"] == "builtin":
                self.run_builtin()
            else:
                self.run_command()
        except Stopped:
            self.log("stopped")
            self.set("stopped", "stopped from the dashboard")
        except Exception as e:  # noqa: BLE001 - shown to the user, never kills the runner
            self.log(f"failed: {type(e).__name__}: {e}")
            self.set("failed", f"{type(e).__name__}: {e}"[:300])
        finally:
            self.r.kick.set()

    def stop(self):
        self.stop_ev.set()
        c = self.conn
        if c is not None and c.sock is not None:
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        pid = self.pid
        if pid:
            for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 0)):
                try:
                    os.killpg(pid, sig)
                except OSError:
                    break
                for _ in range(wait * 10):
                    try:
                        os.killpg(pid, 0)
                    except OSError:
                        break
                    time.sleep(0.1)
            if self.status == "interrupted":
                self.pid = None
                self.reason += "; stopped its process"
                self.save()
        if self.status in ("queued",):
            self.set("stopped", "stopped before it started")

    # -- the builtin agent
    def post(self, body):
        """One chat request; retried while the pool has no room or the endpoint is briefly away. Returns the reply."""
        u = urlparse(self.endpoint)
        waited = 0.0
        while True:
            if self.stop_ev.is_set():
                raise Stopped()
            st = self.r.state_fn() or {}
            bad = self.r.check(st, self.endpoint)
            if bad and self.r.pick:  # its copy is not answering: the runner's pick moves it to one that is
                ep, _ = self.r.target(self.id)
                if ep != self.endpoint:
                    self.log(f"moved to {ep}: {self.endpoint} {bad}")
                    self.endpoint, u = ep, urlparse(ep)
                    continue
            if bad and waited < 900:  # the model halod started is not the one answering (a bench on its port): wait
                self.reason = "waiting: the endpoint is not answering with the model halod started"
                self.log(f"retry in 5 s: the endpoint check says {bad}")
                if self.stop_ev.wait(5):
                    raise Stopped()
                waited += 5
                continue
            if bad:
                raise RuntimeError("the endpoint never answered with the model halod started")
            c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=900)
            self.conn = c
            try:
                c.request("POST", u.path.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                          {"Content-Type": "application/json", "Authorization": "Bearer none"})
                resp = c.getresponse()
                data = resp.read()
                code = resp.status
            except (OSError, http.client.HTTPException) as e:
                if self.stop_ev.is_set():
                    raise Stopped()
                code, data = None, str(e).encode()
            finally:
                self.conn = None
                c.close()
            self.requests += 1
            if code == 200:
                return json.loads(data)
            text = data.decode("utf-8", "replace")[:400]
            room = code in (400, 429, 503) and re.search(r"fill the context|ContextFull|no free slot|NoFreeSlot|busy", text, re.I)
            if (code is None or room) and waited < 900:
                why = "the pool has no room for this request yet (another stream holds it)" if room else f"the endpoint is not answering ({text[:120]})"
                self.reason = f"waiting: {why}"
                self.log(f"retry in 5 s: {why}")
                if self.stop_ev.wait(5):
                    raise Stopped()
                waited += 5
                continue
            raise RuntimeError(f"HTTP {code}: {text}")

    def run_builtin(self):
        sb = Sandbox(self.spec["workdir"])
        sp = self.spec
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Work directory: {os.path.basename(sb.root) or sb.root}\n\nTask:\n{sp['task']}"}]
        context = self.r.context() or 16384
        mt = int(sp.get("max_tokens") or 2048)
        sampling = {"temperature": 0}
        if float(sp.get("temperature") or 0) > 0:
            sampling = {"temperature": float(sp["temperature"]), "top_k": int(sp.get("top_k") or 40)}
        thinking = self.r.thinking()
        final_only = False
        for turn in range(1, int(sp.get("max_turns") or 40) + 2):
            if self.stop_ev.is_set():
                raise Stopped()
            last_turn = turn > int(sp.get("max_turns") or 40)
            if last_turn or final_only:
                msgs.append({"role": "user", "content": "Stop using tools now and give your answer with what you have."})
            body = dict({"model": self.model, "messages": msgs, "max_tokens": mt}, **sampling)
            # the serving profile's thinking setting, sent per request: Gufo (Qwen) thinks unless told not to
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
            if not (last_turn or final_only):
                body["tools"], body["tool_choice"] = TOOLS, "auto"
            self.reason = f"turn {turn}: waiting for the model"
            self.save()
            reply = self.post(body)
            self.turns = turn
            u = reply.get("usage") or {}
            self.prompt_tokens += u.get("prompt_tokens") or 0
            self.completion_tokens += u.get("completion_tokens") or 0
            self.cached_tokens += ((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            self.last_prompt = u.get("prompt_tokens") or self.last_prompt
            ch = (reply.get("choices") or [{}])[0]
            m = ch.get("message") or {}
            calls = m.get("tool_calls") or []
            content = m.get("content") or ""
            msgs.append({"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})})
            if content.strip():
                self.log(f"turn {turn}: {content.strip()[:1500]}")
            if not calls or last_turn or final_only:
                self.result = content.strip() or "(empty answer)"
                with open(os.path.join(self.dir, "result.md"), "w", encoding="utf-8") as f:
                    f.write(self.result + "\n")
                self.log(f"answer written to result.md ({len(self.result)} characters, finish {ch.get('finish_reason')})")
                self.set("done", f"answered in {turn} turn{'s' if turn != 1 else ''}")
                self.save_transcript(msgs)
                return
            # the next request must fit the window: prompt + max_tokens + 18 <= context (the engine's extent rule).
            # Tool output shares what is left, at a conservative 3 characters a token.
            room = context - mt - 18 - (self.last_prompt + (u.get("completion_tokens") or 0)) - 1500
            cap = min(TOOL_OUT_MAX, max(0, room) * 3 // max(1, len(calls)))
            for c in calls:
                fn = c.get("function") or {}
                name = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    out = sb.call(name, args)
                except (ValueError, ToolError) as e:
                    args, out = fn.get("arguments"), f"error: {e}"
                if cap < 400:
                    out = "error: no room left in the context window for this output; answer with what you have"
                elif len(out) > cap:
                    out = out[:cap] + f"\n... (cut at {cap} characters; ask for less)"
                self.log(f"turn {turn}: {name} {json.dumps(args)[:200]} -> {out.count(chr(10)) + 1} lines")
                msgs.append({"role": "tool", "tool_call_id": c.get("id") or name, "content": out})
            self.save_transcript(msgs)
            if cap < 2000:  # near the window's end: the next request answers without tools
                final_only = True
                self.log(f"context nearly full ({self.last_prompt:,} of {context:,} tokens): asking for the answer")

    def save_transcript(self, msgs):
        with open(os.path.join(self.dir, "transcript.json"), "w", encoding="utf-8") as f:
            json.dump(msgs, f, indent=1)

    # -- the command adapter
    def run_command(self):
        sp = self.spec
        argv = [a.replace("{task}", sp["task"]) for a in shlex.split(sp["command"])]
        env = dict(os.environ)
        env.update({"OPENAI_BASE_URL": self.endpoint, "OPENAI_API_BASE": self.endpoint, "OPENAI_API_KEY": "none",
                    "OPENAI_MODEL": self.model, "HALO_TASK": sp["task"], "HALO_MAX_TOKENS": str(sp.get("max_tokens") or 2048)})
        env.update(parse_env(sp.get("env") or ""))
        log = open(os.path.join(self.dir, "log.txt"), "ab")
        log.write(f"{time.strftime('%H:%M:%S ')}$ {shlex.join(argv)}\n".encode())
        log.flush()
        try:
            p = subprocess.Popen(argv, cwd=sp["workdir"], env=env, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            log.close()
            raise RuntimeError(f"cannot start {argv[0]}: {e.strerror or e}")
        self.pid = p.pid
        self.reason = f"running {os.path.basename(argv[0])} (pid {p.pid})"
        self.save()
        rc = p.wait()
        log.close()
        self.pid = None
        if self.stop_ev.is_set():
            raise Stopped()
        self.log(f"exited with {rc}")
        self.set("done" if rc == 0 else "failed", f"exit code {rc}")


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k.strip()):
            raise ValueError(f"environment line {line[:40]!r}: KEY=VALUE")
        out[k.strip()] = v.strip()
    return out


class Runner:
    """The sessions and their scheduler: queued sessions start in order while the model serves what they want and
    fewer than PARALLEL run."""

    def __init__(self, root, endpoint, state_fn, start_fn=None, model_name="dsv41", verify=None, outside=None, pick=None,
                 copies=None):
        """endpoint: the base URL, or a function of halod's state that maps its endpoint to this Mac's. verify(state):
        None when the endpoint answers with the model halod started (App.model_check), else why not. pick(state, key,
        busy) and copies(state): a model served by a copy on each box (App.endpoint_for, App.replicas): each session gets
        one copy when it starts and keeps it, and twice the profile's streams run at once."""
        self.root, self.endpoint, self.state_fn, self.start_fn, self.model_name = root, endpoint, state_fn, start_fn, model_name
        self.pick, self.copies = pick, copies
        self.outside = outside or os.path.expanduser("~/HaloAgents")  # work folders for Claude Code commands
        self.verify = verify or (lambda st: None)
        os.makedirs(root, exist_ok=True)
        self.sessions = {}
        self.mu = threading.Lock()
        self.kick = threading.Event()
        self.closed = False
        self.started_profile = None
        for d in sorted(os.listdir(root)):
            try:
                s = Session.load(self, os.path.join(root, d))
            except (OSError, ValueError, KeyError):
                continue
            if s.status == "running":
                s.status, s.reason = "interrupted", "the dashboard restarted while it ran" + (
                    f" (its process {s.pid} was left running)" if s.pid and _alive(s.pid) else "")
                s.ended = s.ended or time.time()
                s.save()
            self.sessions[s.id] = s
            if s.status in DONE:
                s.bring_home()  # a Claude Code folder left outside when the app stopped
        threading.Thread(target=self.loop, name="agents", daemon=True).start()

    def target(self, sid=None):
        st = self.state_fn() or {}
        if self.pick and sid is not None:  # a copy on each box: the one fewer running sessions use, kept for the session
            busy = {("agent", s.id) for s in list(self.sessions.values()) if s.status == "running" and s.id != sid}
            return self.pick(st, ("agent", sid), busy), st.get("served_model") or self.model_name
        ep = self.endpoint(st) if callable(self.endpoint) else self.endpoint
        return ep, st.get("served_model") or self.model_name

    def check(self, st, endpoint):
        """verify(state) for the copy a session uses (App.model_check takes the endpoint)."""
        return self.verify(st, endpoint=endpoint) if self.pick else self.verify(st)

    def context(self):
        st = self.state_fn() or {}
        return (st.get("settings") or {}).get("context")

    def thinking(self):
        st = self.state_fn() or {}
        return bool((st.get("settings") or {}).get("thinking"))

    def parallel(self, st):
        if not st or st.get("state") != "ready" or self.verify(st):
            return 0  # nothing starts against a model halod did not start
        return int((st.get("settings") or {}).get("streams") or 1) * (self.copies(st) if self.copies else 1)

    def bring_home(self, s):
        try:
            s.bring_home()
        except Exception as e:  # noqa: BLE001 - never the session's end itself
            s.log(f"work folder: not moved ({type(e).__name__})")

    def create(self, spec):
        spec = check_spec(spec)
        s = Session(self, spec)
        if not spec["workdir"]:  # a private folder of its own: in the store, or beside it for Claude Code
            wd = os.path.join(self.outside, s.id) if claude_code(spec) else os.path.join(s.dir, "work")
            os.makedirs(wd, mode=0o700, exist_ok=True)
            os.chmod(wd, 0o700)
            spec["workdir"] = os.path.realpath(wd)
            spec["outside_default"] = claude_code(spec)  # comes into the store when the session ends (bring_home)
        with self.mu:
            self.sessions[s.id] = s
        s.save()
        s.log(f"queued: {spec['name']}")
        self.kick.set()
        return s.view()

    def stop(self, sid):
        s = self.sessions.get(sid)
        if not s:
            raise KeyError(sid)
        threading.Thread(target=s.stop, daemon=True).start()
        return {"ok": True}

    def stop_all(self):
        n = 0
        for s in list(self.sessions.values()):
            if s.status not in DONE:
                threading.Thread(target=s.stop, daemon=True).start()
                n += 1
        return {"ok": True, "stopping": n}

    def remove(self, sid):
        s = self.sessions.get(sid)
        if not s:
            raise KeyError(sid)
        if s.status not in DONE:
            raise ValueError("stop it first")
        with self.mu:
            del self.sessions[sid]
        import shutil
        shutil.rmtree(s.dir, ignore_errors=True)
        return {"ok": True}

    def view(self):
        st = self.state_fn()
        rows = sorted((s.view() for s in self.sessions.values()), key=lambda v: v["created"], reverse=True)
        running = sum(1 for v in rows if v["status"] == "running")
        return {"sessions": rows, "running": running, "parallel": self.parallel(st),
                "serving": ((st or {}).get("profile") or {}).get("id") if st and st.get("state") == "ready" else None}

    def log_tail(self, sid, offset=0, limit=200_000):
        s = self.sessions.get(sid)
        if not s:
            raise KeyError(sid)
        p = os.path.join(s.dir, "log.txt")
        try:
            size = os.path.getsize(p)
        except OSError:
            return {"offset": 0, "text": ""}
        offset = max(0, min(int(offset), size))
        if size - offset > limit:
            offset = size - limit
        with open(p, "rb") as f:
            f.seek(offset)
            data = f.read()
        return {"offset": offset + len(data), "text": data.decode("utf-8", "replace"), "size": size}

    def loop(self):
        while not self.closed:
            self.kick.wait(1.0)
            self.kick.clear()
            try:
                self.schedule()
            except Exception:  # noqa: BLE001
                pass

    def schedule(self):
        queued = sorted((s for s in self.sessions.values() if s.status == "queued"), key=lambda s: s.created)
        if not queued:
            return
        st = self.state_fn()
        state = (st or {}).get("state")
        serving = ((st or {}).get("profile") or {}).get("id") if st else None
        running = sum(1 for s in self.sessions.values() if s.status == "running")
        free = self.parallel(st) - running
        for s in queued:
            want = s.spec.get("profile") or "any"
            if st is None:
                s.reason = "waiting: no contact with halod"
            elif state in ("idle", "failed") and want != "any" and s.spec.get("start_profile") and self.start_fn:
                if self.started_profile != want:
                    self.started_profile = want
                    ok, msg = self.start_fn(want)
                    s.log(f"started profile {want}: {msg}" if ok else f"could not start profile {want}: {msg}")
                s.reason = f"waiting: starting {want}"
            elif state != "ready":
                s.reason = f"waiting for the model ({state})" + ("" if want == "any" else f"; wants {want}")
            elif want != "any" and serving != want:
                s.reason = f"waiting: {serving} is serving, this session wants {want}"
            elif free <= 0:
                s.reason = f"waiting for a free stream ({running} of {self.parallel(st)} in use)"
            else:
                free -= 1
                running += 1
                s.status = "running"
                threading.Thread(target=s.run, name=f"agent-{s.id}", daemon=True).start()
                continue
        if state == "ready":
            self.started_profile = None

    def close(self):
        self.closed = True
        for s in self.sessions.values():
            if s.status == "running":
                s.stop()


def claude_code(spec):
    """A command adapter that runs Claude Code: a deny rule of Claude's on the private store
    would keep it out of a work folder there, so its default folder is outside the store."""
    if spec.get("adapter") != "command":
        return False
    try:
        argv = shlex.split(spec.get("command") or "")
    except ValueError:
        return False
    return any(os.path.basename(a) in ("claude", "claude-code") or "claude-code" in a for a in argv[:3])


def check_spec(spec):
    """A session spec, checked: plain errors for the form."""
    if not isinstance(spec, dict):
        raise ValueError("a session is an object")
    out, errors = {}, {}
    out["adapter"] = spec.get("adapter") if spec.get("adapter") in ("builtin", "command") else None
    if not out["adapter"]:
        errors["adapter"] = "builtin or command"
    wd = os.path.expanduser(str(spec.get("workdir") or "").strip())
    if not wd:
        pass  # a private folder of its own (Runner.create)
    elif not os.path.isabs(wd):
        errors["workdir"] = "an absolute path (~ allowed), or empty for a private folder"
    elif not os.path.isdir(wd):
        errors["workdir"] = "no such directory"
    elif os.path.realpath(wd) == "/":
        errors["workdir"] = "not the whole disk"
    out["workdir"] = os.path.realpath(wd) if wd else wd
    task = str(spec.get("task") or "").strip()
    if not task or len(task) > 20000:
        errors["task"] = "1 to 20,000 characters"
    out["task"] = task
    name = str(spec.get("name") or "").strip()[:80] or (task.splitlines()[0][:60] if task else "agent")
    out["name"] = name
    prof = str(spec.get("profile") or "any")
    if not re.fullmatch(r"[a-z0-9-]{1,80}", prof):
        errors["profile"] = "a profile id or any"
    out["profile"] = prof
    out["start_profile"] = bool(spec.get("start_profile"))
    for k, lo, hi, dflt in (("max_turns", 1, 200, 40), ("max_tokens", 64, 16384, 2048)):
        try:
            v = int(spec.get(k) or dflt)
            if not lo <= v <= hi:
                raise ValueError
            out[k] = v
        except (TypeError, ValueError):
            errors[k] = f"a whole number {lo:,} to {hi:,}"
    try:
        t = float(spec.get("temperature") or 0)
        if not 0 <= t <= 2:
            raise ValueError
        out["temperature"] = t
        out["top_k"] = int(spec.get("top_k") or 40)
        if not 1 <= out["top_k"] <= 56:
            raise ValueError
    except (TypeError, ValueError):
        errors["temperature"] = "temperature 0 to 2 (above 0 always sends top_k 1 to 56)"
    if out.get("adapter") == "command":
        cmd = str(spec.get("command") or "").strip()
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            argv = []
            errors["command"] = f"cannot split: {e}"
        if not argv and "command" not in errors:
            errors["command"] = "a command to run"
        out["command"] = cmd
        out["env"] = str(spec.get("env") or "")
        try:
            parse_env(out["env"])
        except ValueError as e:
            errors["env"] = str(e)
    if errors:
        e = ValueError("; ".join(f"{k}: {v}" for k, v in errors.items()))
        e.errors = errors
        raise e
    return out


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
