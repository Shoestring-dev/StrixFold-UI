"""The agent runner (mac/agents.py): the built-in agent's read-only sandbox, the session spec checks, the scheduler's
PARALLEL cap and profile matching, the built-in loop against a small OpenAI-style server, the command adapter, stop."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import agents as A  # noqa: E402


class Model:
    """An OpenAI-style endpoint: list_dir, then read_file README.md, then an answer. Records every request body."""

    def __init__(self, delay=0.0):
        self.bodies, self.delay, self.active, self.peak = [], delay, 0, 0
        self.mu = threading.Lock()
        model = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with model.mu:
                    model.bodies.append(body)
                    model.active += 1
                    model.peak = max(model.peak, model.active)
                try:
                    time.sleep(model.delay)
                    tools = sum(1 for m in body["messages"] if m["role"] == "tool")
                    if body.get("tools") and tools == 0:
                        msg = {"role": "assistant", "content": "", "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]}
                    elif body.get("tools") and tools == 1:
                        msg = {"role": "assistant", "content": "", "tool_calls": [
                            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "../outside.txt"})}},
                            {"id": "c3", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "README.md"})}}]}
                    else:
                        msg = {"role": "assistant", "content": "The project prints hello."}
                    out = json.dumps({"id": "x", "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 100 * (tools + 1), "completion_tokens": 10,
                                                "prompt_tokens_details": {"cached_tokens": 50 * tools}}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                finally:
                    with model.mu:
                        model.active -= 1

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"


def wait(fn, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-agents-")
        self.wd = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.wd, "src"))
        open(os.path.join(self.wd, "README.md"), "w").write("# demo\nprints hello\n")
        open(os.path.join(self.tmp, "outside.txt"), "w").write("secret\n")
        self.state = {"state": "ready", "profile": {"id": "agent-coding"}, "settings": {"context": 63488, "streams": 1}}

    def runner(self, model, start_fn=None, verify=None):
        r = A.Runner(os.path.join(self.tmp, "agents"), model.url, lambda: self.state, start_fn, verify=verify)
        self.addCleanup(r.close)
        return r


class SandboxAndSpec(Base):
    def test_sandbox_stays_inside(self):
        os.symlink(os.path.join(self.tmp, "outside.txt"), os.path.join(self.wd, "link.txt"))
        sb = A.Sandbox(self.wd)
        self.assertIn("README.md", sb.call("list_dir", {}))
        self.assertIn("prints hello", sb.call("read_file", {"path": "README.md"}))
        for name, args in (("read_file", {"path": "../outside.txt"}), ("read_file", {"path": "link.txt"}),
                           ("read_file", {"path": os.path.join(self.tmp, "outside.txt")}), ("run", {"cmd": "id"})):
            with self.assertRaises(A.ToolError):
                sb.call(name, args)
        self.assertEqual(sb.call("grep", {"pattern": "secret"}), "no matches")  # the link is never read

    def test_spec_checks(self):
        ok = A.check_spec({"workdir": self.wd, "task": "x", "adapter": "builtin"})
        self.assertEqual((ok["profile"], ok["max_tokens"], ok["temperature"]), ("any", 2048, 0.0))
        for bad, key in (({"workdir": "relative", "task": "x", "adapter": "builtin"}, "workdir"),
                         ({"workdir": "/", "task": "x", "adapter": "builtin"}, "workdir"),
                         ({"workdir": self.wd, "task": "", "adapter": "builtin"}, "task"),
                         ({"workdir": self.wd, "task": "x", "adapter": "shell"}, "adapter"),
                         ({"workdir": self.wd, "task": "x", "adapter": "command", "command": "'unclosed"}, "command"),
                         ({"workdir": self.wd, "task": "x", "adapter": "command", "command": "ls", "env": "not a pair"}, "env"),
                         ({"workdir": self.wd, "task": "x", "adapter": "builtin", "max_tokens": 10}, "max_tokens")):
            with self.assertRaises(ValueError) as e:
                A.check_spec(bad)
            self.assertIn(key, e.exception.errors)


class Running(Base):
    def test_builtin_session(self):
        m = Model()
        r = self.runner(m)
        s = r.create({"name": "demo", "workdir": self.wd, "task": "What does it print?", "adapter": "builtin"})
        v = wait(lambda: next((x for x in r.view()["sessions"] if x["id"] == s["id"] and x["status"] in A.DONE), None))
        self.assertEqual(v["status"], "done", v)
        self.assertEqual(v["turns"], 3)
        self.assertEqual((v["prompt_tokens"], v["completion_tokens"], v["cached_tokens"]), (700, 30, 200))  # 100 + 200 + 400 prompt tokens over the three turns
        self.assertEqual(open(os.path.join(r.root, s["id"], "result.md")).read().strip(), "The project prints hello.")
        for b in m.bodies:  # U49 / greedy and an explicit reply cap on every request
            self.assertEqual(b["temperature"], 0)
            self.assertNotIn("top_k", b)
            self.assertEqual(b["max_tokens"], 2048)
            self.assertEqual(b["chat_template_kwargs"], {"enable_thinking": False})  # the serving profile's thinking
        tools = [x for x in m.bodies[2]["messages"] if x["role"] == "tool"]
        self.assertIn("outside the work directory", tools[1]["content"])
        self.assertIn("prints hello", tools[2]["content"])
        log = r.log_tail(s["id"])["text"]
        self.assertIn("read_file", log)

    def test_context_budget(self):
        m = Model()
        r = self.runner(m)
        self.state["settings"]["context"] = 3000
        s = r.create({"workdir": self.wd, "task": "x", "adapter": "builtin", "max_tokens": 1024})
        wait(lambda: {x["id"]: x for x in r.view()["sessions"]}[s["id"]]["status"] in A.DONE)
        self.assertEqual(len(m.bodies), 2)
        self.assertNotIn("tools", m.bodies[1])  # the window is nearly full: answer without tools
        self.assertIn("Stop using tools now", m.bodies[1]["messages"][-1]["content"])
        self.assertLessEqual(len(m.bodies[1]["messages"][-2]["content"]), 1100)  # the tool output was cut to the room left

    def test_sampled_sends_top_k(self):
        m = Model()
        r = self.runner(m)
        r.create({"workdir": self.wd, "task": "x", "adapter": "builtin", "temperature": 0.7})
        wait(lambda: all(x["status"] in A.DONE for x in r.view()["sessions"]))
        self.assertTrue(all(b["temperature"] == 0.7 and b["top_k"] == 40 for b in m.bodies))

    def test_parallel_cap_and_profile_match(self):
        m = Model(delay=0.4)
        r = self.runner(m)
        self.state["settings"]["streams"] = 2
        ids = [r.create({"workdir": self.wd, "task": f"t{i}", "adapter": "builtin"})["id"] for i in range(4)]
        other = r.create({"workdir": self.wd, "task": "x", "adapter": "builtin", "profile": "quick-chat"})["id"]
        wait(lambda: all(x["status"] in A.DONE for x in r.view()["sessions"] if x["id"] in ids), 30)
        self.assertEqual(m.peak, 2)  # never more requests at once than PARALLEL
        v = {x["id"]: x for x in r.view()["sessions"]}
        self.assertEqual(v[other]["status"], "queued")
        self.assertIn("wants quick-chat", v[other]["reason"])
        self.state["state"] = "idle"
        time.sleep(1.2)
        self.assertIn("waiting for the model", {x["id"]: x for x in r.view()["sessions"]}[other]["reason"])

    def test_never_against_a_model_halod_did_not_start(self):
        """App.model_check says the tunnel does not answer with the model halod started (a bench on its port): no
        session starts, and a running one waits before its next request instead of sending it."""
        m = Model()
        bad = {"why": "mismatch"}
        r = self.runner(m, verify=lambda st: bad["why"])
        s = r.create({"workdir": self.wd, "task": "x", "adapter": "builtin"})
        time.sleep(1.5)
        self.assertEqual(({x["id"]: x for x in r.view()["sessions"]}[s["id"]]["status"], r.parallel(self.state), len(m.bodies)), ("queued", 0, 0))
        bad["why"] = None
        wait(lambda: {x["id"]: x for x in r.view()["sessions"]}[s["id"]]["status"] == "done")
        self.assertGreater(len(m.bodies), 0)

    def test_starts_its_profile_when_idle(self):
        m = Model()
        calls = []
        self.state = {"state": "idle", "profile": None, "settings": None}
        r = self.runner(m, lambda pid: (calls.append(pid), (True, "starting"))[1])
        s = r.create({"workdir": self.wd, "task": "x", "adapter": "builtin", "profile": "quick-chat", "start_profile": True})
        wait(lambda: calls)
        time.sleep(1.5)
        self.assertEqual(calls, ["quick-chat"])  # once, not every scheduler pass
        self.state = {"state": "ready", "profile": {"id": "quick-chat"}, "settings": {"context": 16384, "streams": 1}}
        wait(lambda: {x["id"]: x for x in r.view()["sessions"]}[s["id"]]["status"] == "done")

    def test_command_adapter_and_stop(self):
        m = Model()
        r = self.runner(m)
        s = r.create({"workdir": self.wd, "task": "hello task", "adapter": "command",
                      "command": "/bin/sh -c 'echo \"$0 $OPENAI_BASE_URL $HALO_TASK $EXTRA\"' {task}", "env": "EXTRA=yes"})
        wait(lambda: {x["id"]: x for x in r.view()["sessions"]}[s["id"]]["status"] in A.DONE)
        self.assertIn(f"hello task {m.url} hello task yes", r.log_tail(s["id"])["text"])
        s2 = r.create({"workdir": self.wd, "task": "x", "adapter": "command", "command": "/bin/sleep 30"})
        pid = wait(lambda: {x["id"]: x for x in r.view()["sessions"]}[s2["id"]]["pid"])
        r.stop(s2["id"])
        v = wait(lambda: next((x for x in r.view()["sessions"] if x["id"] == s2["id"] and x["status"] in A.DONE), None))
        self.assertEqual(v["status"], "stopped")
        with self.assertRaises(OSError):
            os.kill(pid, 0)

    def test_restart_marks_running_interrupted(self):
        m = Model(delay=3)
        r = self.runner(m)
        s = r.create({"workdir": self.wd, "task": "x", "adapter": "builtin"})
        wait(lambda: {x["id"]: x for x in r.view()["sessions"]}[s["id"]]["status"] == "running")
        r.closed = True  # the dashboard went away without stopping it
        r2 = A.Runner(r.root, m.url, lambda: self.state)
        self.addCleanup(r2.close)
        self.assertEqual({x["id"]: x for x in r2.view()["sessions"]}[s["id"]]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()


class TwoCopies(Base):
    """A Qwen engine with a copy on each box (Kyojin, Gufo v0.21: `up each`): each session gets the copy fewer running
    sessions use when it starts and keeps it, and the profile's streams run on each copy."""

    def test_sessions_spread_over_the_copies_and_keep_theirs(self):
        m1, m2 = Model(delay=0.4), Model(delay=0.4)
        picks = []

        def pick(st, key, busy):
            used = [e for k, e in picks if k in busy]
            ep = min((m1.url, m2.url), key=lambda e: used.count(e))
            picks.append((key, ep))
            return ep
        seen = []
        r = A.Runner(os.path.join(self.tmp, "agents"), m1.url, lambda: self.state, verify=lambda st, endpoint=None: seen.append(endpoint),
                     pick=pick, copies=lambda st: 2)
        self.addCleanup(r.close)
        self.assertEqual(r.parallel(self.state), 2)  # one stream a copy
        ids = [r.create({"name": f"s{i}", "workdir": self.wd, "task": "What does it print?", "adapter": "builtin"})["id"] for i in range(2)]
        for sid in ids:
            wait(lambda: next((x for x in r.view()["sessions"] if x["id"] == sid and x["status"] in A.DONE), None))
        self.assertEqual(sorted(e for _, e in picks), sorted([m1.url, m2.url]))  # one session on each copy
        self.assertEqual((len(m1.bodies), len(m2.bodies)), (3, 3))  # and every turn of a session on its own copy
        self.assertTrue(set(seen) <= {m1.url, m2.url, None} and m2.url in seen)  # each copy is checked before its requests

