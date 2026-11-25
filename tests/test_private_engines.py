"""Where a Private chat's words may go (mac/private_engines.py): the five engine facts, each
failing on its own on fake box answers; the private ssh taking nothing from ~/.ssh (no config, the host key pinned and
handed over by KnownHostsCommand); the listener on the private port must be the app's own tunnel; the send route
refusing in words. Nothing reaches a box: the facts and lsof are stand-ins."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import private_engines as E  # noqa: E402
import private_store as P  # noqa: E402
import site_fixture  # noqa: E402


def good(box):
    """A box where every fact holds, with DeepSeek's rank on it."""
    procs = [{"pid": 11, "port": "18080" if box == "box1" else "", "comm": "tensorfold-nati" if box == "box1" else "tf-dsv41-lanes",
              "core_soft": "0", "args": "/home/u/tfp/deploy/x/native-hip/bin/tensorfold-native serve /models/m --port 18080"},
             {"pid": 12, "port": "18090", "comm": "python3", "core_soft": "0", "args": "python -u tools/qwen/serve.py --model /p --port 18090"}]
    return {"host": box, "ptrace_scope": "1", "core_pattern": "|/usr/lib/systemd/systemd-coredump", "procs": procs,
            "swaps": [{"dev": "/dev/zram0", "type": "disk"}]}


class Facts(unittest.TestCase):
    def setUp(self):
        site_fixture.use_private(self)

    def test_all_five_hold(self):
        by = {"box1": good("box1"), "box2": good("box2")}
        for engine in ("dsv41", "dsv41-unc", "qwen38-kyojin", "qwen38"):
            self.assertEqual(E.problems(engine, by), [], engine)

    def test_each_fact_fails_on_its_own(self):
        cases = [
            (lambda f: f.update(ptrace_scope="0"), "lets any process read another's memory (ptrace_scope 0)"),
            (lambda f: f.update(swaps=[{"dev": "/dev/nvme0n1p3", "type": "part"}]), "swaps to /dev/nvme0n1p3, which isn't encrypted"),
            (lambda f: f["procs"][0].update(core_soft="unlimited"), "keeps core dumps of tensorfold-nati (its core limit is unlimited)"),
            (lambda f: f["procs"][0].update(args=f["procs"][0]["args"] + " --slot-save-path /tmp/s"), "writes to disk (--slot-save-path)"),
            (lambda f: f["procs"][0].update(args=f["procs"][0]["args"] + " --log-prompts"), "writes to disk (--log-prompts)"),
            (lambda f: f.update(procs=[]), "has no DeepSeek V4.1 running"),
        ]
        for change, words in cases:
            f1 = good("box1")
            change(f1)
            got = E.problems("dsv41", {"box1": f1, "box2": good("box2")})
            self.assertEqual(len(got), 1, (words, got))
            self.assertIn(words, got[0])
        self.assertEqual(E.problems("dsv41", {"box1": good("box1"), "box2": None}), ["box2 couldn't be checked"])
        pipe = good("box1")
        pipe["core_pattern"] = "|/usr/share/apport/apport -p%p -s%s"  # a pipe ignores the core limit
        self.assertEqual(E.problems("dsv41", {"box1": pipe, "box2": good("box2")}), ["box1 sends core dumps to /usr/share/apport/apport"])
        plain = good("box1")
        plain["core_pattern"] = "core"  # a file: the core limit of 0 holds
        self.assertEqual(E.problems("dsv41", {"box1": plain, "box2": good("box2")}), [])
        enc = good("box1")
        enc["swaps"] = [{"dev": "/dev/dm-1", "type": "crypt"}]
        self.assertEqual(E.problems("dsv41", {"box1": enc, "box2": good("box2")}), [])

    def test_the_rank_on_the_second_box_counts(self):
        h2 = good("box2")
        h2["procs"][0]["core_soft"] = "unlimited"  # box2's tf-dsv41-lanes holds the prompt too (tensor-parallel)
        self.assertIn("box2 keeps core dumps of tf-dsv41-lanes", E.problems("dsv41", {"box1": good("box1"), "box2": h2})[0])
        self.assertEqual(E.problems("qwen38-kyojin", {"box1": good("box1"), "box2": h2}), [])  # Kyojin is box1's here

    def gate(self, found=None, probe=None, read=good):
        """A gate whose request, box search and tunnel are stand-ins (nothing reaches a box)."""
        calls = {"probe": [], "found": []}

        def pr(ep, served):
            calls["probe"].append((ep, served))
            return probe(ep, served) if probe else "hc-canary-" + "0" * 24

        def fd(box, marker):
            calls["found"].append((box, marker))
            return found(box) if found else []
        g = E.Gate(read=read, probe=pr, found=fd)
        g.endpoint = lambda engine: "http://127.0.0.1:18580/v1"
        return g, calls

    def test_the_gate_keeps_answers_per_run(self):
        reads = []
        g, calls = self.gate(read=lambda box: reads.append(box) or good(box))
        self.assertEqual(g.check("dsv41", "run-1", "dsv41"), [])
        g.check("dsv41", "run-1", "dsv41")
        self.assertEqual(reads, ["box1", "box2"])  # kept for the run
        self.assertEqual(len(calls["probe"]), 1)
        g.check("dsv41", "run-2", "dsv41")
        self.assertEqual(len(reads), 4)  # a new run is checked again
        self.assertIn("Private chats run only on DeepSeek or Qwen 3.8", g.check("qwen38-gufo21", "r")[0])
        self.assertEqual(E.Gate(fake=True).check("dsv41", "r"), [])
        old = E.CANARY_PROBE
        E.CANARY_PROBE = False
        self.addCleanup(setattr, E, "CANARY_PROBE", old)
        self.assertEqual(E.Gate(read=good).check("dsv41", "r"), ["whether its engine logs prompts isn't checked yet"])

    def test_warm_checks_in_the_background_one_at_a_time(self):
        import threading
        import time as _t
        gate_open = threading.Event()
        reads = []

        def read(box):
            gate_open.wait(5)
            reads.append(box)
            return good(box)
        g, calls = self.gate(read=read)
        g.warm("dsv41", "r1", "dsv41")
        g.warm("dsv41", "r1", "dsv41")  # already warming: not twice
        gate_open.set()
        for _ in range(50):
            if not g.warming:
                break
            _t.sleep(0.05)
        self.assertEqual(reads, ["box1", "box2"])
        self.assertEqual(g.check("dsv41", "r1", "dsv41"), [])  # the turn finds it ready
        self.assertEqual(len(reads), 2)
        # a kept answer near its end: returned at once, and checked again behind it
        k = ("dsv41", "r1")
        g.cache[k] = (_t.time() - (E.FACTS_TTL_S - 60), [])
        self.assertEqual(g.check("dsv41", "r1", "dsv41"), [])
        for _ in range(50):
            if len(reads) == 4 and not g.warming:
                break
            _t.sleep(0.05)
        self.assertEqual(len(reads), 4)
        self.assertGreater(g.cache[k][0], _t.time() - 30)
        E.Gate(fake=True).warm("dsv41", "r")  # the fake app: nothing

    def test_the_canary_runs_last_on_every_box_of_the_engine(self):
        g, calls = self.gate()
        self.assertEqual(g.check("dsv41", "r", "dsv41"), [])
        self.assertEqual(calls["probe"], [("http://127.0.0.1:18580/v1", "dsv41")])
        self.assertEqual([b for b, _ in calls["found"]], ["box1", "box2"])  # tensor-parallel: both boxes saw it
        g, calls = self.gate(found=lambda box: ["box2 keeps prompts in files (1, e.g. /home/u/x.log)"] if box == "box2" else [])
        self.assertEqual(g.check("dsv41", "r", "dsv41"), ["box2 keeps prompts in files (1, e.g. /home/u/x.log)"])
        g, calls = self.gate(read=lambda box: dict(good(box), ptrace_scope="0"))
        g.check("dsv41", "r")
        self.assertEqual(calls["probe"], [])  # an engine another fact refuses gets no request at all
        g, calls = self.gate(probe=lambda ep, served: (_ for _ in ()).throw(OSError("refused")))
        self.assertEqual(g.check("dsv41", "r"), ["whether its engine logs prompts isn't checked (OSError)"])
        self.assertEqual(calls["found"], [])

    def test_the_box_search_reads_names_and_a_count_only(self):
        marker = "hc-canary-" + "ab" * 12
        seen = []

        def run(argv, input=None, **k):
            seen.append((argv, input))
            return types.SimpleNamespace(returncode=answer["code"], stdout=answer["out"].encode(), stderr=b"")
        answer = {"code": 0, "out": "journal 0\n"}
        self.assertEqual(E.canary_found("box1", marker, run), [])
        argv, script = seen[0]
        self.assertNotIn(marker, " ".join(argv))  # never on the ssh command line
        lines = script.decode().splitlines()
        self.assertEqual([x for x in lines if marker in x], [f"export m='{marker}'"])  # only in the environment: grep reads -f <(...)
        for want in (b'-path "$HOME/models" -prune', b"-mmin -3", b"tail -c 67108864", b"-gt 209715200", b"grep -qF -f <(pat)",
                     b"journalctl --user --since -3min", b"grep -cF -f <(pat)", b"timeout 20 bash -c", b"/tmp /var/tmp"):
            self.assertIn(want, script)
        self.assertNotIn(b"sudo", script)
        self.assertNotIn(b"-size", script)  # a big file is searched by its last 64 MB, not skipped
        answer.update(out="file /home/user1/tfp/run.log\nfile /tmp/x\njournal 0\n")
        self.assertEqual(E.canary_found("box1", marker, run), ["box1 keeps prompts in files (2, e.g. /home/user1/tfp/run.log)"])
        answer.update(out="journal 3\n")
        self.assertEqual(E.canary_found("box1", marker, run), ["box1 keeps prompts in its journal (3 lines)"])
        answer.update(code=124, out="")
        self.assertIn("isn't checked (the search took over 20 s)", E.canary_found("box1", marker, run)[0])
        answer.update(code=0, out="")
        self.assertIn("isn't checked (no answer)", E.canary_found("box1", marker, run)[0])
        with self.assertRaises(ValueError):
            E.canary_found("box1", "hc-canary-'; rm -rf ~; '", run)


class Tunnel(unittest.TestCase):
    def setUp(self):
        site_fixture.use_private(self)

    def test_the_private_ssh_takes_nothing_from_dot_ssh(self):
        a = E.ssh_argv("box1", "-N")
        s = " ".join(a)
        ip, user = E.BOXES["box1"]
        self.assertEqual(a[0], "/usr/bin/ssh")
        for want in ("-F /dev/null", "StrictHostKeyChecking=yes", "UserKnownHostsFile=/dev/null", "GlobalKnownHostsFile=/dev/null",
                     f"KnownHostsCommand=/bin/echo {ip} {E.HOSTKEY}", "HostKeyAlgorithms=ssh-ed25519", "IdentitiesOnly=yes",
                     "ProxyCommand=none", "ForwardAgent=no", f"-l {user}", f"-i {E.KEY}"):
            self.assertIn(want, s)
        self.assertEqual(a[-1], ip)
        self.assertIn("ProxyCommand=/usr/bin/nc -X 5 -x localhost:1055 %h %p", " ".join(E.ssh_argv("box2", direct=False)))
        self.assertTrue(E.HOSTKEY.startswith("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5"))
        # the pinned key is what ssh is handed: KnownHostsCommand's output is a known_hosts line for the address
        out = subprocess.run(["/bin/echo", ip, *E.HOSTKEY.split()], capture_output=True, text=True).stdout.split()
        self.assertEqual(out, [ip, "ssh-ed25519", E.HOSTKEY.split()[1]])

    def test_no_pinned_box_no_private_turn(self):
        """A copy whose checked code pins no boxes (the open source until its user does) refuses in words, never by
        falling back to ~/.ssh."""
        E.BOXES, E.HOSTKEY = {}, None
        with self.assertRaises(RuntimeError) as cm:
            E.ssh_argv("box1", "-N")
        self.assertIn("no pinned address and host key", str(cm.exception))
        with self.assertRaises(RuntimeError):
            E.facts("box1", run=lambda *a, **k: self.fail("ssh ran without a pinned box"))
        self.assertNotIn(18080, {e["local"] for e in E.ENGINES.values()})  # never the ordinary tunnel's port

    def test_the_listener_must_be_the_apps_own_tunnel(self):
        seen = {"pids": "p4242\n"}
        g = E.Gate(run=lambda argv, **k: types.SimpleNamespace(stdout=seen["pids"].encode()))
        self.assertEqual(g.listener(18580), {4242})
        g.tunnels[18580] = types.SimpleNamespace(pid=4242, poll=lambda: None, terminate=lambda: None)
        self.assertEqual(g.endpoint("dsv41"), "http://127.0.0.1:18580/v1")
        seen["pids"] = "p9999\n"  # something else holds the port
        with self.assertRaisesRegex(RuntimeError, "isn't the app's own tunnel"):
            g.endpoint("dsv41")
        seen["pids"] = "p4242\np9999\n"  # two listeners
        with self.assertRaises(RuntimeError):
            g.endpoint("dsv41")
        argv = []
        g2 = E.Gate(run=lambda a, **k: argv.append(a) or types.SimpleNamespace(stdout=b""))
        g2.listener(18580)
        self.assertEqual(argv[0][0], "/usr/sbin/lsof")
        self.assertIsNone(E.Gate(fake=True).endpoint("dsv41"))

    def test_engines_on_one_box_port_share_its_tunnel(self):
        # DeepSeek V4.1's tunnel held 18580, and uncensored's own ssh found it taken
        started = []

        def popen(argv, **k):
            started.append(argv)
            return types.SimpleNamespace(pid=4242, poll=lambda: None, kill=lambda: None, terminate=lambda: None)
        g = E.Gate(run=lambda argv, **k: types.SimpleNamespace(stdout=b"p4242\n"))
        with mock.patch.object(E.subprocess, "Popen", popen):
            self.assertEqual(g.endpoint("dsv41"), "http://127.0.0.1:18580/v1")
            self.assertEqual(g.endpoint("dsv41-unc"), "http://127.0.0.1:18580/v1")
            self.assertEqual(len(started), 1)
            self.assertEqual(g.endpoint("qwen38-kyojin"), "http://127.0.0.1:18590/v1")
            self.assertEqual(g.endpoint("qwen38"), "http://127.0.0.1:18590/v1")
            self.assertEqual(len(started), 2)
        self.assertEqual(set(g.tunnels), {18580, 18590})


class SendRoute(unittest.TestCase):
    """A Private chat's send is refused in words when its engine can't take it."""

    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("hc_test_studio_e", os.path.join(ROOT, "tests", "test_studio.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.ts = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ts)
        site_fixture.use_private(self)  # the fixture's box1 + box2, whatever this setup pins
        self.ts.Http.setUp(self)
        self.ts.Http.unlock(self)
        self.st = self.app.studio["store"]
        self.app.code_check = types.SimpleNamespace(view=lambda: {"clean": True, "problems": [], "line": "x"})

    def req(self, *a, **k):
        return self.ts.Http.req(self, *a, **k)

    def test_refused_in_words(self):
        tok = json.loads(self.req("POST", "/studio/api/private/unlock", {"passcode": "correct horse"})[1])["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "model": "quick-chat"},
                                  X_Halo_Private=tok)[1])["id"]
        hs = {"state": "ready", "profile": {"id": "quick-chat", "model_id": "dsv41", "settings": {"context": 16384}},
              "served_model": "dsv41", "run_id": "r9", "endpoint": "http://127.0.0.1:9/v1"}
        self.app.halod_state = lambda fresh=False: hs
        self.app.model_check = lambda s=None: None
        self.app.studio["outbox"].why_not = lambda st, mid: None
        self.app.studio["private_gate"] = E.Gate(read=lambda box: dict(good(box), ptrace_scope="0"))
        code, body, _ = self.req("POST", f"/studio/api/chats/{cid}/send", {"content": "hello", "mode": "send"}, X_Halo_Private=tok)
        self.assertEqual(code, 409, body)
        d = json.loads(body)
        self.assertEqual(d["why"], "private_engine")
        self.assertTrue(d["error"].startswith("Private chats can't run on DeepSeek V4.1 right now: box1 lets any process read"), d)
        with P.granted():
            self.assertEqual(self.st.messages(cid), [])  # nothing stored, nothing sent



class ExtraSend(unittest.TestCase):
    """A chat whose model the extra pair serves streams from the pair at once (App.chat_state), though the
    main boxes serve another model; a Private chat on the same model stays on the main boxes (and waits for them)."""

    def setUp(self):
        site_fixture.use_pairs(self)
        SendRoute.setUp(self)
        loader = importlib.machinery.SourceFileLoader("hc_test_chat_b", os.path.join(ROOT, "tests", "test_chat.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        tc = importlib.util.module_from_spec(spec)
        loader.exec_module(tc)
        self.sse = tc.SSE(n=3)
        self.addCleanup(self.sse.close) if hasattr(self.sse, "close") else None
        halos = {"state": "ready", "profile": {"id": "qwen38-halogen-64k", "model_id": "qwen38-halogen", "settings": {"context": 65536}},
                 "served_model": "Qwen3.8", "run_id": "h1", "endpoint": "http://127.0.0.1:18090/v1"}
        self.bos = {"state": "ready", "profile": {"id": "unc-chat", "model_id": "dsv41-unc", "settings": {"context": 63488}},
                    "served_model": "dsv41", "run_id": "b1", "endpoint": "http://127.0.0.1:18080/v1", "pair": "box-b"}
        self.app.halod_state = lambda fresh=False: halos
        self.app.pair_state = lambda name, fresh=False: self.bos if name == "box-b" else None
        self.app.model_check = lambda st=None, model_id=None, endpoint=None: None
        self.app.local_endpoint = lambda st=None: self.sse.url if (st or {}).get("pair") == "box-b" else "http://127.0.0.1:9/v1"

    def req(self, *a, **k):
        return self.ts.Http.req(self, *a, **k)

    def test_goes_to_the_bosgames(self):
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "unc-chat"})[1])["id"]
        code, body, _ = self.req("POST", f"/studio/api/chats/{cid}/send", {"content": "hello", "mode": "send", "no_tools": True})
        self.assertEqual(code, 200, body)
        self.assertEqual(len(self.sse.bodies), 1)  # streamed from the pair's endpoint, not queued for a Switch
        tok = json.loads(self.req("POST", "/studio/api/private/unlock", {"passcode": "correct horse"})[1])["token"]
        pid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "model": "unc-chat"},
                                  X_Halo_Private=tok)[1])["id"]
        code, body, _ = self.req("POST", f"/studio/api/chats/{pid}/send", {"content": "hello", "mode": "send"}, X_Halo_Private=tok)
        self.assertEqual(code, 202, body)  # the main boxes serve Qwen: a Private chat's message waits for its Switch there
        self.assertEqual(len(self.sse.bodies), 1)

if __name__ == "__main__":
    unittest.main()
