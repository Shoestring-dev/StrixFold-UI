"""halod's state machine against the fake rank scripts (box/fake): start to ready and stop, lock contention, a rank
dying, the restart limit, re-adoption after a halod restart, the idle stop, a stall, single-flight commands, the HTTP
API's token and Host checks. Each test builds its own fake two-box world in a temporary directory."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "box", "fake"))
import fake_env  # noqa: E402
import halod  # noqa: E402
import profiles  # noqa: E402

FAKE = os.path.join(ROOT, "box", "fake", "fake_serve.py")


_given = set()


def free_port(span=1):
    """A free port (and the span-1 after it, for a fake world's derived ports), picked at random in 20000-39999: below
    the ephemeral range other processes' outgoing connections draw from, so another program on the Mac rarely takes it
    between the pick and the bind; never one this test run handed out already."""
    import random
    for _ in range(500):
        base = random.randrange(20000, 40000 - span)
        ports = range(base, base + span)
        if any(p in _given for p in ports):
            continue
        socks = []
        try:
            for p in ports:
                s = socket.socket()
                s.bind(("127.0.0.1", p))
                socks.append(s)
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
        _given.update(ports)
        return base
    raise RuntimeError("no free port range")


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


ROOTS = []  # every fake world this run made: tearDownModule kills whatever is still running in any of them


def kill_worlds():
    for r in ROOTS:
        subprocess.run(["pkill", "-f", r], capture_output=True)


class World(unittest.TestCase):
    load_s, speed = 0.6, 4.0
    overrides = {}

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hc-test-")
        ROOTS.append(self.root)
        self.port = free_port(16)  # the fake world's ports derive from it (+1, +2, +10 to +12)
        self.cfg = fake_env.build(self.root, load_s=self.load_s, speed=self.speed, endpoint_port=self.port, tick_s=0.3,
                                  lanes=getattr(self, "lanes", 1), takes_lock=getattr(self, "takes_lock", False))
        self.cfg.update({"lock_try_s": 1, "lock_pair_s": 1, "lock_backoff_s": [0.3], "health_fail_s": 3,
                         "stall_s": 600, "probe_after_idle_s": 3600, "minute_s": 60, "sweep_after_s": 2,
                         "timeouts": {"waiting": 120, "loading": 60, "linking": 60, "verify": 60, "stopping": 30}})
        self.cfg.update(self.overrides)
        self.shared = self.cfg["shared"]
        self.outsiders = []
        self.H = self.make()
        # a cleanup, not tearDown: it runs even when a subclass's setUp fails after this (EndToEnd's port taken, say),
        # which left the world's halod starting ranks and lock holders that outlived the run
        self.addCleanup(self.end_world)

    def make(self):
        H = halod.Halod(halod.load_config(self.cfg))
        H.worker.start()
        H.monitor.start()
        return H

    def end_world(self):
        try:
            if self.H.view_state()["state"] not in ("idle",):
                self.H.stop("test teardown")
                self.wait_state(("idle", "failed"), 30)
        finally:
            self.H.close()
            for p in self.outsiders:
                if p.poll() is None:
                    p.kill()
            # nothing of ours may be left: no rank, no lock holder, no folder
            subprocess.run(["pkill", "-f", self.root], capture_output=True)
            shutil.rmtree(self.root, ignore_errors=True)

    def ctl(self, name, on=True):
        p = os.path.join(self.shared, name)
        if on:
            open(p, "w").close()
        elif os.path.exists(p):
            os.unlink(p)

    def wait_state(self, states, timeout=30, H=None):
        H = H or self.H
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = H.view_state()
            if st["state"] in states:
                return st
            time.sleep(0.05)
        st = H.view_state()
        logs = ""
        if st.get("run_id"):
            for box, r in (("box1", 0), ("box2", 1)):
                p = os.path.join(self.run_files(box, st["run_id"]), f"rank{r}.log")
                if os.path.exists(p):
                    logs += f"\n--- {box} rank{r}.log\n" + open(p).read()[-600:]
        self.fail(f"state {st['state']} ({st['reason']}) after {timeout}s, wanted {states}{logs}")

    def chat(self, text, max_tokens=20):
        body = json.dumps({"model": "dsv41", "messages": [{"role": "user", "content": text}], "max_tokens": max_tokens}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", body, {"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=30))

    def hold_outside(self, box, label="other-team-run"):
        """Another team member's run holding a box's lock (the fake holder, outside halod)."""
        rd = os.path.join(self.root, box, "elsewhere", label)
        os.makedirs(rd, exist_ok=True)
        lock = os.path.join(self.root, f"{box}.lock")
        p = subprocess.Popen([sys.executable, FAKE, "holder", lock, os.path.join(rd, "lock.pid")])
        self.outsiders.append(p)
        for _ in range(100):
            if os.path.exists(os.path.join(rd, "lock.pid")):
                return p
            time.sleep(0.02)
        self.fail("outside holder did not get the lock")

    def run_files(self, box, rid):
        return os.path.join(self.root, box, "halo-control", "runs", rid)

    def leftovers(self, rid):
        out = []
        for box, rank in (("box1", 0), ("box2", 1)):
            for f in (f"rank{rank}.pid", "lock.pid"):
                p = os.path.join(self.run_files(box, rid), f)
                if os.path.exists(p):
                    pid = int(open(p).read())
                    if alive(pid):
                        out.append(f"{box}/{f}={pid}")
        return out


class Lifecycle(World):
    def test_start_ready_request_stop(self):
        self.H.start("agent-coding")
        st = self.wait_state(("ready", "failed"))
        self.assertEqual(st["state"], "ready", st["reason"])
        for k in ("waiting", "loading", "linking", "ready"):
            self.assertIn(k, st["steps"])
        rid = st["run_id"]
        conf = open(os.path.join(self.run_files("box1", rid), "serve.conf")).read()
        self.assertEqual(conf, open(os.path.join(self.run_files("box2", rid), "serve.conf")).read())
        self.assertIn("CONTEXT=63488", conf)
        self.chat("hello " * 400, 30)
        time.sleep(1.0)
        reqs = self.H.view_requests(10)["requests"]
        self.assertEqual(len(reqs), 1, reqs)  # halod's own first request is not listed
        r = reqs[0]
        self.assertGreater(r["prompt"], 500)
        self.assertEqual(r["tokens"], 30)
        for k in ("ttft_s", "prefill_s", "decode_s", "decode_tps", "prefill_tps", "acceptance"):
            self.assertIsNotNone(r[k], k)
        self.assertEqual(r["approx"], 0)
        self.H.stop()
        st = self.wait_state(("idle", "failed"))
        self.assertEqual(st["state"], "idle", st["reason"])
        self.assertEqual(self.leftovers(rid), [])
        kinds = [(e["state_from"], e["state_to"]) for e in self.H.view_events(200)["events"] if e["kind"] == "state"][::-1]
        self.assertEqual(kinds, [("idle", "waiting"), ("waiting", "loading"), ("loading", "linking"), ("linking", "ready"),
                                 ("ready", "stopping"), ("stopping", "idle")])

    def test_single_flight_and_idempotent(self):
        self.H.start("quick-chat")
        self.assertIn("already", self.H.start("quick-chat").get("note", ""))
        with self.assertRaises(halod.profiles.Invalid):
            self.H.start("agent-coding")  # another profile without replace
        self.wait_state(("ready",))
        self.assertIn("already", self.H.start("quick-chat").get("note", ""))
        runs = self.H.db.q("SELECT run_id FROM runs")
        self.assertEqual(len(runs), 1)
        self.H.stop()
        self.H.stop()
        self.wait_state(("idle",))
        self.assertIn("already", self.H.stop().get("note", ""))

    def test_switch_profile(self):
        self.H.start("quick-chat")
        st = self.wait_state(("ready",))
        first = st["run_id"]
        self.H.start("agent-coding", replace=True)
        t0 = time.time()
        while time.time() - t0 < 30:
            st = self.H.view_state()
            if st["state"] == "ready" and st["run_id"] != first:
                break
            time.sleep(0.05)
        self.assertEqual(st["profile"]["id"], "agent-coding")
        self.assertEqual(self.leftovers(first), [])

    def test_not_installed_refused(self):
        self.H.cfg["external"]["qwen38"]["check"] = "false"
        self.assertFalse(self.H.check_installs()["qwen38"]["ok"])
        with self.assertRaises(halod.profiles.Invalid):
            self.H.start("qwen38-flash-next")


class Locks(World):
    def test_waits_for_box1_then_proceeds(self):
        other = self.hold_outside("box1")
        self.H.start("quick-chat")
        st = self.wait_state(("waiting",))
        time.sleep(2.5)
        st = self.H.view_state()
        self.assertEqual(st["state"], "waiting")
        locks = self.H.view_locks()
        holders = locks["box1"]["lock"]["holders"]
        self.assertEqual([h["who"]["label"] for h in holders], ["other-team-run"])
        self.assertFalse(holders[0]["mine"])
        # all or nothing: box2's lock is not held by us while box1's is someone else's
        self.assertEqual(locks["box2"]["lock"]["holders"], [])
        other.terminate()
        st = self.wait_state(("ready", "failed"))
        self.assertEqual(st["state"], "ready", st["reason"])

    def test_box2_busy_releases_box1(self):
        other = self.hold_outside("box2")
        self.H.start("quick-chat")
        self.wait_state(("waiting",))
        # between attempts box1's lock is given back (never held while waiting on box2 past the pair window)
        seen_free = False
        t0 = time.time()
        while time.time() - t0 < 6:
            if not self.H.view_locks()["box1"]["lock"]["holders"]:
                seen_free = True
                break
            time.sleep(0.1)
        self.assertTrue(seen_free)
        other.terminate()
        st = self.wait_state(("ready", "failed"))
        self.assertEqual(st["state"], "ready", st["reason"])

    def test_queue_wait_cancel_leaves_no_waiter(self):
        self.H.close()
        self.cfg["lock_try_s"] = 60  # wait in the queue like the team's scripts
        self.H = self.make()
        other = self.hold_outside("box1")
        self.H.start("quick-chat")
        st = self.wait_state(("waiting",))
        rid = st["run_id"]
        time.sleep(1.5)
        self.H.stop()
        self.assertEqual(self.wait_state(("idle", "failed"))["state"], "idle")
        other.terminate()
        time.sleep(2.0)  # a waiter left behind would take the free lock now
        self.assertEqual(self.H.view_locks()["box1"]["lock"]["holders"], [])
        self.assertEqual(self.leftovers(rid), [])

    def test_cancel_while_waiting_leaves_nothing(self):
        other = self.hold_outside("box1")
        self.H.start("quick-chat")
        st = self.wait_state(("waiting",))
        rid = st["run_id"]
        time.sleep(1.0)
        self.H.stop()
        st = self.wait_state(("idle", "failed"))
        self.assertEqual(st["state"], "idle", st["reason"])
        self.assertEqual(self.leftovers(rid), [])
        self.assertTrue(other.poll() is None)  # never steals or breaks another holder's lock


    def test_sweeper_releases_only_our_stale_holder(self):
        # a holder left by one of halod's own earlier runs (e.g. a lock attempt cut short by a dropped ssh)
        rid = "hc-20250101-000000-old-run"
        rd = self.run_files("box1", rid)
        os.makedirs(rd)
        open(os.path.join(rd, "serve.conf"), "w").write(open(os.path.join(self.root, "box1", "tfp", "deploy", "current", "tools", "dsv41-hip", "serve.conf")).read()
                                                       .replace("RUN_ROOT='$HOME/tfp/run/serve'", "RUN_ROOT='$HOME/halo-control/runs'"))
        self.H.db.x("INSERT INTO runs (run_id, profile, deploy, started) VALUES (?,?,?,?)", (rid, "quick-chat", fake_env.COMMIT, time.time() - 3600))
        lock = os.path.join(self.root, "box1.lock")
        stale = subprocess.Popen([sys.executable, FAKE, "holder", lock, os.path.join(rd, "lock.pid")])
        self.outsiders.append(stale)
        other = self.hold_outside("box2")  # someone else's: must stay
        t0 = time.time()
        while stale.poll() is None and time.time() - t0 < 60:
            time.sleep(0.2)
        self.assertIsNotNone(stale.poll(), "the stale holder of our old run was not released")
        self.assertIsNone(other.poll())
        ev = []
        for _ in range(50):  # the sweep's event is written once its unlock command returns, after the holder died
            ev = [e["detail"] for e in self.H.view_events(100)["events"] if e["kind"] == "sweep"]
            if ev:
                break
            time.sleep(0.1)
        self.assertTrue(ev and rid in ev[0], ev)


class Failures(World):
    def test_rank_dies_then_restarts(self):
        self.H.start("quick-chat")
        st = self.wait_state(("ready",))
        first = st["run_id"]
        self.ctl("die-rank1")
        t0 = time.time()
        while self.H.view_state()["state"] == "ready" and time.time() - t0 < 20:
            time.sleep(0.05)
        self.ctl("die-rank1", False)
        st = self.wait_state(("ready", "failed"), 40)
        self.assertEqual(st["state"], "ready", st["reason"])
        self.assertNotEqual(st["run_id"], first)
        self.assertEqual(len(st["restarts"]), 1)
        self.assertEqual(self.leftovers(first), [])
        ev = [e["detail"] for e in self.H.view_events(200)["events"] if e["kind"] == "restart"]
        self.assertTrue(ev and "rank 1" in ev[0], ev)

    def test_restart_limit(self):
        self.ctl("die-rank1")  # rank 1 dies on every start
        self.H.start("quick-chat")
        st = self.wait_state(("failed",), 90)
        self.assertIn("3 restarts", st["reason"])
        self.assertEqual(len(st["restarts"]), 3)
        runs = [r["run_id"] for r in self.H.db.q("SELECT run_id FROM runs")]
        self.assertEqual(len(runs), 4)
        for rid in runs:
            self.assertEqual(self.leftovers(rid), [], rid)
        # a start by hand gets a fresh budget
        self.ctl("die-rank1", False)
        self.H.start("quick-chat")
        self.wait_state(("waiting", "loading", "linking"), 10)
        st = self.wait_state(("ready", "failed"), 30)
        self.assertEqual(st["state"], "ready", st["reason"])

    def test_stall_detected(self):
        self.H.start("quick-chat")
        st = self.wait_state(("ready",))
        first = st["run_id"]
        self.ctl("stall")
        t0 = time.time()
        while self.H.view_state()["run_id"] == first and time.time() - t0 < 30:
            time.sleep(0.1)
        self.ctl("stall", False)
        ev = [e["detail"] for e in self.H.view_events(200)["events"] if e["kind"] == "restart"]
        self.assertTrue(ev and "stall" in ev[-1], ev)
        self.assertEqual(self.wait_state(("ready", "failed"), 40)["state"], "ready")

    def test_load_failure(self):
        self.ctl("fail-load")
        self.H.start("quick-chat")
        st = self.wait_state(("failed",), 90)
        self.assertIn("rank 0 exited", st["reason"])


class Adoption(World):
    def test_readopt_after_restart(self):
        self.H.start("quick-chat")
        st = self.wait_state(("ready",))
        rid = st["run_id"]
        pids = [int(open(os.path.join(self.run_files(b, rid), f"rank{r}.pid")).read()) for b, r in (("box1", 0), ("box2", 1))]
        self.H.close()  # halod goes away; the ranks keep running
        time.sleep(0.5)
        self.assertTrue(all(alive(p) for p in pids))
        self.H = self.make()
        t0 = time.time()
        while time.time() - t0 < 40 and "re-adopted" not in (self.H.view_state()["reason"] or ""):
            time.sleep(0.1)
        st = self.wait_state(("ready", "failed"), 40)
        self.assertEqual(st["state"], "ready", st["reason"])
        self.assertEqual(st["run_id"], rid)
        self.assertEqual(pids, [int(open(os.path.join(self.run_files(b, rid), f"rank{r}.pid")).read()) for b, r in (("box1", 0), ("box2", 1))])
        self.assertIn("re-adopted", st["reason"] or "")
        self.chat("after restart " * 50)
        time.sleep(1.0)
        reqs = self.H.view_requests(10)["requests"]
        self.assertEqual(len(reqs), 1, json.dumps(reqs, indent=1))

    def test_partial_after_restart_cleans_up(self):
        self.H.start("quick-chat")
        st = self.wait_state(("ready",))
        rid = st["run_id"]
        self.H.close()
        self.ctl("die-rank1")
        time.sleep(1.0)
        self.ctl("die-rank1", False)
        self.H = self.make()
        st = self.wait_state(("failed", "idle"), 40)
        self.assertEqual(st["state"], "failed")
        self.assertIn("gone", st["reason"])
        self.assertEqual(self.leftovers(rid), [])


class Idle(World):
    overrides = {"minute_s": 0.4}

    def test_idle_stop_and_hold(self):
        self.H.start("quick-chat")  # idle_min 45 -> 18 s here
        self.wait_state(("ready",))
        self.H.hold(True)
        time.sleep(20)
        self.assertEqual(self.H.view_state()["state"], "ready")
        self.chat("keep me busy")
        time.sleep(1.0)
        self.H.hold(False)
        st = self.H.view_state()
        self.assertGreater(st["idle"]["stop_at"], time.time() + 15)
        st = self.wait_state(("idle",), 40)
        self.assertIn("idle for 45 minutes", st["reason"])


class Api(World):
    def test_token_and_host(self):
        srv = halod.serve(self.H, port=0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        st = json.load(urllib.request.urlopen(base + "/api/state"))
        self.assertEqual(st["state"], "idle")

        def post(path, body, token=None, host=None):
            h = {"Content-Type": "application/json"}
            if token:
                h["X-Halo-Token"] = token
            if host:
                h["Host"] = host
            req = urllib.request.Request(base + path, json.dumps(body).encode(), h, method="POST")
            try:
                return urllib.request.urlopen(req).status, None
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)
        self.assertEqual(post("/api/start", {"profile": "quick-chat"})[0], 403)
        self.assertEqual(post("/api/start", {"profile": "quick-chat"}, "wrong")[0], 403)
        self.assertEqual(post("/api/start", {"profile": "quick-chat"}, self.H.token, "evil.example:8710")[0], 403)
        req = urllib.request.Request(base + "/api/state", headers={"Host": "attacker.example"})
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(req)
        code, err = post("/api/profiles/validate", {"settings": {"context": 1000}}, self.H.token)
        self.assertEqual(code, 200)
        code, err = post("/api/profiles", {"name": "Mine", "base": "quick-chat", "settings": {"context": 100}}, self.H.token)
        self.assertEqual(code, 400)
        self.assertIn("context", err["errors"])
        self.assertEqual(post("/api/start", {"profile": "quick-chat"}, self.H.token)[0], 200)
        self.wait_state(("ready",))
        srv.shutdown()


class ModelsAndAdvanced(World):
    def api(self, method, path, body=None, token=True):
        if not hasattr(self, "srv"):
            self.srv = halod.serve(self.H, port=0)
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()
            self.addCleanup(self.srv.shutdown)
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Halo-Token"] = self.H.token
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_address[1]}{path}",
                                     None if body is None else json.dumps(body).encode(), h, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_uncensored_runs_from_its_own_deploy_and_bundle(self):
        # the official builds in the same root (current, fa4e...) know no EDIT: never offered for this model
        self.assertEqual(set(self.H.deploys("dsv41-unc")), {fake_env.UNC_COMMIT})
        self.assertIn(fake_env.UNC_COMMIT, self.H.deploys("dsv41"))
        self.assertTrue(self.H.check_installs()["dsv41-unc"]["ok"])
        self.H.start("unc-chat")
        st = self.wait_state(("ready", "failed"))
        self.assertEqual(st["state"], "ready", st["reason"])
        for box in ("box1", "box2"):
            v = halod.profiles.parse_conf(open(os.path.join(self.run_files(box, st["run_id"]), "serve.conf")).read())
            self.assertEqual(v["DEPLOY_DIR"], f"$HOME/tfp/deploy/{fake_env.UNC_COMMIT}")
            self.assertEqual(v["BUNDLE"], "$HOME/models/tf-port/inputs-tp2-c178a012")  # the official pack, edited
            self.assertEqual(v["BUNDLE_SHA256"], fake_env.UNC_SHA)
            self.assertEqual(v["EDIT"], "$HOME/models/tf-port/unc-edit-89af9bf1.safetensors")
            self.assertEqual(len(v["EDIT_SHA256"]), 64)
            self.assertEqual((v["CONTEXT"], v["DRAFTS"]), ("63488", "1"))  # drafts on since U52 (766ef21); 63,488 from the team's runs
        row = self.H.db.q("SELECT deploy_root, model_id FROM runs WHERE run_id=?", (st["run_id"],))[0]
        self.assertEqual(row, {"deploy_root": "$HOME/tfp/deploy", "model_id": "dsv41-unc"})
        rid = st["run_id"]
        self.H.stop()
        self.wait_state(("idle",))
        self.assertEqual(self.leftovers(rid), [])

    def test_uncensored_refused_without_its_verify_report(self):
        rep = glob_one(os.path.join(self.root, "box2", "models", "tf-port", "inputs-tp2-c178a012.verify-rank1-*.json"))
        os.unlink(rep)
        self.assertFalse(self.H.check_installs()["dsv41-unc"]["ok"])
        with self.assertRaises(halod.profiles.Invalid) as e:
            self.H.start("unc-agent")
        self.assertIn("verify report on box2", str(e.exception))
        prof = {p["id"]: p for p in self.H.view_profiles()["profiles"]}
        self.assertFalse(prof["unc-agent"]["installed"])
        self.assertTrue(prof["agent-coding"]["installed"])

    def test_advanced_and_streams_reach_the_run_conf(self):
        code, r = self.api("POST", "/api/profiles", {"name": "Two at once", "base": "quick-chat", "settings": {"streams": 2},
                                                      "advanced": {"ROUND_ROWS": "8", "TRACE_ROUNDS": "1"}})
        self.assertEqual(code, 200, r)
        pid = r["profile"]["id"]
        ev = [e["detail"] for e in self.H.view_events(20)["events"] if e["kind"] == "profile"][0]
        self.assertIn("streams 1 -> 2", ev)
        self.assertIn("ROUND_ROWS (conf default) -> 8", ev)
        self.H.start(pid)
        st = self.wait_state(("ready", "failed"))
        self.assertEqual(st["state"], "ready", st["reason"])
        v = halod.profiles.parse_conf(open(os.path.join(self.run_files("box1", st["run_id"]), "serve.conf")).read())
        self.assertEqual((v["STREAMS"], v["PARALLEL"], v["ROUND_ROWS"], v["TRACE_ROUNDS"]), ("2", "2", "8", "1"))
        code, r = self.api("PUT", f"/api/profiles/{pid}", {"advanced": {"ROUND_ROWS": "4"}})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["running"])
        ev = [e["detail"] for e in self.H.view_events(20)["events"] if e["kind"] == "profile"][0]
        self.assertIn("ROUND_ROWS 8 -> 4; TRACE_ROUNDS 1 -> (conf default) (applies at its next start)", ev)

    def test_advanced_refusals(self):
        code, r = self.api("POST", "/api/profiles", {"name": "Bad", "base": "agent-coding", "advanced": {"WRAP0": "/bin/sh"}})
        self.assertEqual(code, 400)
        self.assertIn("WRAP0", r["errors"])
        code, r = self.api("POST", "/api/profiles", {"name": "Bad", "base": "agent-coding", "advanced": {"COLL_KIND": "host"}})
        self.assertIn("Expert", r["errors"]["COLL_KIND"])
        code, r = self.api("POST", "/api/profiles/validate", {"base": "agent-coding", "settings": dict(halod.profiles.BUILTIN[0]["settings"]),
                                                               "advanced": {"COLL_KIND": "host"}, "expert": True})
        self.assertFalse(r["ok"])
        self.assertIn("adv:GRAPHS", r["errors"])
        # a profile edited by hand on the box with a value the checks refuse never starts
        self.H.store.custom.append({"id": "custom-hand", "name": "By hand", "model_id": "dsv41", "settings": dict(halod.profiles.BUILTIN[2]["settings"]),
                                    "advanced": {"SERVE_HOST": "0.0.0.0"}})
        self.H.start("custom-hand")
        st = self.wait_state(("failed",))
        self.assertIn("SERVE_HOST", st["reason"])
        self.assertEqual(self.H.view_locks()["box1"]["lock"]["holders"], [])

    def test_conf_view_export_import(self):
        code, r = self.api("GET", "/api/conf?profile=unc-agent", token=False)
        self.assertEqual(code, 200, r)
        keys = {k["key"]: k for k in r["knobs"]}
        self.assertIn("TOP_K_MAX", keys)  # only the uncensored deploy's conf has it
        self.assertTrue(keys["TOP_K_MAX"]["editable"])
        self.assertIn(f"deploy/{fake_env.UNC_COMMIT}/tools/dsv41-hip/serve.conf", r["path"])
        code, r = self.api("GET", "/api/conf?profile=agent-coding", token=False)
        self.assertNotIn("TOP_K_MAX", {k["key"] for k in r["knobs"]})
        self.api("POST", "/api/profiles", {"name": "Exp", "base": "agent-coding", "advanced": {"COLL_RTO_MIN_US": "9000"}, "expert": True})
        code, data = self.api("GET", "/api/profiles/export", token=False)
        self.assertEqual(len(data["profiles"]), 1)
        self.H.store.delete("custom-exp")
        code, r = self.api("POST", "/api/profiles/import", {"data": data})
        self.assertEqual((code, r["created"], r["errors"]), (200, ["custom-exp"], {}))
        self.assertEqual(self.H.store.get("custom-exp")["advanced"], {"COLL_RTO_MIN_US": "9000"})
        code, r = self.api("POST", "/api/profiles/import", {"data": {"halo_control_profiles": 1, "profiles": [
            {"name": "Sneaky", "model_id": "dsv41", "base": "agent-coding", "advanced": {"COLL_RTO_MIN_US": "9000"}}]}})
        self.assertIn("Expert", r["errors"]["Sneaky"])
        self.assertIn("imported 0 profile(s)", self.H.view_events(5)["events"][0]["detail"])


class External(World):
    """Qwen3.8 through halo-qwen (fake_qwen.py): its own start, stop and lock handling."""

    def qdir(self):
        return os.path.join(self.root, "qwen")

    def test_start_ready_stop(self):
        self.assertTrue(self.H.check_installs()["qwen38"]["ok"])
        self.H.start("qwen38-flash-next")
        st = self.wait_state(("ready", "failed"), 60)
        self.assertEqual(st["state"], "ready", st["reason"])
        port = self.H.cfg["external"]["qwen38"]["port"]
        self.assertEqual(st["endpoint"], f"http://127.0.0.1:{port}/v1")
        self.assertEqual(st["served_model"], "qwen3.8-flash-next-fake")
        self.assertIsNotNone(st["idle"])  # its idle stop reads Gufo's request counters
        self.assertTrue(os.path.exists(os.path.join(self.qdir(), "alive")))
        self.assertIn("max_tokens=64 thinking=False", open(os.path.join(self.qdir(), "rank.log")).read())  # the first request
        logs = self.H.view_logs(1, 50)
        self.assertTrue(any("one-box Qwen is up" in l for l in logs["lines"]), logs)
        self.H.stop()
        st = self.wait_state(("idle", "failed"), 60)
        self.assertEqual(st["state"], "idle", st["reason"])
        self.assertFalse(os.path.exists(os.path.join(self.qdir(), "alive")))
        self.assertTrue(open(os.path.join(self.qdir(), "state")).read().startswith("released"))
        self.assertIsNone(self.H.view_state()["served_model"])

    def test_up_fails_then_stays_failed(self):
        open(os.path.join(self.qdir(), "fail-up"), "w").close()
        self.H.start("qwen38-flash-next")
        st = self.wait_state(("failed",), 90)
        self.assertIn("ended before the server was ready", st["reason"])
        self.assertIn("not restarted automatically", st["reason"])
        downs = [e for e in self.H.view_events(200)["events"] if e["kind"] == "cmd" and "down ->" in (e["detail"] or "")]
        self.assertEqual(len(downs), 1)  # the failed start was cleaned up with halo-qwen down, and not retried

    def test_someone_elses_engine_is_left_alone(self):
        open(os.path.join(self.qdir(), "alive"), "w").close()  # the team's own halo-qwen is running
        open(os.path.join(self.qdir(), "state"), "w").write("rank 0 running (theirs)\n")
        self.H.start("qwen38-flash-next")
        st = self.wait_state(("failed",), 30)
        self.assertIn("not started by StrixFold UI", st["reason"])
        self.assertTrue(os.path.exists(os.path.join(self.qdir(), "alive")))  # never `down`ed
        self.assertEqual(open(os.path.join(self.qdir(), "state")).read(), "rank 0 running (theirs)\n")
        self.assertFalse([e for e in self.H.view_events(100)["events"] if "down ->" in (e["detail"] or "")])

    def test_pending_until_verified(self):
        self.H.cfg["models_ready"] = []
        self.assertIn("Not verified on your boxes", self.H.check_installs()["qwen38"]["why"])
        with self.assertRaises(halod.profiles.Invalid):
            self.H.start("qwen38-flash-next")

    def test_idle_stop_from_its_counters(self):
        self.H.cfg["minute_s"] = 0.05  # 45 "minutes" = 2.25 s
        self.H.start("qwen38-flash-next")
        self.wait_state(("ready",), 60)
        self.assertIsNotNone(self.H.view_state()["idle"])
        st = self.wait_state(("idle",), 60)
        self.assertIn("idle for 45 minutes", st["reason"])
        self.assertFalse(os.path.exists(os.path.join(self.qdir(), "alive")))

    def test_one_server_per_box_on_its_health_path(self):
        # Kyojin through halo-qwen-kyojin: ready on /health (its stand-in answers 404 on /ready), checked on both boxes
        self.assertTrue(self.H.check_installs()["qwen38-kyojin"]["ok"])
        self.H.start("qwen38-kyojin-64k")
        st = self.wait_state(("ready", "failed"), 60)
        self.assertEqual(st["state"], "ready", st["reason"])
        # the profile's window, sessions and idle minutes reach the engine's start command
        self.assertEqual(open(os.path.join(self.root, "kyojin", "env")).read().split(),
                         ["KQ_CONTEXT=131072", "KQ_IDLE_MIN=45", "KQ_SESSIONS=2"])  # Kyojin's pool: the window x sessions
        self.assertEqual((st["profile"]["settings"] or {}).get("context"), 65536)  # what Chat budgets on
        holds = [os.path.join(self.root, b, "kyojin", "hold") for b in ("box1", "box2")]
        self.H.hold(True)  # keep running reaches the engine's own idle timer on each box
        self.assertTrue(all(os.path.exists(h) for h in holds))
        self.H.hold(False)
        self.assertFalse(any(os.path.exists(h) for h in holds))
        self.assertEqual(st["copies"], ["box1", "box2"])
        self.assertEqual(st["endpoint"], f"http://127.0.0.1:{self.H.cfg['external']['qwen38-kyojin']['port']}/v1")
        probes = [e["detail"] for e in self.H.view_events(100)["events"] if e["kind"] == "probe"]
        self.assertTrue(any("first request on box1 ok" in d for d in probes), probes)
        self.assertTrue(any("first request on box2 ok" in d for d in probes), probes)
        self.assertIsNone(st["idle"])  # no request counters: no idle stop
        self.H.stop()
        st = self.wait_state(("idle", "failed"), 60)
        self.assertEqual(st["state"], "idle", st["reason"])
        self.assertTrue(open(os.path.join(self.root, "kyojin", "state")).read().startswith("released"))
        self.H.cfg["models_ready"] = ["qwen38"]
        self.assertIn("Not verified on your boxes", self.H.check_installs()["qwen38-kyojin"]["why"])

    def kyojin_stopped(self, state=None, marker_on=()):
        """Both boxes' nodes gone, as after the node's idle `down`: the server, each box's alive file; optionally a state
        line and the idle marker on some boxes only."""
        k = os.path.join(self.root, "kyojin")
        os.kill(int(open(os.path.join(k, "server.pid")).read()), 9)
        for b in ("box1", "box2"):
            d = os.path.join(self.root, b, "kyojin")
            os.unlink(os.path.join(d, "alive"))
            if state:
                open(os.path.join(d, "state"), "w").write(state + "\n")
            if b in marker_on:
                open(os.path.join(d, "idle-stop"), "w").close()

    def test_an_engine_that_stops_itself_when_idle_is_an_idle_stop(self):
        self.H.start("qwen38-kyojin-64k")
        self.wait_state(("ready",), 60)
        self.kyojin_stopped(state="released (idle 45 min)")
        st = self.wait_state(("idle", "failed"), 60)
        self.assertEqual(st["state"], "idle", st["reason"])
        self.assertIn("stopped itself", " ".join(e["detail"] or "" for e in self.H.view_events(50)["events"]))
        # a node killed before its exit trap, and the other box never told: the idle marker on box2 alone says why
        self.H.start("qwen38-kyojin-64k")
        self.wait_state(("ready",), 60)
        self.kyojin_stopped(marker_on=("box2",))
        st = self.wait_state(("idle", "failed"), 60)
        self.assertEqual(st["state"], "idle", st["reason"])

    def test_keep_running_set_before_the_start_or_in_the_lock_wait_reaches_the_engine(self):
        holds = [os.path.join(self.root, b, "kyojin", "hold") for b in ("box1", "box2")]
        self.H.hold(True)  # before the start
        self.H.start("qwen38-kyojin-64k")
        self.assertEqual(self.wait_state(("ready", "failed"), 60)["state"], "ready")
        self.assertTrue(all(os.path.exists(h) for h in holds))  # the node removed it at the lock; halod wrote it again
        self.H.stop()
        self.wait_state(("idle",), 60)
        self.H.hold(False)
        open(os.path.join(self.root, "kyojin", "up-s"), "w").write("3")  # a long lock wait
        self.H.start("qwen38-kyojin-64k")
        self.wait_state(("waiting",), 30)
        self.H.hold(True)  # during the wait
        self.assertEqual(self.wait_state(("ready", "failed"), 60)["state"], "ready")
        self.assertTrue(all(os.path.exists(h) for h in holds))

    def test_one_copy_dies_and_the_other_keeps_serving(self):
        self.H.cfg["health_fail_s"] = 3
        self.H.start("qwen38-kyojin-64k")
        self.assertEqual(self.wait_state(("ready", "failed"), 60)["state"], "ready")
        os.unlink(os.path.join(self.root, "box2", "kyojin", "alive"))  # box2's node is gone; box1's still serves
        t0 = time.time()
        while time.time() - t0 < 30 and self.H.view_state().get("copies") != ["box1"]:
            time.sleep(0.3)
        st = self.H.view_state()
        self.assertEqual((st["state"], st["copies"], st["reason"]), ("ready", ["box1"], "one copy (box2 down)"))
        self.assertTrue(any(e["kind"] == "copies" and "box2 dropped" in (e["detail"] or "") for e in self.H.view_events(50)["events"]))
        time.sleep(4)
        self.assertEqual(self.H.view_state()["state"], "ready")  # still serving past health_fail_s
        # the survivor's node is told, so its idle timer stops waiting for box2
        self.assertEqual(open(os.path.join(self.root, "box1", "kyojin", "peers-dropped")).read().split(), ["box2"])

    def test_an_unreachable_box_drops_its_own_copy_not_the_healthy_one(self):
        """Box2 out of reach (ssh fails) must not take box1's copy down: box2's copy is dropped after
        the grace, box1 keeps serving, and both serve again once box2 answers."""
        self.H.cfg["health_fail_s"] = 3
        self.H.start("qwen38-kyojin-64k")
        self.assertEqual(self.wait_state(("ready", "failed"), 60)["state"], "ready")
        b2 = self.H.box_named("box2")
        real = b2.run
        b2.run = lambda *a, **k: (255, "")
        t0 = time.time()
        while time.time() - t0 < 30 and self.H.view_state().get("copies") != ["box1"]:
            time.sleep(0.3)
        st = self.H.view_state()
        self.assertEqual((st["state"], st["copies"], st["reason"]), ("ready", ["box1"], "one copy (box2 down)"))
        time.sleep(4)
        self.assertEqual(self.H.view_state()["state"], "ready")
        self.assertEqual(open(os.path.join(self.root, "box1", "kyojin", "peers-dropped")).read().split(), ["box2"])
        b2.run = real  # box2 answers again: its copy rejoins
        t0 = time.time()
        while time.time() - t0 < 30 and self.H.view_state().get("copies") != ["box1", "box2"]:
            time.sleep(0.3)
        st = self.H.view_state()
        self.assertEqual((st["state"], st["copies"], st["reason"]), ("ready", ["box1", "box2"], None))
        self.assertFalse(os.path.exists(os.path.join(self.root, "box1", "kyojin", "peers-dropped")))

    def test_qwen_windows_follow_the_engine_not_the_rope_kit(self):
        self.assertEqual(self.H.kit_limit("qwen38-kyojin", "current"), 524288)
        self.assertEqual(self.H.kit_limit("qwen38-gufo21", "current"), 262144)
        lim = self.H.limits()
        self.assertEqual((lim["qwen38-kyojin-256k"], lim["qwen38-gufo21-256k"]), (524288, 262144))
        self.assertEqual(lim["quick-chat"], halod.profiles.ROPE_ROWS)  # DeepSeek keeps its RoPE kit's limit

    def test_engine_dies_while_ready(self):
        self.H.cfg["health_fail_s"] = 3
        self.H.start("qwen38-flash-next")
        self.wait_state(("ready",), 60)
        pid = int(open(os.path.join(self.qdir(), "server.pid")).read())
        os.kill(pid, 9)
        os.unlink(os.path.join(self.qdir(), "alive"))
        st = self.wait_state(("failed",), 60)
        self.assertIn("stopped answering /ready", st["reason"])


class Power(World):
    """Max speed: performance on both boxes only while the profile serves, balanced on every way out."""

    def profile_file(self, box):
        return os.path.join(self.root, box, "platform_profile")

    def profiles_now(self):
        return [open(self.profile_file(b)).read().strip() for b in ("box1", "box2")]

    def max_profile(self):
        return self.H.store.create("Fast chat", "quick-chat", {"power": "max"}, None)["id"]

    def test_max_speed_while_serving_then_balanced(self):
        pid = self.max_profile()
        self.H.start(pid)
        self.wait_state(("ready",))
        self.assertEqual(self.profiles_now(), ["performance", "performance"])
        self.H.stop()
        self.wait_state(("idle",))
        self.assertEqual(self.profiles_now(), ["balanced", "balanced"])
        writes = [e["detail"] for e in self.H.view_events(200)["events"] if e["kind"] == "power"]
        self.assertEqual(len(writes), 4)  # two writes each way, each logged
        self.assertFalse(self.H.s.get("power_set"))

    def test_default_never_writes(self):
        self.H.start("quick-chat")
        self.wait_state(("ready",))
        self.H.stop()
        self.wait_state(("idle",))
        self.assertEqual([e for e in self.H.view_events(200)["events"] if e["kind"] == "power"], [])

    def test_failure_restores_before_the_restart(self):
        pid = self.max_profile()
        self.H.start(pid)
        st = self.wait_state(("ready",))
        self.ctl("die-rank1")
        t0 = time.time()
        while time.time() - t0 < 30 and not [e for e in self.H.view_events(200)["events"] if e["kind"] == "power" and "balanced" in e["detail"]]:
            time.sleep(0.1)
        self.ctl("die-rank1", False)
        self.assertTrue([e for e in self.H.view_events(200)["events"] if e["kind"] == "power" and "balanced" in e["detail"]])
        st = self.wait_state(("ready", "failed"), 60)
        if st["state"] == "ready":  # restarted: Max speed again
            self.assertEqual(self.profiles_now(), ["performance", "performance"])

    def test_restart_without_a_run_restores(self):
        for b in ("box1", "box2"):
            open(self.profile_file(b), "w").write("performance\n")
        self.H.close()
        self.H.s["power_set"] = True
        self.H._save()
        self.H = self.make()  # halod restarted; no run recorded
        t0 = time.time()
        while time.time() - t0 < 20 and self.profiles_now() != ["balanced", "balanced"]:
            time.sleep(0.1)
        self.assertEqual(self.profiles_now(), ["balanced", "balanced"])

    def test_someone_measuring_keeps_their_setting(self):
        open(self.profile_file("box1"), "w").write("performance\n")
        self.hold_outside("box1")
        self.H.close()
        self.H = self.make()
        time.sleep(3)
        self.assertEqual(self.profiles_now()[0], "performance")
        self.assertTrue([e for e in self.H.view_events(50)["events"] if "left as it is" in (e["detail"] or "")])

    def test_exec_stop_post_restore(self):
        for b in ("box1", "box2"):
            open(self.profile_file(b), "w").write("performance\n")
        self.H.s["power_set"] = True
        self.H._save()
        self.assertEqual(halod.restore_power(halod.load_config(self.cfg)), 0)
        self.assertEqual(self.profiles_now(), ["balanced", "balanced"])


def glob_one(pattern):
    import glob
    found = glob.glob(pattern)
    assert len(found) == 1, (pattern, found)
    return found[0]


def tearDownModule():
    kill_worlds()


class KitLimit(World):
    """The longest window comes from the deploy's own RoPE kit (serve_lib.sh: its rows - 49), read
    on every box (the smallest wins; a box without a ds-kit, or none at all: the bundle kit's 65,487), and a profile
    longer than that is refused before anything running is stopped."""

    def kit(self, rows, boxes=("box1", "box2"), commit=fake_env.COMMIT, short=None):
        """A ds-kit as the team's: four RoPE tables of rows x 128 bytes (sparse files); `short` makes one table smaller
        (serve_lib takes the smallest); rope.json says something else on purpose (serve_lib does not read it)."""
        for box in boxes:
            d = os.path.join(self.root, box, "tfp", "deploy", commit, "ds-kit")
            os.makedirs(d, exist_ok=True)
            for f in profiles.KIT_TABLES:
                with open(os.path.join(d, f), "wb") as fh:
                    fh.truncate((short if short and f == "rope-compressed-sin.f32" else rows) * profiles.KIT_ROW_BYTES)
            json.dump({"rows": 999999999}, open(os.path.join(d, "rope.json"), "w"))

    def test_the_deploys_kit_sets_the_window(self):
        self.assertEqual(self.H.kit_limit("dsv41", "current", fresh=True), 65487)  # no ds-kit
        self.kit(264192)
        self.assertEqual(self.H.kit_limit("dsv41", "current", fresh=True), 264143)
        self.assertEqual(self.H.kit_limit("dsv41", fake_env.COMMIT, fresh=True), 264143)
        self.kit(264192, short=131121)  # two table sizes: the smallest decides, as in serve_lib
        self.assertEqual(self.H.kit_limit("dsv41", "current", fresh=True), 131072)
        os.unlink(os.path.join(self.root, "box2", "tfp", "deploy", fake_env.COMMIT, "ds-kit", "rope-plain-cos.f32"))
        self.assertEqual(self.H.kit_limit("dsv41", "current", fresh=True), 65487)  # a box without its tables: the bundle kit's
        self.kit(1114112)
        self.H._refresh_limits()
        lim = self.H.view_state()["limits"]
        self.assertEqual((lim["quick-chat-1m"], lim["quick-chat"]), (1114063, 1114063))
        self.assertEqual(lim["unc-chat-128k"], 65487)  # the uncensored deploy has no long kit here
        self.kit(1114112, commit=fake_env.UNC_COMMIT)
        self.H._refresh_limits()
        self.assertEqual(self.H.view_state()["limits"]["unc-chat-1m"], 1114063)
        long = next(p for p in profiles.BUILTIN if p["id"] == "quick-chat-128k")["settings"]
        self.assertEqual(profiles.validate_settings(long, max_rows=264143)["context"], 131072)
        with self.assertRaises(profiles.Invalid):
            profiles.validate_settings(long)  # the bundle kit's 65,487

    def test_a_window_the_kit_cannot_serve_is_refused_before_anything_stops(self):
        self.H.start("quick-chat")
        self.wait_state(("ready",), 60)
        run = self.H.view_state()["run_id"]
        with self.assertRaises(profiles.Invalid) as e:
            self.H.start("quick-chat-128k", replace=True)
        self.assertIn("Nothing was stopped", str(e.exception))
        st = self.H.view_state()
        self.assertEqual((st["state"], st["profile"]["id"], st["run_id"]), ("ready", "quick-chat", run))


    api = ModelsAndAdvanced.api

    def test_a_profile_longer_than_the_bundle_kit_is_saved_when_its_deploy_serves_it(self):
        # create, edit and import checked the window against the bundle kit's 65,487 only, so a 1M custom
        # profile was refused although /validate and start allowed it
        body = {"name": "Long", "base": "quick-chat", "settings": {"context": 1048576, "streams": 4}}
        code, r = self.api("POST", "/api/profiles", body)
        self.assertEqual(code, 400)  # no ds-kit yet: the bundle kit's
        self.assertIn("65,487", r["errors"]["context"])
        self.kit(1114112)
        self.H.kit_limit("dsv41", "current", fresh=True)
        code, r = self.api("POST", "/api/profiles", body)
        self.assertEqual(code, 200, r)
        pid = r["profile"]["id"]
        self.assertEqual((r["profile"]["settings"]["context"], r["profile"]["settings"]["streams"]), (1048576, 4))
        code, r = self.api("PUT", f"/api/profiles/{pid}", {"settings": {"context": 524288}})
        self.assertEqual((code, r["profile"]["settings"]["context"]), (200, 524288), r)
        code, r = self.api("POST", "/api/profiles/validate", {"base": pid, "settings": dict(self.H.store.get(pid)["settings"], context=1048576)})
        self.assertTrue(r["ok"], r)
        code, data = self.api("GET", "/api/profiles/export", token=False)
        self.H.store.delete(pid)
        code, r = self.api("POST", "/api/profiles/import", {"data": data})
        self.assertEqual((code, r["errors"]), (200, {}), r)
        code, r = self.api("PUT", f"/api/profiles/{r['created'][0]}", {"settings": {"context": 2097152}})
        self.assertEqual(code, 400, r)  # longer than the kit serves

class StallAllowance(World):
    """The no-progress rule (the lead): the engine reports nothing while it reads a prompt (its prefill speed
    comes only when a prefill ends), so a request that has not decoded yet gets time by the run's window; once it has,
    a stall is stall_s after its last sign of progress. A request that ends gives the next one a first read of its
    own."""
    overrides = {"stall_s": 240}

    def test_a_first_read_gets_time_by_the_window_and_a_decode_the_plain_rule(self):
        H, t = self.H, halod.now()
        busy = t - 30
        H.live.update(busy_since=busy, progress_at=None, done_at=None)
        for ctx, allow in ((16384, 240), (63488, 63488 / 900 + 180), (524288, 524288 / 900 + 180), (1048576, 1048576 / 900 + 180)):
            self.assertEqual(H.stall_allowance(busy, {"settings": {"context": ctx}}), (busy, max(240, allow)), ctx)
        self.assertGreater(H.stall_allowance(busy, {"settings": {"context": 524288}})[1], 7.5 * 60 + 240)  # unc 512K's read
        self.assertGreater(H.stall_allowance(busy, {"settings": {"context": 1048576}})[1], 16 * 60 + 240)  # unc 1M's
        self.assertEqual(H.stall_allowance(busy, {}), (busy, 240))  # no window known: the plain rule
        # a decode seen (health with progress while busy): the plain rule from that moment
        H._on_health({"health": {"status": "ok", "live": {"connections": 1, "decode_tokens_per_second": 30.0}}})
        since, allow = H.stall_allowance(busy, {"settings": {"context": 1048576}})
        self.assertEqual(allow, 240)
        self.assertGreaterEqual(since, t)
        # that request ended and the next began reading at once (no idle tick between): a first read again
        H.live["done_at"] = H.live["progress_at"] + 1
        self.assertEqual(H.stall_allowance(busy, {"settings": {"context": 1048576}}),
                         (H.live["done_at"], 1048576 / 900 + 180))
        # nothing running: no progress is recorded (an idle engine's zeros are not a decode)
        H.live.update(progress_at=None)
        H._on_health({"health": {"status": "ok", "live": {"connections": 0, "decode_tokens_per_second": 0.0}}})
        self.assertIsNone(H.live.get("progress_at"))


class PromptDisk(World):
    """sf-disk (StrixFold #15): the pair's disk budget (halod.json prompt_disk_gib) reaches both boxes' run conf
    as PROMPT_DISK_GIB and PROMPT_DISK_DIR when the run keeps prompt states, and the uncensored swarm is a 1M pool."""
    overrides = {"prompt_disk_gib": 20}

    def conf(self, rid):
        texts = [open(os.path.join(self.run_files(box, rid), "serve.conf")).read() for box in ("box1", "box2")]
        self.assertEqual(texts[0], texts[1])  # byte for byte: the ranks' handshake compares the conf's SHA-256
        return halod.profiles.parse_conf(texts[0])

    def test_the_swarm_runs_a_1m_pool_with_the_pairs_disk(self):
        KitLimit.kit(self, 1114112, commit=fake_env.UNC_COMMIT)  # the deploy's own 1,114,112-row RoPE kit
        self.H.start("unc-swarm")
        st = self.wait_state(("ready", "failed"))
        self.assertEqual(st["state"], "ready", st["reason"])
        v = self.conf(st["run_id"])
        self.assertEqual((v["CONTEXT"], v["STREAMS"], v["PARALLEL"], v["PEAK_GIB"]), ("1048576", "4", "4", "107"))
        self.assertEqual(v["DEPLOY_DIR"], f"$HOME/tfp/deploy/{fake_env.UNC_COMMIT}")
        self.assertEqual((v["PROMPT_DISK_GIB"], v["PROMPT_DISK_DIR"]), ("20", "$HOME/strixfold-cache/prefix"))

    def test_no_disk_without_the_prompt_cache(self):
        self.H.start("unc-chat-16k")  # prompt_cache_gib 0: the disk tier writes what the memory tier keeps
        st = self.wait_state(("ready", "failed"))
        self.assertEqual(st["state"], "ready", st["reason"])
        v = self.conf(st["run_id"])
        self.assertNotIn("PROMPT_DISK_GIB", v)
        self.assertNotIn("PROMPT_DISK_DIR", v)


class PromptDiskOff(World):
    def test_a_halod_without_a_budget_writes_no_disk_keys(self):
        self.H.start("unc-chat")
        st = self.wait_state(("ready", "failed"))
        self.assertEqual(st["state"], "ready", st["reason"])
        v = halod.profiles.parse_conf(open(os.path.join(self.run_files("box1", st["run_id"]), "serve.conf")).read())
        self.assertEqual(v["PROMPT_CACHE_GIB"], "2")
        self.assertNotIn("PROMPT_DISK_GIB", v)


class PromptDiskBadDir(World):
    overrides = {"prompt_disk_gib": 20, "prompt_disk_dir": "strixfold-cache"}

    def test_a_bad_disk_dir_refuses_the_start(self):
        self.H.start("unc-chat")
        st = self.wait_state(("failed", "ready"))
        self.assertEqual(st["state"], "failed")
        self.assertIn("halod.json: prompt_disk_dir", st["reason"])


if __name__ == "__main__":
    unittest.main()
