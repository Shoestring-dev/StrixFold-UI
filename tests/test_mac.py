"""The Mac server's guards (mac/halo_control.py): writes need the install token, a localhost Host, a same-origin Origin
and JSON; reads need a localhost Host; the agent endpoints sit behind the same checks; the page carries the token."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import halo_control as HC  # noqa: E402
import site_fixture  # noqa: E402
import agents  # noqa: E402


def unlocked(app, tmp):
    """Studio's session for the test client (Lab chat and agents live in the private store)."""
    import studio
    st = studio.Store(os.path.join(tmp, "StrixFoldUI-test"))
    app.studio = {"store": st, "auth": studio.Auth(st)}
    app.studio["auth"].setup("correct horse", True)
    tok, _ = app.studio["auth"].new_session(True, "test", "127.0.0.1")
    return f"{studio.COOKIE}={tok}"

class Guards(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="hc-mac-")
        HC.CONF_DIR = tmp
        self.wd = os.path.join(tmp, "proj")
        os.makedirs(self.wd)
        cfg = dict(HC.DEFAULTS, ui_port=0, halod_local_port=1)  # halod unreachable: reads of /api fail cleanly
        self.app = HC.App(cfg)
        self.app.runner = agents.Runner(os.path.join(tmp, "agents"), "http://127.0.0.1:1/v1", lambda: None)
        self.addCleanup(self.app.runner.close)
        self.cookie = unlocked(self.app, tmp)
        HC.Handler.A = self.app
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), HC.Handler)
        self.port = self.srv.server_address[1]
        cfg["ui_port"] = self.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.host = f"127.0.0.1:{self.port}"

    def req(self, method, path, body=None, **h):
        headers = {"Host": self.host, "Cookie": self.cookie}
        headers.update({k.replace("_", "-"): v for k, v in h.items()})
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", None if body is None else json.dumps(body).encode(),
                                   headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_page_and_hosts(self):
        code, page = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(self.app.ui_token.encode(), page)
        self.assertEqual(self.req("GET", "/", Host="evil.example")[0], 403)  # DNS rebinding
        self.assertEqual(self.req("GET", "/local/agents", Host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/ui/../../halo_control.py")[0], 404)

    def test_writes_need_token_origin_json(self):
        spec = {"workdir": self.wd, "task": "x", "adapter": "command", "command": "/usr/bin/true"}
        tok = self.app.ui_token
        cases = [({}, 403), ({"X_Halo_UI_Token": "wrong", "Content_Type": "application/json"}, 403),
                 ({"X_Halo_UI_Token": tok, "Content_Type": "text/plain"}, 403),
                 ({"X_Halo_UI_Token": tok, "Content_Type": "application/json", "Origin": "http://evil.example"}, 403),
                 ({"X_Halo_UI_Token": tok, "Content_Type": "application/json", "Host": "evil.example"}, 403)]
        for h, want in cases:
            self.assertEqual(self.req("POST", "/local/agents", spec, **h)[0], want, h)
        self.assertEqual(self.app.runner.sessions, {})  # nothing was queued by a refused request
        code, body = self.req("POST", "/local/agents", spec, X_Halo_UI_Token=tok, Content_Type="application/json",
                              Origin=f"http://{self.host}")
        self.assertEqual(code, 200, body)
        sid = json.loads(body)["session"]["id"]
        code, body = self.req("POST", "/local/agents", {"workdir": "rel", "task": "", "adapter": "x"},
                              X_Halo_UI_Token=tok, Content_Type="application/json")
        self.assertEqual(code, 400)
        self.assertEqual(set(json.loads(body)["errors"]), {"workdir", "task", "adapter"})
        self.assertEqual(self.req("POST", f"/local/agents/{sid}/stop", {}, Content_Type="application/json")[0], 403)
        self.assertEqual(self.req("POST", f"/local/agents/{sid}/stop", {}, X_Halo_UI_Token=tok, Content_Type="application/json")[0], 200)

    def test_api_proxy_without_halod(self):
        code, body = self.req("GET", "/api/state")
        self.assertEqual(code, 502)
        self.assertTrue(json.loads(body)["unreachable"])


class Copies(unittest.TestCase):
    """box2's copy of a one-per-box Qwen engine (the lead): the :18095 tunnel, and one copy per
    conversation, picked when it starts, kept, and given up only when that copy stops answering."""

    def setUp(self):
        cfg = dict(HC.DEFAULTS, ui_port=0, halod_local_port=1)
        self.app = HC.App(cfg)
        self.bad = set()
        self.app.model_check = lambda st, model_id=None, endpoint=None: "unreachable" if endpoint in self.bad else None

    def st(self, model_id, run="r1"):
        return {"state": "ready", "run_id": run, "endpoint": "http://127.0.0.1:18090/v1", "served_model": "Qwen3.8-Flash-Yamz",
                "profile": {"id": f"{model_id}-64k", "model_id": model_id}}

    def test_replicas(self):
        one, two = "http://127.0.0.1:18090/v1", "http://127.0.0.1:18095/v1"
        self.assertEqual(self.app.replicas(self.st("qwen38-kyojin")), [one, two])
        self.assertEqual(self.app.replicas(self.st("qwen38-gufo21")), [one, two])
        self.assertEqual(self.app.replicas(self.st("qwen38")), [one])  # Gufo's halo-qwen: box1 only
        ds = {"state": "ready", "run_id": "r", "endpoint": "http://127.0.0.1:18080/v1", "profile": {"model_id": "dsv41"}}
        self.assertEqual(self.app.replicas(ds), ["http://127.0.0.1:18080/v1"])
        self.assertEqual([(t["endpoint3_host"], t["endpoint3_local_port"], t["endpoint3_port"]) for t in [HC.DEFAULTS]],
                         [("box2", 18095, 18090)])

    def test_affinity(self):
        one, two = "http://127.0.0.1:18090/v1", "http://127.0.0.1:18095/v1"
        st = self.st("qwen38-kyojin")
        a = self.app.endpoint_for(st, ("chat", 1), busy=set())
        b = self.app.endpoint_for(st, ("chat", 2), busy={("chat", 1)})
        self.assertEqual((a, b), (one, two))  # the second conversation goes to the copy the first is not on
        self.assertEqual(self.app.endpoint_for(st, ("chat", 1), busy={("chat", 2)}), one)  # kept
        self.assertEqual(self.app.endpoint_for(st, ("chat", 2), busy=set()), two)
        c = self.app.endpoint_for(st, ("agent", "x"), busy={("chat", 2)})  # box1's chat is idle: box1 is freer
        self.assertEqual(c, one)
        self.bad.add(two)  # box2's copy stops answering: its conversation goes back to box1 and stays
        self.assertEqual(self.app.endpoint_for(st, ("chat", 2), busy=set()), one)
        self.bad.clear()
        self.assertEqual(self.app.endpoint_for(st, ("chat", 2), busy=set()), one)
        st2 = self.st("qwen38-kyojin", run="r2")  # a new run: picked afresh
        self.assertEqual(self.app.endpoint_for(st2, ("chat", 2), busy=set()), one)
        self.assertEqual(self.app.endpoint_for(st2, ("chat", 3), busy={("chat", 2)}), two)
        self.assertEqual(self.app.endpoint_for(self.st("qwen38"), ("chat", 9)), one)  # one copy: always it

    def test_a_dropped_copy(self):
        """halod keeps serving from one copy when the other box fails ("copies"): the Mac sends every conversation,
        pinned or new, to the copy that is left, and halod's own endpoint maps to it."""
        one, two = "http://127.0.0.1:18090/v1", "http://127.0.0.1:18095/v1"
        st = self.st("qwen38-kyojin")
        self.assertEqual((self.app.endpoint_for(st, ("chat", 1)), self.app.endpoint_for(st, ("chat", 2), busy={("chat", 1)})), (one, two))
        only1 = dict(st, copies=["box1"])
        self.assertEqual(self.app.replicas(only1), [one])
        self.assertEqual(self.app.endpoint_for(only1, ("chat", 2)), one)  # moved off the dropped copy
        only2 = dict(st, copies=["box2"])
        self.assertEqual((self.app.replicas(only2), self.app.local_endpoint(only2)), ([two], two))
        self.assertEqual(self.app.endpoint_for(only2, ("chat", 1)), two)


class ExtraPair(unittest.TestCase):
    """The extra pair: a chat goes to box5+box4 while their halod serves the chat's model and
    their tunnel answers with it; a Private chat, another model or a pair that is not ready stays on the main boxes."""

    def setUp(self):
        cfg = dict(HC.DEFAULTS, ui_port=0, halod_local_port=1, pairs=site_fixture.PAIRS)
        self.app = HC.App(cfg)
        self.bos = {"state": "ready", "run_id": "b1", "endpoint": "http://127.0.0.1:18080/v1", "served_model": "dsv41",
                    "profile": {"id": "unc-chat", "model_id": "dsv41-unc"}, "pair": "box-b"}
        self.halos = {"state": "ready", "run_id": "h1", "endpoint": "http://127.0.0.1:18080/v1", "served_model": "dsv41",
                      "profile": {"id": "quick-chat", "model_id": "dsv41"}}
        self.app.pair_state = lambda name, fresh=False: self.bos if name == "box-b" else None
        self.why = None
        self.app.model_check = lambda st, model_id=None, endpoint=None: self.why if st.get("pair") == "box-b" else None

    def test_route(self):
        A = self.app
        self.assertIs(A.chat_state("dsv41-unc", self.halos), self.bos)
        self.assertEqual(A.local_endpoint(self.bos), "http://127.0.0.1:18081/v1")
        self.assertEqual(A.endpoint_for(self.bos, ("chat", 1)), "http://127.0.0.1:18081/v1")
        self.assertEqual(A.local_endpoint(self.halos), "http://127.0.0.1:18080/v1")
        self.assertIs(A.chat_state("dsv41", self.halos), self.halos)  # the official model: the main boxes
        self.assertIs(A.chat_state("dsv41-unc", self.halos, private=True), self.halos)  # Private: never the extra pairs
        self.why = "mismatch"  # their tunnel answers with something else: held off them
        self.assertIs(A.chat_state("dsv41-unc", self.halos), self.halos)
        self.why = None
        self.bos["state"] = "loading"
        self.assertIs(A.chat_state("dsv41-unc", self.halos), self.halos)
        self.bos = None  # their halod does not answer: the chat waits for them a while (the main boxes serve another model)
        self.assertEqual(A.chat_state("dsv41-unc", self.halos)["state"], "waiting")
        A.pairs["box-b"].pop("good")  # never seen serving: the main boxes' state
        self.assertIs(A.chat_state("dsv41-unc", self.halos), self.halos)

    def test_the_window_the_chat_chose(self):
        """the user: a 256K chat showed "55K of 63K": the main boxes' 63K run stood in for the extra pairs' 256K one."""
        A = self.app
        self.bos["profile"]["settings"] = {"context": 262144}
        halos = {"state": "ready", "run_id": "h2", "served_model": "dsv41",
                 "profile": {"id": "unc-chat", "model_id": "dsv41-unc", "settings": {"context": 63488}}}
        self.assertIs(A.chat_state("dsv41-unc", halos, want=262144), self.bos)
        self.assertIs(A.chat_state("dsv41-unc", halos, want=65536), self.bos)  # a window the main boxes don't have
        self.assertIs(A.chat_state("dsv41-unc", halos, want=32768, key="c1"), halos)  # it fits: the main boxes first
        import conductor
        old_c = conductor.C
        self.addCleanup(setattr, conductor, "C", old_c)
        conductor.C = conductor.Conductor(A)
        conductor.C.live[1] = {"id": 1, "pair": "halos", "kind": "chat", "key": "c2", "who": "the user's chat", "since": 0}
        self.assertIs(A.chat_state("dsv41-unc", halos, want=32768, key="c1"), self.bos)  # their one stream is c2's
        self.assertIs(A.chat_state("dsv41-unc", halos, want=32768, key="c2"), halos)  # c2 itself stays
        conductor.C.live.clear()
        gone = self.bos
        self.bos = None  # their tunnel is coming back (an app restart): a 256K chat waits for them
        st = A.chat_state("dsv41-unc", halos, want=262144)
        self.assertEqual((st["state"], st["pair"], st["profile"]["settings"]["context"]), ("waiting", "box-b", 262144))
        self.assertIs(A.chat_state("dsv41-unc", halos, want=63488), halos)  # a chat that fits the main boxes goes there
        self.assertIs(A.chat_state("dsv41-unc", halos, want=262144, private=True), halos)
        HC.PAIR_GRACE_S, old = -1, HC.PAIR_GRACE_S  # long gone: the main boxes' window is what there is
        self.addCleanup(setattr, HC, "PAIR_GRACE_S", old)
        self.assertIs(A.chat_state("dsv41-unc", halos, want=262144), halos)
        self.bos = gone


class PidFile(unittest.TestCase):
    """The real dashboard writes its own PID file (launchd starts it with no launcher to write one); a fake or test
    dashboard on its own port never claims it."""

    def test_only_the_real_dashboard_claims_it(self):
        self.assertEqual(HC.pid_file(False, ["serve"]), HC.PID_FILE)
        self.assertIsNone(HC.pid_file(True, ["serve", "--fake"]))
        self.assertIsNone(HC.pid_file(False, ["serve", "--port", "18711"]))

    def test_written_whole(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "server.pid")
            HC.write_pid(p)
            self.assertEqual(open(p).read(), f"{os.getpid()}\n")
            self.assertEqual(os.listdir(os.path.dirname(p)), ["server.pid"])
            HC.write_pid(None)  # nothing to write

    def test_login_item_renders(self):
        import plistlib
        import subprocess
        src = open(os.path.join(ROOT, "mac", "launchd", "com.halo-control.dashboard.plist")).read()
        out = (src.replace("__PY__", "/usr/bin/python3").replace("__SERVER__", "/x/mac/halo_control.py")
               .replace("__LOG__", "/x/halo-control.log").replace("__PATH__", "/u/.local/bin:/usr/bin:/bin"))
        d = plistlib.loads(out.encode())
        self.assertEqual(d["EnvironmentVariables"]["PATH"], "/u/.local/bin:/usr/bin:/bin")
        self.assertEqual(d["ProcessType"], "Standard")
        self.assertEqual(d["ProgramArguments"][-2:], ["/x/mac/halo_control.py", "serve"])
        launcher = open(os.path.join(ROOT, "bin", "halo-control")).read()
        self.assertIn("s|__PATH__|$PATH|", launcher)
        self.assertEqual(subprocess.run(["bash", "-n", os.path.join(ROOT, "bin", "halo-control")]).returncode, 0)


class LockPage(unittest.TestCase):
    """the user: after the Mac's restart the user's phone's page said "Set the passcode on the Mac" with nowhere to
    type: its auth check had met Tailscale's 502 while the app was down, and a failed check read as "no passcode
    set"."""

    def test_a_failed_check_is_cant_reach_never_set_a_passcode(self):
        js = open(os.path.join(ROOT, "mac", "ui", "studio.js")).read()
        self.assertIn('S.auth = r.ok ? await r.json() : null', js)
        lock = js[js.index("function showLock()"):]
        self.assertLess(lock.index("if (!S.auth)"), lock.index("Set the passcode on the Mac"))
        self.assertIn("Can't reach StrixFold UI", lock[:lock.index("const a = S.auth;")])
        self.assertIn("location.reload()", lock[:lock.index("const a = S.auth;")])  # back by itself once the app answers


class TailnetName(unittest.TestCase):
    """Started by launchd the server has no TERM, and the Tailscale app's CLI without it fails, so the tailnet listener
    knew no name and refused the user's phone ("host not allowed"). The CLI always gets a TERM."""

    def test_the_cli_gets_a_term(self):
        import subprocess
        seen = []

        def run(args, **kw):
            seen.append(kw.get("env") or {})
            out = b'{"Self": {"DNSName": "mac.tail0.ts.net."}}' if args[1:] == ["status", "--json"] else b'{"x": "127.0.0.1:8713"}'
            return subprocess.CompletedProcess(args, 0, out, b"")
        old_run, old_term = HC.subprocess.run, os.environ.pop("TERM", None)
        HC.subprocess.run = run
        try:
            self.assertEqual(HC.tailnet_name({"tailscale": "/x/Tailscale", "studio_tailnet_port": 8713}, fresh=True), ("mac.tail0.ts.net", True))
        finally:
            HC.subprocess.run = old_run
            if old_term is not None:
                os.environ["TERM"] = old_term
        self.assertEqual([e.get("TERM") for e in seen], ["dumb", "dumb"])
        self.assertEqual(seen[0].get("HOME"), os.environ.get("HOME"))  # the rest of the environment as it was


if __name__ == "__main__":
    unittest.main()


class FakeLeftovers(unittest.TestCase):
    """A fake app stopped hard leaves its halod and stand-in model holding its ports; the next start on the
    same fake root ends them, and nothing else (a process naming a root beside it, or no fake command, is left)."""

    def test_a_wide_fake_root_ends_nothing(self):
        """A fake root of /, ~, a parent of ~ or a short path like /tmp could name other agents'
        fakes, so the start ends nothing then."""
        import subprocess
        import time
        base = tempfile.mkdtemp(prefix="hc-fake-")
        stand_in = os.path.join(HC.REPO, "box", "fake", "stand-in.py")
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", stand_in, "rank", "0", os.path.join(base, "fake", "box1")])
        try:
            time.sleep(0.3)
            for wide in ("/", os.path.expanduser("~"), os.path.dirname(os.path.expanduser("~")), "/tmp", "/private/tmp"):
                app = HC.App.__new__(HC.App)
                app.fake_root = wide
                app.fake_leftovers()
            time.sleep(0.5)
            self.assertIsNone(other.poll())
        finally:
            other.kill()
            other.wait()

    def test_a_start_ends_only_its_own_roots_leftovers(self):
        import subprocess
        import time
        base = tempfile.mkdtemp(prefix="hc-fake-")
        root, beside = os.path.join(base, "fake"), os.path.join(base, "fake2")
        stand_in = os.path.join(HC.REPO, "box", "fake", "stand-in.py")  # a command line like a fake rank's
        sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
        ours = subprocess.Popen(sleep + [stand_in, "rank", "0", os.path.join(root, "box1", "runs", "r1")])
        theirs = subprocess.Popen(sleep + [stand_in, "rank", "0", os.path.join(beside, "box1", "runs", "r1")])
        plain = subprocess.Popen(sleep + [os.path.join(root, "notes")])  # names the root, but isn't one of the fake's
        procs = (ours, theirs, plain)
        try:
            time.sleep(0.3)
            app = HC.App.__new__(HC.App)
            app.fake_root = root
            app.fake_leftovers()
            ours.wait(timeout=6)
            self.assertIsNotNone(ours.returncode)
            self.assertEqual((theirs.poll(), plain.poll()), (None, None))
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                p.wait()
