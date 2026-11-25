"""Lab chat and the agent sessions in Studio's private store: the move (copied with SQLite's backup,
checked by row counts and row hashes, file by file for agents; switched; only then the old copy removed; a copy that
does not check out leaves the old place in use), Studio's session on every moved route (LAN: the tailnet pointer;
the tailnet never serves them), a private default work folder (Claude Code's outside the store), and no prompt,
title or output in the app's log. Synthetic data only, in temporary folders."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import agents  # noqa: E402
import chat_team  # noqa: E402
import chats  # noqa: E402
import conductor  # noqa: E402
import halo_control as HC  # noqa: E402
import private_move as PM  # noqa: E402
import studio  # noqa: E402

CANARY = "lynx-canary-4417"


def lab(path, n=3):
    """A Lab chat database like the dashboard's, its last rows still in the write-ahead log."""
    s = chats.Store(path)
    ids = []
    for i in range(n):
        c = s.create(f"{CANARY} chat {i}", "", {})
        cid = c["id"] if isinstance(c, dict) else c
        ids.append(cid)
        s.add(cid, "user", f"{CANARY} question {i}") if hasattr(s, "add") else s.c.execute(
            "INSERT INTO messages (chat_id, role, content, created) VALUES (?,?,?,0)", (cid, "user", f"{CANARY} question {i}"))
    s.c.commit()
    return s, ids


def keep_app_globals(t):
    """A started App sets chat_team.TEAM and conductor.C: put them back, or a later file's App-less harness finds this one."""
    t.addCleanup(setattr, chat_team, "TEAM", chat_team.TEAM)
    t.addCleanup(setattr, conductor, "C", conductor.C)


class Move(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-private-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.logs = []

    def test_lab_chat_moves_whole_and_checked(self):
        old, new = os.path.join(self.tmp, "conf", "chats.sqlite"), os.path.join(self.tmp, "store", "lab", "chats.sqlite")
        s, ids = lab(old)  # left open: rows in its WAL
        want = PM.digests(old)
        self.assertEqual(PM.move_lab(old, new, self.logs.append), new)
        s.c.close()
        self.assertEqual(PM.digests(new), want)
        self.assertEqual(want["chats"][0], 3)
        for p in (old, old + "-wal", old + "-shm"):
            self.assertFalse(os.path.exists(p), p)
        self.assertEqual(stat.S_IMODE(os.stat(new).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(new)).st_mode), 0o700)
        self.assertEqual(PM.move_lab(old, new, self.logs.append), new)  # once: nothing more to do
        self.assertEqual(self.logs, ["lab chat: moved into the private store (3 chats, 3 messages)"])
        self.assertNotIn(CANARY, json.dumps(self.logs))

    def test_an_interrupted_move_finishes_or_keeps_both(self):
        """A crash between the switch and the removal left both copies, and the old one survived
        outside the store with no word. The next start compares them: the same rows: the old one goes; different: both
        stay and the log says so (counts only)."""
        old, new = os.path.join(self.tmp, "conf", "chats.sqlite"), os.path.join(self.tmp, "store", "lab", "chats.sqlite")
        s, _ = lab(old)
        s.c.close()
        PM.move_lab(old, new, self.logs.append)
        self.assertEqual(sorted(os.listdir(os.path.dirname(new))), ["chats.sqlite"])  # no -wal, -shm or .part left
        shutil.copy(new, old)  # the crash: switched, the old copy not yet removed
        self.assertEqual(PM.move_lab(old, new, self.logs.append), new)
        self.assertFalse(os.path.exists(old))
        self.assertIn("removed the old copy an interrupted move left (3 chats, 3 messages", self.logs[-1])
        shutil.copy(new, old)
        db = sqlite3.connect(old)
        db.execute("INSERT INTO chats (id, title, created, updated) VALUES ('x', 'later', 0, 0)")
        db.commit()
        db.close()
        self.assertEqual(PM.move_lab(old, new, self.logs.append), new)
        self.assertTrue(os.path.exists(old))  # different rows: both kept, the store's in use
        self.assertEqual(self.logs[-1], "lab chat: an old copy outside the private store differs from it (4 chats, 3 messages "
                                        "there, 3 chats, 3 messages in the store); both kept, the store's in use")
        self.assertNotIn(CANARY, json.dumps(self.logs))
        # agent sessions the same way
        a_old, a_new = os.path.join(self.tmp, "conf", "agents"), os.path.join(self.tmp, "store", "agents")
        for sid in ("s1", "s2"):
            for d in (a_old, a_new):
                os.makedirs(os.path.join(d, sid), exist_ok=True)
                open(os.path.join(d, sid, "log.txt"), "w").write("same\n")
        open(os.path.join(a_old, "s2", "log.txt"), "a").write("more\n")
        self.assertEqual(PM.move_agents(a_old, a_new, self.logs.append), [])
        self.assertEqual(os.listdir(a_old), ["s2"])
        self.assertEqual(self.logs[-1], "agent sessions: 0 moved into the private store; 1 old copies an interrupted move left "
                                        "removed (the same as the store's); 1 differ from the store's copy, both kept (the store's in use): s2")

    def test_a_copy_that_does_not_check_out_keeps_the_old_place(self):
        old, new = os.path.join(self.tmp, "conf", "chats.sqlite"), os.path.join(self.tmp, "store", "lab", "chats.sqlite")
        s, _ = lab(old)
        s.c.close()
        real = PM.digests
        PM.digests = lambda p: real(p) if p == old else {"chats": (0, "x")}
        self.addCleanup(setattr, PM, "digests", real)
        self.assertEqual(PM.move_lab(old, new, self.logs.append), old)
        self.assertTrue(os.path.exists(old))
        self.assertEqual(sorted(os.listdir(os.path.dirname(new))), [])  # the copy is gone, nothing half-moved
        self.assertIn("kept where it was", self.logs[0])
        self.assertNotIn(CANARY, json.dumps(self.logs))

    def test_agent_sessions_move_file_by_file(self):
        old, new = os.path.join(self.tmp, "conf", "agents"), os.path.join(self.tmp, "store", "agents")
        for sid in ("1021-1000-aaaa", "1021-1001-bbbb", "1021-1002-cccc"):
            d = os.path.join(old, sid, "deep")
            os.makedirs(d)
            json.dump({"id": sid, "spec": {"task": CANARY}}, open(os.path.join(old, sid, "session.json"), "w"))
            open(os.path.join(old, sid, "log.txt"), "w").write(f"{CANARY} output\n")
            open(os.path.join(d, "x.bin"), "wb").write(os.urandom(4096))
        os.makedirs(os.path.join(new, "1021-1002-cccc"))  # already there: left alone, the old one kept
        before = {sid: PM.file_digests(os.path.join(old, sid)) for sid in os.listdir(old)}
        self.assertEqual(PM.move_agents(old, new, self.logs.append), ["1021-1000-aaaa", "1021-1001-bbbb"])
        for sid in ("1021-1000-aaaa", "1021-1001-bbbb"):
            self.assertEqual(PM.file_digests(os.path.join(new, sid)), before[sid])
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(new, sid)).st_mode), 0o700)
        self.assertEqual(os.listdir(old), ["1021-1002-cccc"])
        self.assertEqual(self.logs, ["agent sessions: 2 moved into the private store; 1 differ from the store's copy, both kept "
                                     "(the store's in use): 1021-1002-cccc"])

    def test_the_app_moves_them_at_start_and_reads_them_there(self):
        conf, store = os.path.join(self.tmp, "conf"), os.path.join(self.tmp, "StrixFoldUI-test")
        old_conf = HC.CONF_DIR
        HC.CONF_DIR = conf
        self.addCleanup(setattr, HC, "CONF_DIR", old_conf)
        s, ids = lab(os.path.join(conf, "chats.sqlite"))
        s.c.close()
        os.makedirs(os.path.join(conf, "agents", "1021-1000-aaaa"))
        json.dump({"id": "1021-1000-aaaa", "spec": {"adapter": "builtin", "workdir": self.tmp, "task": "t", "name": "n", "profile": "any"},
                   "status": "done"}, open(os.path.join(conf, "agents", "1021-1000-aaaa", "session.json"), "w"))
        cfg = dict(HC.DEFAULTS, ui_port=0, halod_local_port=1, endpoint_local_port=9, endpoint2_local_port=9, studio_root_override=store,
                   agents_outside_root=os.path.join(self.tmp, "HaloAgents"))
        keep_app_globals(self)
        app = HC.App(cfg)
        lines = []
        real_log = HC.log
        HC.log = lines.append
        self.addCleanup(setattr, HC, "log", real_log)
        app.start_runner()
        self.addCleanup(lambda: setattr(app.studio["engine"], "stopped", True))
        self.assertEqual(app.chats.path, os.path.join(os.path.realpath(store), "lab", "chats.sqlite"))
        self.assertEqual(sorted(c["id"] for c in app.chats.list()) if hasattr(app.chats, "list") else
                         sorted(r[0] for r in app.chats.c.execute("SELECT id FROM chats")), sorted(ids))
        self.assertEqual(app.runner.root, os.path.join(os.path.realpath(store), "agents"))
        self.assertIn("1021-1000-aaaa", app.runner.sessions)
        self.assertFalse(os.path.exists(os.path.join(conf, "chats.sqlite")))
        self.assertNotIn(CANARY, json.dumps(lines))


class Workdir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-agents-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.r = agents.Runner(os.path.join(self.tmp, "store", "agents"), lambda st=None: "http://127.0.0.1:9/v1", lambda fresh=False: {"state": "idle"},
                               outside=os.path.join(self.tmp, "HaloAgents"))
        self.r.closed = True

    def test_a_private_folder_by_default(self):
        v = self.r.create({"adapter": "builtin", "task": "look around", "workdir": ""})
        self.assertEqual(v["spec"]["workdir"], os.path.realpath(os.path.join(self.tmp, "store", "agents", v["id"], "work")))
        self.assertEqual(stat.S_IMODE(os.stat(v["spec"]["workdir"]).st_mode), 0o700)
        # Claude Code: Claude's deny rule on the store would keep it out, so its folder is beside the store
        for cmd in ("claude -p {task}", "/opt/homebrew/bin/claude --print {task}", "npx @anthropic-ai/claude-code -p {task}"):
            v = self.r.create({"adapter": "command", "command": cmd, "task": "fix it", "workdir": ""})
            self.assertEqual(v["spec"]["workdir"], os.path.realpath(os.path.join(self.tmp, "HaloAgents", v["id"])), cmd)
        v = self.r.create({"adapter": "command", "command": "aider --message {task}", "task": "x", "workdir": ""})
        self.assertTrue(v["spec"]["workdir"].endswith(os.path.join(v["id"], "work")))
        mine = os.path.join(self.tmp, "project")
        os.makedirs(mine)
        v = self.r.create({"adapter": "builtin", "task": "x", "workdir": mine})  # the user's own choice stays
        self.assertEqual(v["spec"]["workdir"], os.path.realpath(mine))

    def test_a_claude_code_folder_comes_into_the_store_when_the_session_ends(self):
        """the user: all output into the private folder. Claude Code works outside the store while it runs (its deny
        rule); when the session ends its folder is copied in, checked, and the outside copy removed. A failed check
        keeps both. A folder the user chose stays where the user put it."""
        v = self.r.create({"adapter": "command", "command": "claude -p {task}", "task": "fix it", "workdir": ""})
        s = self.r.sessions[v["id"]]
        out = v["spec"]["workdir"]
        os.makedirs(os.path.join(out, "src"))
        open(os.path.join(out, "src", "fix.py"), "w").write("print('fixed')\n")
        os.chmod(os.path.join(out, "src", "fix.py"), 0o755)
        s.set("done", "finished")
        home = os.path.join(self.tmp, "store", "agents", v["id"], "work")
        self.assertFalse(os.path.exists(out))
        self.assertEqual(open(os.path.join(home, "src", "fix.py")).read(), "print('fixed')\n")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(home, "src", "fix.py")).st_mode), 0o755)
        self.assertEqual((s.spec["workdir"], s.spec["outside_default"], s.spec["ran_in"]), (os.path.realpath(home), False, out))
        self.assertIn("work folder: moved into the private store", open(os.path.join(s.dir, "log.txt")).read())
        # a copy that does not check out: both stay, the log says so
        v2 = self.r.create({"adapter": "command", "command": "claude -p {task}", "task": "x", "workdir": ""})
        open(os.path.join(v2["spec"]["workdir"], "a.txt"), "w").write("a")
        real = __import__("private_move").file_digests
        __import__("private_move").file_digests = lambda p: {"x": p}
        self.addCleanup(setattr, __import__("private_move"), "file_digests", real)
        self.r.sessions[v2["id"]].set("failed", "x")
        self.assertTrue(os.path.exists(os.path.join(v2["spec"]["workdir"], "a.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "store", "agents", v2["id"], "work")))
        self.assertIn("did not check out", open(os.path.join(self.r.sessions[v2["id"]].dir, "log.txt")).read())
        __import__("private_move").file_digests = real
        # a folder the user chose: never moved
        mine = os.path.join(self.tmp, "mine")
        os.makedirs(mine)
        v3 = self.r.create({"adapter": "command", "command": "claude -p {task}", "task": "x", "workdir": mine})
        self.r.sessions[v3["id"]].set("done")
        self.assertTrue(os.path.isdir(mine))
        # a session that ended while the app was away: its folder comes in at the next start
        v4 = self.r.create({"adapter": "command", "command": "claude -p {task}", "task": "x", "workdir": ""})
        s4 = self.r.sessions[v4["id"]]
        with s4.mu:
            s4.status, s4.ended = "stopped", 1.0
        s4.save()
        r2 = agents.Runner(os.path.join(self.tmp, "store", "agents"), lambda st=None: "http://127.0.0.1:9/v1", lambda fresh=False: {"state": "idle"},
                           outside=os.path.join(self.tmp, "HaloAgents"))
        r2.closed = True
        self.assertFalse(os.path.exists(v4["spec"]["workdir"]))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "store", "agents", v4["id"], "work")))

    def test_the_fake_dashboard_never_uses_the_real_halo_agents(self):
        cfg = dict(HC.DEFAULTS, ui_port=0, halod_local_port=1, endpoint_local_port=9, endpoint2_local_port=9,
                   studio_root_override=os.path.join(self.tmp, "StrixFoldUI-test"))
        keep_app_globals(self)
        app = HC.App(cfg, True)
        app.fake_root = os.path.join(self.tmp, "fake")
        app.start_runner()
        self.addCleanup(lambda: setattr(app.studio["engine"], "stopped", True))
        self.assertEqual(app.runner.outside, os.path.join(self.tmp, "fake", "HaloAgents"))

    def test_the_lan_is_told_these_pages_are_mac_only(self):
        js = open(os.path.join(ROOT, "mac", "ui", "chat.js")).read()
        self.assertIn("Lab chat and agents open on this Mac only. On your phone, use the chat.", js)

    def test_the_store_keeps_a_work_folders_file_modes(self):
        st_root = os.path.join(self.tmp, "store")
        v = self.r.create({"adapter": "builtin", "task": "x", "workdir": ""})
        script = os.path.join(v["spec"]["workdir"], "run.sh")
        open(script, "w").write("#!/bin/sh\n")
        os.chmod(script, 0o755)
        studio.Store(st_root)  # its start closes every folder and file: not a project's scripts
        self.assertEqual(stat.S_IMODE(os.stat(script).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(v["spec"]["workdir"]).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(st_root, "agents", v["id"], "session.json")).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
