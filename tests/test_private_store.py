"""Private chats in the store (mac/private_store.py and studio.Store): a Private chat's
title, messages, waiting messages and attachments are sealed before they reach SQLite or a file; every read needs the
grant and Private unlocked, so every other caller (the Command Centre, Agents, the outbox while locked, search, the
list) gets Locked or "Private chat"; delete removes the key first. A canary is searched for in the whole store folder,
the SQLite file and its WAL included, in plain text, base64 and hex. Synthetic chats only."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import base64
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import mediainfo  # noqa: E402
import private_store as P  # noqa: E402
import studio  # noqa: E402

CANARY = "zebra-canary-private-7f3a"


def setUpModule():  # Private's photo and PDF reader, as private_code.install sets it up at app start (built once here)
    import private_reader as R
    global _READER
    _READER = R.READER
    R.READER = R.Reader(open(os.path.join(ROOT, "mac", "halo_extract.swift"), "rb").read(), True)


def tearDownModule():
    import private_reader as R
    R.READER.close()
    R.READER = _READER
PASS = "correct horse battery staple"


def forms(s):
    """The canary as it could sit on disk: plain (UTF-8 and UTF-16), hex, and base64 at each of the three alignments."""
    b = s.encode()
    out = {b, s.encode("utf-16-le"), b.hex().encode(), b.hex().upper().encode()}
    for pad in range(3):
        e = base64.b64encode(b"\0" * pad + b)
        out.add(e[4:-4])  # the stretch that depends on the canary alone
    return out


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hs-private-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.st = studio.Store(os.path.join(self.tmp, "S"))
        self.st.vault.unlock(PASS)

    def g(self):
        return P.granted()

    def fill(self):
        """A Private chat with the canary everywhere it can go."""
        with self.g():
            c = self.st.create_chat(kind="chat", private=True, title=f"About {CANARY}")
            cid = c["id"]
            m1 = self.st.add_message(cid, "user", f"my secret is {CANARY}", {"attachments": [], "note": CANARY})
            m2 = self.st.add_message(cid, "assistant", "", {"partial": True})
            self.st.set_message(m2, f"you said {CANARY}", {"reasoning": f"thinking about {CANARY}"})
            self.st.rename_chat(cid, f"Renamed {CANARY}")
            self.st.enqueue(cid, f"queued {CANARY}", {"attachments": [], "effort": "low", "x": CANARY})
            t = self.st.add_upload(f"notes with {CANARY}\n".encode(), f"{CANARY}.txt", cid, text_ok=True)
            pic = self.st.add_upload(mediainfo.test_pattern(64, 48, 3), f"{CANARY}.png", cid, text_ok=True)
        return cid, (m1, m2), t, pic

    def scan(self):
        """Every file under the store folder (the SQLite file, its WAL and shm, refs/) holds none of the canary's forms."""
        hits = []
        fs = forms(CANARY)
        for dp, dn, fn in os.walk(self.st.root):
            for f in fn:
                data = open(os.path.join(dp, f), "rb").read()
                hits += [(os.path.join(dp, f), x) for x in fs if x in data]
        return hits

    def test_nothing_sealed_is_on_disk_in_plain(self):
        cid, (m1, m2), t, pic = self.fill()
        self.assertTrue(os.path.exists(self.st.path("studio.sqlite-wal")))  # WAL mode: the log is checked too
        self.assertEqual(self.scan(), [])
        # and the store still says it, with the grant
        with self.g():
            c = self.st.chat(cid)
            self.assertEqual(c["title"], f"Renamed {CANARY}")
            self.assertTrue(c["private"])
            self.assertNotIn("pkey", c)
            self.assertEqual([m["content"] for m in c["messages"]], [f"my secret is {CANARY}", f"you said {CANARY}"])
            self.assertEqual(c["messages"][0]["meta"]["note"], CANARY)
            self.assertEqual(c["messages"][1]["meta"]["reasoning"], f"thinking about {CANARY}")
            self.assertEqual(self.st.message(m1)["content"], f"my secret is {CANARY}")
            w = self.st.waiting(cid)[0]
            self.assertEqual((w["content"], w["meta"]["x"]), (f"queued {CANARY}", CANARY))
            self.assertEqual(self.st.upload(t["id"])["name"], f"{CANARY}.txt")
            self.assertEqual(self.st.upload_text(t["id"]), f"notes with {CANARY}\n")
            self.assertEqual(self.st.upload_bytes(pic["id"])[:8], b"\x89PNG\r\n\x1a\n")
        # no sha256, no size in pixels for a private upload
        raw = self.st.one("SELECT sha256, width, height, name FROM uploads WHERE id=?", (pic["id"],))
        self.assertEqual((raw["sha256"], raw["width"], raw["height"]), (None, None, None))
        self.assertTrue(raw["name"].startswith("hpv:"))
        self.assertEqual(sorted(os.listdir(self.st.path("refs"))), sorted([f"{t['id']}.txt.hpv", f"{pic['id']}.hpv", f"{pic['id']}.txt.hpv"]))

    def test_without_the_grant_every_read_is_locked(self):
        cid, (m1, m2), t, pic = self.fill()
        for read in (lambda: self.st.chat(cid), lambda: self.st.messages(cid), lambda: self.st.message(m1),
                     lambda: self.st.upload(t["id"]), lambda: self.st.upload_text(t["id"]), lambda: self.st.upload_bytes(pic["id"]),
                     lambda: self.st.add_message(cid, "user", "x"), lambda: self.st.set_message(m1, "x"),
                     lambda: self.st.rename_chat(cid, "x"), lambda: self.st.enqueue(cid, "x", {}), lambda: self.st.delete_chat(cid),
                     lambda: self.st.create_chat(kind="chat", private=True),
                     lambda: self.st.add_upload(b"text", "a.txt", cid, text_ok=True)):
            with self.assertRaises(P.Locked):
                read()
        # the list says "Private chat"; search never finds it; the outbox sees a sealed item it leaves alone
        row = next(r for r in self.st.chats(kind="all") if r["id"] == cid)
        self.assertEqual((row["title"], row["private"]), ("Private chat", True))
        self.assertEqual(self.st.chats(q=CANARY, kind="all"), [])
        self.assertEqual(self.st.chats(q="hpv", kind="all"), [])
        w = self.st.waiting()
        self.assertEqual((w[0]["content"], w[0]["meta"]), ("", {"locked_private": True, "attachments": []}))
        # nothing changed by the refused writes
        with self.g():
            self.assertEqual(len(self.st.messages(cid)), 2)
            self.assertEqual(self.st.chat(cid)["title"], f"Renamed {CANARY}")

    def test_another_thread_has_no_grant_unless_it_carries_the_context(self):
        cid = self.fill()[0]
        got = {}

        def plain():
            try:
                self.st.chat(cid)
                got["plain"] = "read"
            except P.Locked:
                got["plain"] = "locked"
        th = threading.Thread(target=plain)
        with self.g():
            th.start()
            th.join()
        self.assertEqual(got["plain"], "locked")  # a thread doesn't inherit the grant by itself
        import contextvars
        with self.g():
            ctx = contextvars.copy_context()
        th = threading.Thread(target=ctx.run, args=(lambda: got.__setitem__("ctx", self.st.chat(cid)["title"]),))
        th.start()
        th.join()
        self.assertEqual(got["ctx"], f"Renamed {CANARY}")  # halo_chat.run_detached carries it this way

    def test_lock_idle_lock_and_the_passcode(self):
        cid = self.fill()[0]
        self.st.vault.lock()
        with self.g(), self.assertRaises(P.Locked):
            self.st.chat(cid)
        with self.assertRaises(P.Locked):
            self.st.vault.unlock("not the passcode")
        self.assertFalse(self.st.vault.unlocked())
        self.st.vault.unlock(PASS)
        with self.g():
            self.assertEqual(self.st.chat(cid)["title"], f"Renamed {CANARY}")
        self.st.vault.used = time.time() - P.IDLE_S - 1  # 30 minutes with no private request
        self.assertFalse(self.st.vault.unlocked())
        with self.g(), self.assertRaises(P.Locked):
            self.st.chat(cid)
        # a passcode change keeps the chats: the same key, wrapped under the new passcode
        self.st.set_kv("private_wrap", self.st.vault.rewrapped(PASS, "a brand new passphrase"))
        with self.assertRaises(P.Locked):
            self.st.vault.unlock(PASS)
        self.st.vault.unlock("a brand new passphrase")
        with self.g():
            self.assertEqual(self.st.chat(cid)["title"], f"Renamed {CANARY}")
        # an app restart: a new Store on the same folder starts locked
        st2 = studio.Store(self.st.root)
        with self.g(), self.assertRaises(P.Locked):
            st2.chat(cid)

    def test_delete_takes_the_key_first_then_every_file(self):
        cid, _, t, pic = self.fill()
        pk = self.st.one("SELECT pkey FROM chats WHERE id=?", (cid,))["pkey"]
        old = {pk.encode(), base64.b64decode(pk)}  # the wrapped key as stored, and its bytes
        db = self.st.path("studio.sqlite")
        self.assertTrue(any(x in open(db, "rb").read() + open(db + "-wal", "rb").read() for x in old))  # there before
        with self.g():
            self.st.delete_chat(cid)
        # secure_delete zeroes the freed space, and the checkpoint leaves no old page in the WAL
        for f in (db, db + "-wal"):
            data = open(f, "rb").read() if os.path.exists(f) else b""
            self.assertFalse([x for x in old if x in data], f)
        self.assertEqual(self.st.one("PRAGMA secure_delete")["secure_delete"], 1)
        self.assertEqual(os.listdir(self.st.path("refs")), [])
        self.assertIsNone(self.st.one("SELECT 1 AS x FROM chats WHERE id=?", (cid,)))
        for table in ("messages", "outbox", "uploads"):
            self.assertIsNone(self.st.one(f"SELECT 1 AS x FROM {table} WHERE chat_id=?", (cid,)), table)

    def test_a_passcode_change_is_checked_first_and_written_with_the_wrap(self):
        """A refused change leaves both the passcode and Private's wrap as they were; an accepted one writes
        both together. Lengths 5 and 129 refused, 6 and 128 taken, with Private set up (P4, the user: 6 digits);
        6 without it."""
        auth = studio.Auth(self.st)
        auth._store(PASS)
        cid = self.fill()[0]
        with self.g():
            title = self.st.chat(cid)["title"]

        def both():
            return self.st.kv("passcode"), self.st.kv("private_wrap")
        for n in (5, 129):
            before = both()
            with self.assertRaisesRegex(ValueError, "characters"):
                auth.change(PASS, "x" * n, "127.0.0.1", True)
            self.assertEqual(both(), before, n)
        self.assertTrue(auth.verify(PASS, "127.0.0.1", True))
        with self.assertRaises(PermissionError):  # a wrong current passcode changes nothing either
            auth.change("not the passcode", "y" * 12, "127.0.0.1", True)
        for n in (6, 128):
            new = "9" * n
            old = PASS if n == 6 else "9" * 6
            auth.change(old, new, "127.0.0.1", True)
            self.assertFalse(self.st.vault.unlocked())  # a change locks Private
            self.assertTrue(auth.verify(new, "127.0.0.1", True))
            self.assertEqual(self.st.kv("passcode")["rounds"], 1_000_000)
            self.st.vault.unlock(new)  # the same key, under the new passcode: the chat still reads
            with self.g():
                self.assertEqual(self.st.chat(cid)["title"], title)
        # a failure between the two records can't split them: one transaction
        real = self.st.vault.rewrapped
        self.st.vault.rewrapped = lambda o, n: (_ for _ in ()).throw(RuntimeError("disk full"))
        before = both()
        with self.assertRaises(RuntimeError):
            auth.change("9" * 128, "w" * 12, "127.0.0.1", True)
        self.assertEqual(both(), before)
        self.st.vault.rewrapped = real
        # without Private, 6 characters are still enough for the app
        st2 = studio.Store(os.path.join(self.tmp, "S2"))
        a2 = studio.Auth(st2)
        a2._store("abcdef")
        a2.change("abcdef", "ghijkl", "127.0.0.1", True)
        self.assertTrue(a2.verify("ghijkl", "127.0.0.1", True))

    def test_the_list_never_carries_a_private_chats_perm_opts(self):
        """The chat list sends perm_opts (Full's folder line); a Private chat's is blanked, with the grant
        or without it."""
        cid = self.fill()[0]
        plain = self.st.create_chat(kind="chat")["id"]
        self.st.x("UPDATE chats SET perm_opts=? WHERE id=?", ('{"last_folder": "~/HaloChats/x"}', plain))
        for ctx in (self.g(), contextlib.nullcontext()):
            with ctx:
                rows = {r["id"]: r for r in self.st.chats(kind="all")}
            self.assertIsNone(rows[cid]["perm_opts"])
            self.assertEqual(rows[plain]["perm_opts"], '{"last_folder": "~/HaloChats/x"}')

    def test_full_access_stays_off_at_the_data_layer(self):
        """NICE: whoever writes, a Private chat never gets Full access; the sealed container levels it may have
       ."""
        cid = self.fill()[0]
        with self.g():
            with self.assertRaisesRegex(ValueError, "Full access isn't offered in a Private chat"):
                self.st.set_perm(cid, "full", {"scope": "folder"})
            for lv in ("box", "box_web", "none"):
                self.assertEqual(self.st.set_perm(cid, lv, {})["perm"], lv)
            self.st.update_chat(cid, effort="high")

    def test_the_sealed_folders_key_and_version(self):
        """The key is made from the chat's key (stable, never stored, another chat's differs); the version only
        goes up, is sealed in the row, and nothing of it opens without the grant."""
        cid = self.fill()[0]
        with self.g():
            other = self.st.create_chat(kind="chat", private=True)["id"]
            wk, ver = self.st.private_workspace(cid)
            self.assertEqual((len(wk), ver), (32, 0))
            self.assertEqual(self.st.private_workspace(cid)[0], wk)
            self.assertNotEqual(self.st.private_workspace(other)[0], wk)
            self.st.set_private_workspace_ver(cid, 3)
            self.st.set_private_workspace_ver(cid, 2)  # never down
            self.assertEqual(self.st.private_workspace(cid)[1], 3)
            plain = self.st.create_chat(kind="chat")["id"]
            with self.assertRaisesRegex(ValueError, "not a Private chat"):
                self.st.private_workspace(plain)
            self.assertNotIn("pwork", self.st.chat(cid))  # the row's record never goes out with the chat
        row = self.st.one("SELECT pwork FROM chats WHERE id=?", (cid,))["pwork"]
        self.assertTrue(row.startswith(P.PREFIX))
        with self.assertRaises(P.Locked):
            self.st.private_workspace(cid)
        for dp, dn, fn in os.walk(self.st.root):  # the key itself is nowhere on disk, in any form
            for f in fn:
                data = open(os.path.join(dp, f), "rb").read()
                self.assertFalse(any(x in data for x in (wk, wk.hex().encode(), base64.b64encode(wk))), f)
        with self.g():  # a changed record says so, never a version of 0
            self.st.x("UPDATE chats SET pwork=? WHERE id=?", (row[:-6] + ("A" if row[-6] != "A" else "B") + row[-5:], cid))
            with self.assertRaisesRegex(ValueError, "can't be read"):
                self.st.private_workspace(cid)

    def test_a_damaged_chat_key_is_unreadable_and_deletes(self):
        cid = self.fill()[0]
        pk = bytearray(base64.b64decode(self.st.one("SELECT pkey FROM chats WHERE id=?", (cid,))["pkey"]))
        pk[33] ^= 1
        self.st.x("UPDATE chats SET pkey=? WHERE id=?", (base64.b64encode(bytes(pk)).decode(), cid))
        self.st.vault.forget(cid)
        with self.g():
            with self.assertRaises(P.Unreadable) as e:
                self.st.chat(cid)
            self.assertEqual(str(e.exception), P.UNREADABLE_CHAT)
            self.assertTrue(isinstance(e.exception, P.Locked))
            self.st.delete_chat(cid)
        self.assertIsNone(self.st.one("SELECT id FROM chats WHERE id=?", (cid,)))
        with self.assertRaises(P.Locked):  # never Unreadable without the grant: it says nothing more than locked
            cid2 = self.fill()[0]
            self.st.x("UPDATE chats SET pkey='AAAA' WHERE id=?", (cid2,))
            self.st.vault.forget(cid2)
            try:
                self.st.chat(cid2)
            except P.Unreadable:
                self.fail("Unreadable without the grant")

    def test_a_changed_field_reads_as_damaged_and_the_rest_still_reads(self):
        cid, (m1, m2), _, _ = self.fill()
        v = self.st.one("SELECT content FROM messages WHERE id=?", (m1,))["content"]
        b = bytearray(base64.b64decode(v[4:]))
        b[30] ^= 1
        self.st.x("UPDATE messages SET content=? WHERE id=?", ("hpv:" + base64.b64encode(bytes(b)).decode(), m1))
        # a sealed field moved to another row doesn't open there either
        self.st.x("UPDATE messages SET meta=(SELECT meta FROM messages WHERE id=?) WHERE id=?", (m1, m2))
        with self.g():
            ms = self.st.messages(cid)
        self.assertEqual(ms[0]["content"], P.UNREADABLE)
        self.assertEqual(ms[1]["meta"], {"unreadable": True})
        self.assertEqual(ms[1]["content"], f"you said {CANARY}")

    def test_a_private_chat_is_a_new_halo_chat_only(self):
        with self.g():
            bad = [{"kind": "chat", "cousteau": "notes-app"}]
            for kw in bad:
                with self.assertRaises(ValueError):
                    self.st.create_chat(private=True, **kw)
        c = self.st.create_chat(kind="chat")  # an ordinary chat needs no grant and stays as before
        self.assertFalse(c["private"])
        self.st.add_message(c["id"], "user", "plain words")
        self.assertIn(b"plain words", open(self.st.path("studio.sqlite-wal"), "rb").read() +
                      open(self.st.path("studio.sqlite"), "rb").read())

    def test_what_a_private_chat_wont_take(self):
        with self.g():
            cid = self.st.create_chat(kind="chat", private=True)["id"]
            for data, name, words in ((b"%PDF-1.4 x", "a.pdf", "This PDF couldn't be read"),
                                      (b"PK\x03\x04 docx", "a.docx", "Office files"),
                                      (mediainfo.test_pattern(8, 8, 3)[:0] + b"ID3\x04" + b"\0" * 64, "a.mp3", "audio, video")):
                with self.assertRaisesRegex(ValueError, words):
                    self.st.add_upload(data, name, cid, text_ok=True)
        self.assertEqual(os.listdir(self.st.path("refs")), [])


class Readers(unittest.TestCase):
    """The callers that are not the user's unlocked page never get a Private chat's words."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hs-private-r-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.st = studio.Store(os.path.join(self.tmp, "S"))
        self.st.vault.unlock(PASS)
        with P.granted():
            self.cid = self.st.create_chat(kind="chat", model="quick-chat", private=True)["id"]
            self.st.add_message(self.cid, "user", f"hello {CANARY}")
        self.plain = self.st.create_chat(kind="chat", model="quick-chat")["id"]
        self.st.add_message(self.plain, "user", "hello")

    def test_the_rewarm_never_picks_a_private_chat(self):
        import halo_agents
        ids = [r["id"] for r in self.st.q(halo_agents.REWARM_SQL)]
        self.assertEqual(ids, [self.plain])

    def test_the_outbox_leaves_a_locked_private_message_waiting(self):
        import outbox
        with P.granted():
            self.st.enqueue(self.cid, f"later {CANARY}", {"attachments": [], "effort": "low"})
        self.st.vault.lock()
        sent = []
        ob = outbox.Outbox.__new__(outbox.Outbox)
        ob.s = self.st
        ob.halod_state = lambda fresh=False: sent.append("asked the model") or {"state": "idle"}
        ob.tick()
        self.assertEqual(sent, [])  # nothing to send: the private item was never opened
        self.assertEqual(len(self.st.q("SELECT id FROM outbox")), 1)  # still waiting

    def outbox(self):
        """The outbox with stand-ins for the model's state and a recorder for the send thread (tick's take)."""
        import types
        import chats
        import halo_chat
        import outbox
        ob = outbox.Outbox.__new__(outbox.Outbox)
        ob.s, ob.mu, ob.running, ob.started_at = self.st, threading.Lock(), set(), 0.0
        relay = chats.Relay()
        ob.agent = types.SimpleNamespace(relay=relay)
        ob.relay = relay  # the chats' claims (outbox.Outbox takes them from its relay)
        ob.chat_agent = halo_chat.ChatAgent(self.st, relay)
        ob.engine = types.SimpleNamespace(kick=threading.Event())
        ob.pick, ob.endpoint, ob.vision = None, lambda st: "http://127.0.0.1:9/v1", lambda: False
        hs = {"state": "ready", "profile": {"id": "quick-chat", "model_id": "dsv41"}, "served_model": "dsv41", "run_id": "r1"}
        ob.halod_state = lambda fresh=False: hs
        ob.why_not = lambda st, model_id="dsv41": None
        ob.need = lambda cid, w=None: ("quick-chat", "dsv41")
        taken = []
        ob.send = lambda cid, w, st, entry=None: taken.append((cid, w, st, entry))  # the thread's work, run by the test
        return ob, relay, taken

    def test_a_lock_between_the_take_and_the_send_keeps_the_message(self):
        """Tick takes a Private chat's waiting message, Private locks before the send runs. The row stays,
        sealed, nothing is written; after the next unlock it goes into the thread once (here with the gate's refusal,
        as no Private gate is set up)."""
        import outbox
        with P.granted():
            oid = self.st.enqueue(self.cid, f"later {CANARY}", {"attachments": [], "effort": "low"})
        ob, relay, taken = self.outbox()
        ob.tick()
        self.assertEqual([t[1]["id"] for t in taken], [oid])
        self.assertEqual(self.st.q("SELECT id FROM outbox"), [{"id": oid}])  # taken, not unqueued
        self.st.vault.lock()  # the race
        outbox.Outbox.send(ob, *taken.pop())
        row = self.st.one("SELECT content FROM outbox WHERE id=?", (oid,))
        self.assertTrue(row and row["content"].startswith(P.PREFIX) and CANARY not in row["content"], row)  # still there, sealed
        self.assertEqual(len(self.st.q("SELECT id FROM messages WHERE chat_id=?", (self.cid,))), 1)  # nothing written
        self.assertFalse(relay.busy(self.cid))
        self.assertEqual(ob.running, set())
        ob.tick()
        self.assertEqual(taken, [])  # locked: never taken again, never opened
        self.st.vault.unlock(PASS)
        ob.tick()
        self.assertEqual(len(taken), 1)
        outbox.Outbox.send(ob, *taken.pop())
        self.assertEqual(self.st.q("SELECT id FROM outbox"), [])
        with P.granted():
            msgs = self.st.messages(self.cid)
        self.assertEqual([(m["role"], m["content"]) for m in msgs[1:]], [("user", f"later {CANARY}"), ("assistant", "")])
        self.assertIn("the check isn't set up", msgs[2]["meta"]["error"])
        ob.tick()
        self.assertEqual(taken, [])  # written once

    def test_a_message_that_cant_be_sent_goes_into_the_thread_whole(self):
        """Locked keeps a row only in a Private chat; any other failure, expected or not, puts the
        waiting message into the thread with the reason, its meta whole, once; nothing loops."""
        import outbox
        ob, relay, taken = self.outbox()
        meta = {"attachments": [], "effort": "low", "no_tools": True}
        self.st.enqueue(self.plain, "a question", meta)
        ob.tick()
        ob.agent.turn = ob.chat_agent.turn = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # an error nobody planned for
        outbox.Outbox.send(ob, *taken.pop())
        ms = self.st.messages(self.plain)
        self.assertEqual([(m["role"], m["content"]) for m in ms[1:]], [("user", "a question"), ("assistant", "")])
        self.assertEqual(ms[1]["meta"], meta)  # kept whole
        self.assertIn("something went wrong on this Mac (RuntimeError)", ms[2]["meta"]["error"])
        self.assertEqual(self.st.q("SELECT id FROM outbox"), [])
        ob.tick()
        self.assertEqual(taken, [])  # no loop
        # Locked in an ordinary chat (it named a Private chat's file): into the thread, not kept forever
        self.st.enqueue(self.plain, "this one", {"attachments": [], "effort": "low"})
        ob.tick()
        ob.chat_agent.turn = lambda *a, **k: (_ for _ in ()).throw(P.Locked("locked"))
        outbox.Outbox.send(ob, *taken.pop())
        self.assertIn("a file in it belongs to a Private chat", self.st.messages(self.plain)[-1]["meta"]["error"])
        self.assertEqual(self.st.q("SELECT id FROM outbox"), [])

    def test_an_unreadable_waiting_row_is_left_alone(self):
        with P.granted():
            oid = self.st.enqueue(self.cid, "later", {"attachments": [], "effort": "low"})
        self.st.x("UPDATE outbox SET meta=? WHERE id=?", ("hpv:" + base64.b64encode(b"x" * 80).decode(), oid))
        ob, relay, taken = self.outbox()
        ob.tick()
        self.assertEqual(taken, [])
        self.assertEqual(len(self.st.q("SELECT id FROM outbox")), 1)

    def test_a_message_cancelled_after_the_take_is_never_written(self):
        import outbox
        with P.granted():
            oid = self.st.enqueue(self.plain, "never mind", {"attachments": [], "effort": "low"})
        ob, relay, taken = self.outbox()
        ob.tick()
        self.assertTrue(self.st.unqueue(oid))  # the user cancels it in the thread
        with self.assertRaises(studio.OutboxGone):
            self.st.add_message(self.plain, "user", "never mind", {}, from_outbox=oid)
        outbox.Outbox.send(ob, *taken.pop())
        self.assertEqual([m["content"] for m in self.st.messages(self.plain)], ["hello"])  # nothing written
        self.assertFalse(relay.busy(self.plain))


if __name__ == "__main__":
    unittest.main()
