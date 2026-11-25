"""Private chats through the app's routes: Unlock with the passcode gives this page a
private token that only its JS memory holds; a request reads a Private chat only with that token, from the same device
session, while Private is unlocked; everything else answers 404 locked or shows "Private chat". Lock, the app's own lock
and a passcode change end it; the passcode check is re-hashed at 1,000,000 rounds. A turn's thread keeps the request's
grant. test_studio's Http harness (a dashboard on a free port, no models, no boxes)."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import base64
import importlib.machinery
import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import halo_chat  # noqa: E402
import private_store as P  # noqa: E402

PASS = "correct horse"  # the harness's passcode (13 characters)


class Routes(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("hc_test_studio_p", os.path.join(ROOT, "tests", "test_studio.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.ts = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ts)
        self.ts.Http.setUp(self)
        self.ts.Http.unlock(self)
        self.st = self.app.studio["store"]
        import types
        self.app.code_check = types.SimpleNamespace(view=lambda: {"clean": True, "problems": [], "line": "Running code: test, clean"})

    def req(self, *a, **k):
        return self.ts.Http.req(self, *a, **k)

    def unlock_private(self, passcode=PASS):
        code, body, _ = self.req("POST", "/studio/api/private/unlock", {"passcode": passcode})
        return code, json.loads(body)

    def test_unlock_gives_a_token_and_only_it_opens_a_private_chat(self):
        self.assertEqual(json.loads(self.req("GET", "/studio/api/private")[1]), {"set": False, "unlocked": False, "granted": False, "min": 6})
        # no token: a Private chat can't even be made
        code, body, _ = self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "title": "secret plans"})
        self.assertEqual((code, json.loads(body)), (404, {"error": "locked"}))
        code, d = self.unlock_private()
        self.assertEqual((code, d["unlocked"], d["set"]), (200, True, True))
        tok = d["token"]
        self.assertGreaterEqual(len(tok), 40)
        code, body, _ = self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "title": "secret plans"},
                                 X_Halo_Private=tok)
        self.assertEqual(code, 200, body)
        cid = json.loads(body)["id"]
        self.assertTrue(json.loads(body)["private"])
        # with the token: the chat; without it, or with a made-up one: locked
        code, body, _ = self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)
        self.assertEqual((code, json.loads(body)["title"]), (200, "secret plans"))
        for h in ({}, {"X_Halo_Private": "A" * 43}, {"X_Halo_Private": tok[:-2]}):
            code, body, _ = self.req("GET", f"/studio/api/chats/{cid}", **h)
            self.assertEqual((code, json.loads(body)), (404, {"error": "locked"}), h)
        # the list: its title only with the token
        rows = {r["id"]: r for r in json.loads(self.req("GET", "/studio/api/chats?kind=all")[1])["chats"]}
        self.assertEqual(rows[cid]["title"], "Private chat")
        rows = {r["id"]: r for r in json.loads(self.req("GET", "/studio/api/chats?kind=all", X_Halo_Private=tok)[1])["chats"]}
        self.assertEqual(rows[cid]["title"], "secret plans")
        self.assertTrue(json.loads(self.req("GET", "/studio/api/private", X_Halo_Private=tok)[1])["granted"])
        # deleting needs the token too
        self.assertEqual(self.req("DELETE", f"/studio/api/chats/{cid}")[0], 404)
        self.assertEqual(self.req("DELETE", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[0], 200)

    def test_a_private_messages_steps_open_only_with_the_token(self):
        """The chat page's light chat (studio_http.light): a step's output, fetched when the user opens it, needs the
        same grant as the chat."""
        tok = self.unlock_private()[1]["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        g = P.GRANT.set(True)  # as a turn writes it, with the request's grant
        try:
            self.st.add_message(cid, "assistant", "done", {"reasoning": "secret thoughts", "parts": [{"tool": {"id": "a", "line": "Ran: cat", "out": "secret output"}}]})
            mid = self.st.messages(cid)[-1]["id"]
        finally:
            P.GRANT.reset(g)
        light = json.loads(self.req("GET", f"/studio/api/chats/{cid}?light=1", X_Halo_Private=tok)[1])["messages"][-1]["meta"]
        self.assertEqual(light["parts"], [{"tool": {"id": "a", "line": "Ran: cat", "out_n": 13}}])
        self.assertNotIn("secret", json.dumps(light))
        code, body, _ = self.req("GET", f"/studio/api/chats/{cid}/messages/{mid}", X_Halo_Private=tok)
        self.assertEqual((code, json.loads(body)["meta"]["parts"][0]["tool"]["out"]), (200, "secret output"))
        for h in ({}, {"X_Halo_Private": "A" * 43}):
            code, body, _ = self.req("GET", f"/studio/api/chats/{cid}/messages/{mid}", **h)
            self.assertEqual((code, json.loads(body)), (404, {"error": "locked"}), h)

    def test_another_sessions_token_doesnt_open_it(self):
        code, d = self.unlock_private()
        tok = d["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        first = self.cookie
        self.cookie = None  # a second device session (another browser): the app's own unlock
        code, body, h = self.req("POST", "/studio/api/auth/unlock", {"passcode": PASS})
        self.assertEqual(code, 200, body)
        self.cookie = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertNotEqual(self.cookie, first)
        self.assertEqual(self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[0], 404)
        code, d2 = self.unlock_private()  # its own unlock, its own token
        self.assertEqual(self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=d2["token"])[0], 200)

    def test_unlock_warms_the_serving_models_check(self):
        import time
        import types
        warmed = []
        self.app.studio["private_gate"] = types.SimpleNamespace(warm=lambda *a: warmed.append(a), close=lambda: None)
        self.app.halod_state = lambda fresh=False: {"state": "ready", "profile": {"id": "quick-chat", "model_id": "dsv41"},
                                                     "served_model": "dsv41", "run_id": "r7"}
        self.assertEqual(self.unlock_private()[0], 200)
        for _ in range(50):
            if warmed:
                break
            time.sleep(0.05)
        self.assertEqual(warmed, [("dsv41", "r7", "dsv41")])

    def test_wrong_short_lock_and_studio_lock(self):
        code, d = self.unlock_private("not the passcode at all")
        self.assertEqual((code, d["error"]), (403, "That passcode is not right."))
        code, d = self.unlock_private("short")  # shorter than any passcode: just wrong
        self.assertEqual((code, d["error"]), (403, "That passcode is not right."))
        code, d = self.unlock_private()
        tok = d["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        self.assertEqual(json.loads(self.req("POST", "/studio/api/private/lock", {})[1])["unlocked"], False)
        self.assertEqual(self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[0], 404)
        tok = self.unlock_private()[1]["token"]
        self.assertEqual(self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[0], 200)
        self.req("POST", "/studio/api/auth/lock", {})  # the app's own lock locks Private too
        self.assertFalse(self.st.vault.unlocked())

    def test_the_check_is_strengthened_and_a_passcode_change_keeps_the_chats(self):
        self.assertEqual(self.st.kv("passcode")["rounds"], 200_000)
        tok = self.unlock_private()[1]["token"]
        self.assertEqual(self.st.kv("passcode")["rounds"], 1_000_000)
        self.assertTrue(self.app.studio["auth"].verify(PASS, "127.0.0.1", True))  # the app's own unlock still takes it
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "title": "kept"}, X_Halo_Private=tok)[1])["id"]
        code, body, h = self.req("POST", "/studio/api/auth/change", {"old": PASS, "new": "a much longer passphrase"})
        self.assertEqual(code, 200, body)
        self.cookie = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertFalse(self.st.vault.unlocked())  # a change locks Private
        self.assertEqual(self.unlock_private()[0], 403)  # the old passcode no longer opens it
        tok = self.unlock_private("a much longer passphrase")[1]["token"]
        self.assertEqual(json.loads(self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[1])["title"], "kept")
        self.assertEqual(self.st.kv("passcode")["rounds"], 1_000_000)

    def test_a_six_digit_passcode_opens_private(self):
        """P4: a 6-digit passcode sets Private up and opens it, and with Private set up a change to 6 digits is taken
        and keeps the chats."""
        code, body, h = self.req("POST", "/studio/api/auth/change", {"old": PASS, "new": "123456"})
        self.assertEqual(code, 200, body)
        self.cookie = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
        code, d = self.unlock_private("123456")
        self.assertEqual((code, d.get("unlocked")), (200, True), d)
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "title": "kept"},
                                  X_Halo_Private=d["token"])[1])["id"]
        code, body, h = self.req("POST", "/studio/api/auth/change", {"old": "123456", "new": "654321"})
        self.assertEqual(code, 200, body)
        self.cookie = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertEqual(self.unlock_private("123456")[0], 403)
        tok = self.unlock_private("654321")[1]["token"]
        self.assertEqual(json.loads(self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[1])["title"], "kept")
        code, body, _ = self.req("POST", "/studio/api/auth/change", {"old": "654321", "new": "12345"})
        self.assertEqual(code, 400, body)  # still 6 at least

    def test_a_private_chat_takes_the_sealed_container_and_never_full(self):
        """Container and Container + web, Full refused before anything is made; the sheet says it is
        Private; deleting it wipes its sealed folder on Toolbox, and a Private chat that never had the container sends
        Toolbox nothing."""
        import chat_box
        tok = self.unlock_private()[1]["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        bare = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        for lv in ("box", "box_web"):
            code, body, _ = self.req("PUT", f"/studio/api/chats/{cid}", {"perm": lv}, X_Halo_Private=tok)
            self.assertEqual((code, json.loads(body)["perm"]), (200, lv), body)
        for lv in ("full", "direct"):  # Direct access is refused the same way: its files would sit unsealed too
            code, body, _ = self.req("PUT", f"/studio/api/chats/{cid}", {"perm": lv, "confirm": True}, X_Halo_Private=tok)
            self.assertEqual(code, 400, body)
            self.assertIn("isn't offered in a Private chat", json.loads(body)["error"])
        self.assertEqual(self.st.one("SELECT perm FROM chats WHERE id=?", (cid,))["perm"], "box_web")
        self.assertFalse(os.path.exists(os.path.expanduser("~/HaloChats-test")) and
                         any(cid[:6] in n for n in os.listdir(os.path.expanduser("~/HaloChats-test"))))  # no Full folder made
        self.app.fake = True  # Toolbox's health from the fake app's stand-in, never the real box
        code, body, _ = self.req("GET", f"/studio/api/chat/perms?chat={cid}", X_Halo_Private=tok)
        v = json.loads(body)
        self.assertEqual((v["private"], [(r["id"], r["offered"]) for r in v["levels"]]),
                         (True, [("none", True), ("box", True), ("box_web", True), ("full", False), ("direct", False)]), v)
        self.assertEqual(self.req("PUT", f"/studio/api/chats/{cid}", {"perm": "box"})[0], 404)  # without the token: locked
        code, body, _ = self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "perm": "full", "confirm": True},
                                 X_Halo_Private=tok)
        self.assertEqual(code, 400, body)
        wiped = []
        real = chat_box.wipe
        chat_box.wipe = lambda c, fake=False, store=None: wiped.append(c)
        self.addCleanup(setattr, chat_box, "wipe", real)
        for c in (cid, bare):
            self.assertEqual(self.req("DELETE", f"/studio/api/chats/{c}", X_Halo_Private=tok)[0], 200)
        self.assertEqual(wiped, [cid])

    def test_unlock_needs_the_landed_code(self):
        import types
        self.app.code_check = types.SimpleNamespace(view=lambda: {"clean": False, "problems": ["mac/studio.py was changed"],
                                                                  "line": "Running code: x, NOT clean"})
        code, d = self.unlock_private()
        self.assertEqual((code, d["why"]), (409, "code"))
        self.assertIn("mac/studio.py was changed", d["error"])
        self.assertFalse(self.st.vault.unlocked())
        self.app.code_check = None  # not checked at start (no git): no unlock either
        code, d = self.unlock_private()
        self.assertEqual((code, d["why"]), (409, "code"))

    def test_the_page_comes_from_its_start_copy_once_private_is_set_up(self):
        """Before Private is set up the page is read from disk, as before; once a vault
        exists, only the bytes read at start are served, on the dashboard and the tailnet listener."""
        import private_code as PC
        import halo_control as HC
        self.addCleanup(setattr, PC, "UI_FILES", PC.UI_FILES)
        snap = PC.ui_snapshot(HC.UI)
        PC.UI_FILES = dict(snap, **{"halo-chat.js": b"// the start copy\n"})
        disk = open(os.path.join(HC.UI, "halo-chat.js"), "rb").read()
        self.assertFalse(self.st.vault.is_set())
        code, body, h = self.req("GET", "/ui/halo-chat.js", cookie=False)
        self.assertEqual((code, body), (200, disk))
        self.assertEqual(h["Cache-Control"], "no-cache")  # kept, but asked again on every use: the ETag below decides
        disk_tag = h["ETag"]
        self.unlock_private()  # the first Unlock sets Private up
        self.assertTrue(self.st.vault.is_set())
        code, body, h = self.req("GET", "/ui/halo-chat.js", cookie=False)
        self.assertEqual((code, body), (200, b"// the start copy\n"))
        self.assertEqual(h["Cache-Control"], "no-cache")
        # a browser holding the disk copy is not told it is current: it gets the start copy
        code, body, _ = self.req("GET", "/ui/halo-chat.js", cookie=False, If_None_Match=disk_tag)
        self.assertEqual((code, body), (200, b"// the start copy\n"))
        PC.UI_FILES["index.html"] = snap["index.html"].replace(b"</body>", b"<!-- start copy --></body>")
        self.assertIn(b"<!-- start copy -->", self.req("GET", "/", cookie=False)[1])
        self.st.vault.lock()  # locked, Private is still set up: still the start copy
        self.assertEqual(self.req("GET", "/ui/halo-chat.js", cookie=False)[1], b"// the start copy\n")

    def post_file(self, data, name, cid, tok=None, private=False):
        hs = {"Content_Type": "text/plain", "X_Filename": name, "X_Chat": cid or "", "X_Upload_For": "chat"}
        if tok:
            hs["X_Halo_Private"] = tok
        if private:
            hs["X_Private"] = "1"
        code, body, _ = self.req("POST", "/studio/api/uploads", raw=data, **hs)
        return code, json.loads(body)

    def test_a_private_chats_file_never_goes_in_another_chat(self):
        """A file attached in Private chat A and sent from ordinary chat B (the composer kept it) is
        refused, live and queued: 400, nothing stored in B, no model request. And the reverse: an ordinary chat's file
        in a Private chat's message."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        asked = []

        class Engine(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                asked.append(self.path)
                self.send_response(500)
                self.end_headers()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        tok = self.unlock_private()[1]["token"]
        a = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        uid = self.post_file(b"CANARY-SECRET-TEXT line one\n", "secret-name.txt", a, tok, private=True)[1]["id"]
        b = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "quick-chat"}, X_Halo_Private=tok)[1])["id"]
        hs = {"state": "ready", "profile": {"id": "quick-chat", "model_id": "dsv41", "settings": {"context": 16384}},
              "served_model": "dsv41", "run_id": "r1"}
        self.app.halod_state = lambda fresh=False: hs
        self.app.model_check = lambda s=None: None
        self.app.endpoint_for = None
        self.app.local_endpoint = lambda st=None: f"http://127.0.0.1:{srv.server_address[1]}/v1"
        ob = self.app.studio["outbox"]
        for why in (None, "idle"):  # the model serving (live), then not (the queue)
            ob.why_not = lambda st, mid, why=why: why
            code, body, _ = self.req("POST", f"/studio/api/chats/{b}/send", {"content": "what does this say", "mode": "send",
                                                                             "attachments": [{"id": uid}]}, X_Halo_Private=tok)
            self.assertEqual(code, 400, (why, body))
            self.assertIn("can't go in another chat", json.loads(body)["error"])
        self.assertEqual(self.st.messages(b), [])
        self.assertEqual(self.st.waiting(b), [])
        self.assertNotIn(b"secret-name", json.dumps(self.st.chat(b)).encode())
        self.assertEqual(asked, [])  # no model request
        plain = self.post_file(b"an ordinary note\n", "o.txt", b)[1]["id"]
        code, body, _ = self.req("POST", f"/studio/api/chats/{a}/send", {"content": "and this", "mode": "send",
                                                                         "attachments": [{"id": plain}]}, X_Halo_Private=tok)
        self.assertEqual(code, 400, body)
        self.assertIn("only files sent in that chat", json.loads(body)["error"])

    def test_the_macs_look_never_starts_on_a_private_photo(self):
        """W6's upload-time look (a photo described on this Mac for a text-only model) skips a Private chat's photos."""
        import mediainfo
        import private_reader as R
        import types
        old = R.READER
        R.READER = R.Reader(open(os.path.join(ROOT, "mac", "halo_extract.swift"), "rb").read(), True)
        self.addCleanup(setattr, R, "READER", old)
        self.addCleanup(R.READER.close)
        started = []
        self.app.studio["looker"] = types.SimpleNamespace(available=lambda: True, start=started.append,
                                                          state=lambda *a, **k: None, view=lambda *a, **k: None)
        tok = self.unlock_private()[1]["token"]
        priv = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        plain = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat"})[1])["id"]
        png = mediainfo.test_pattern(32, 24, 1)
        ids = {}
        for cid, extra in ((priv, {"X_Private": "1", "X_Halo_Private": tok}), (plain, {})):
            code, body, _ = self.req("POST", "/studio/api/uploads", raw=png, Content_Type="image/png", X_Filename="p.png", X_Chat=cid,
                                     X_Upload_For="chat", X_Model="quick-chat", **extra)
            self.assertEqual(code, 200, body)
            ids[cid] = json.loads(body)["id"]
        self.assertEqual(started, [ids[plain]])  # the ordinary chat's photo only


    def test_a_private_composers_file_is_never_saved_without_its_chat(self):
        """A file marked Private is refused unless its chat is a Private chat, so no upload-time
        reader ever sees it in plaintext."""
        tok = self.unlock_private()[1]["token"]
        plain = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat"})[1])["id"]
        before = (len(self.st.q("SELECT id FROM uploads")), sorted(os.listdir(self.st.path("refs"))))
        for cid in ("", plain):
            code, d = self.post_file(b"a private note\n", "n.txt", cid, tok, private=True)
            self.assertEqual(code, 400, d)
            self.assertIn("saved only with its chat", d["error"])
        self.assertEqual((len(self.st.q("SELECT id FROM uploads")), sorted(os.listdir(self.st.path("refs")))), before)
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        code, d = self.post_file(b"a private note\n", "n.txt", cid, tok, private=True)
        self.assertEqual(code, 200, d)
        self.assertEqual(sorted(set(os.listdir(self.st.path("refs"))) - set(before[1])), [f"{d['id']}.txt.hpv"])
        self.assertEqual(self.post_file(b"x\n", "n.txt", cid, None, private=True)[0], 404)  # no token: locked

    def test_a_damaged_key_reads_unreadable_and_still_deletes(self):
        tok = self.unlock_private()[1]["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        pk = self.st.one("SELECT pkey FROM chats WHERE id=?", (cid,))["pkey"]
        raw = bytearray(base64.b64decode(pk))
        raw[30] ^= 1
        self.st.x("UPDATE chats SET pkey=? WHERE id=?", (base64.b64encode(bytes(raw)).decode(), cid))
        self.st.vault.forget(cid)
        code, body, _ = self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)
        self.assertEqual(code, 409, body)
        self.assertEqual(json.loads(body)["error"], P.UNREADABLE_CHAT)
        self.assertEqual(self.req("GET", f"/studio/api/chats/{cid}")[0], 404)  # without the token: locked, nothing more
        self.assertEqual(self.req("DELETE", f"/studio/api/chats/{cid}")[0], 404)  # nor deleted
        code, body, _ = self.req("DELETE", f"/studio/api/chats/{cid}", X_Halo_Private=tok)
        self.assertEqual(code, 200, body)
        self.assertIsNone(self.st.one("SELECT id FROM chats WHERE id=?", (cid,)))

    def test_a_changed_private_file_cant_be_read(self):
        import private_reader as R
        old = R.READER
        R.READER = R.Reader(open(os.path.join(ROOT, "mac", "halo_extract.swift"), "rb").read(), True)  # what install sets up
        self.addCleanup(setattr, R, "READER", old)
        self.addCleanup(R.READER.close)
        tok = self.unlock_private()[1]["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        import mediainfo
        png = mediainfo.test_pattern(16, 9)
        code, body, _ = self.req("POST", "/studio/api/uploads", raw=png, Content_Type="image/png", X_Filename="p.png", X_Chat=cid,
                                 X_Upload_For="chat", X_Private="1", X_Halo_Private=tok)
        self.assertEqual(code, 200, body)
        uid = json.loads(body)["id"]
        self.assertEqual(self.req("GET", f"/studio/ref/{uid}", X_Halo_Private=tok)[0], 200)
        f = self.st.path("refs", f"{uid}.hpv")
        b = bytearray(open(f, "rb").read())
        b[40] ^= 1
        open(f, "wb").write(bytes(b))
        code, body, h = self.req("GET", f"/studio/ref/{uid}", X_Halo_Private=tok)
        self.assertEqual((code, json.loads(body)["why"]), (409, "unreadable"))
        self.assertIn("can't be read", json.loads(body)["error"])

    def test_compacting_a_private_chat_goes_through_its_gate(self):
        tok = self.unlock_private()[1]["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "model": "quick-chat"},
                                  X_Halo_Private=tok)[1])["id"]
        hs = {"state": "ready", "profile": {"id": "quick-chat", "model_id": "dsv41", "settings": {"context": 16384}},
              "served_model": "dsv41", "run_id": "r9"}
        self.app.halod_state = lambda fresh=False: hs
        self.app.studio["outbox"].why_not = lambda st, mid: None
        sent = []
        self.app.studio["chat_agent"].compact = lambda *a, **k: sent.append(a)
        code, body, _ = self.req("POST", f"/studio/api/chats/{cid}/compact", {}, X_Halo_Private=tok)
        self.assertEqual(code, 409, body)  # no Private gate set up in this harness: refused, never sent
        self.assertEqual(json.loads(body)["why"], "private_engine")
        self.assertEqual(sent, [])
        self.assertFalse(self.app.studio["relay"].busy(cid))

    def test_a_full_flow_leaves_the_canary_nowhere_in_plain(self):
        """Unlock, a Private chat with the canary in its title, a message (queued: no model), a text
        file, a rename, the list and a search, Lock, Unlock, delete: the canary is in no form in the store (before and
        after the delete), the app's log (its stdout and stderr) or a file the temp folders got meanwhile."""
        import contextlib
        import io
        import tempfile
        import time
        can = "quince-canary-route-91c2"
        forms = {can.encode(), can.encode("utf-16-le"), can.encode().hex().encode()}
        for pad in range(3):
            forms.add(base64.b64encode(b"\0" * pad + can.encode())[4:-4])

        def hits(root, since=None):
            out = []
            walk = [(os.path.dirname(root), [], [os.path.basename(root)])] if os.path.isfile(root) else os.walk(root)
            for dp, dn, fn in walk:
                for f in fn:
                    p = os.path.join(dp, f)
                    try:
                        st = os.stat(p)
                        if (since and st.st_mtime < since) or st.st_size > 50 << 20:
                            continue
                        data = open(p, "rb").read()
                    except OSError:
                        continue
                    out += [p for x in forms if x in data]
            return sorted(set(out))
        t0 = time.time() - 1
        log = io.StringIO()
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            tok = self.unlock_private()[1]["token"]
            cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "model": "quick-chat",
                                                                    "title": f"About {can}"}, X_Halo_Private=tok)[1])["id"]
            self.assertEqual(self.post_file(f"notes: {can}\n".encode(), "notes.txt", cid, tok, private=True)[0], 200)
            code, body, _ = self.req("POST", f"/studio/api/chats/{cid}/send", {"content": f"remember {can}", "mode": "send"},
                                     X_Halo_Private=tok)
            self.assertIn(code, (200, 202), body)
            self.req("PUT", f"/studio/api/chats/{cid}", {"title": f"Renamed {can}"}, X_Halo_Private=tok)
            self.assertIn(can, self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[1].decode())
            for path in ("/studio/api/chats?kind=all", f"/studio/api/chats?kind=all&q={can[:6]}"):
                self.assertNotIn(can, self.req("GET", path)[1].decode())
            self.req("POST", "/studio/api/private/lock", {})
            self.assertEqual(self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[0], 404)
            store = self.st.root
            self.assertEqual(hits(store), [])
            tok = self.unlock_private()[1]["token"]
            self.assertEqual(self.req("DELETE", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[0], 200)
        self.assertEqual(hits(store), [])
        self.assertNotIn(can, log.getvalue())
        new = [e.path for e in os.scandir(tempfile.gettempdir()) if e.stat(follow_symlinks=False).st_mtime >= t0]
        self.assertEqual([h for p in new for h in hits(p, since=t0)], [])  # what the temp folder got meanwhile

    def test_lock_stops_a_running_answer_and_keeps_its_words(self):
        """Lock while a Private answer streams stops it, keeps what came, sealed, and says why."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        closed = threading.Event()

        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for _ in range(600):  # a part every 50 ms for 30 s, until the Mac closes the connection
                        self.wfile.write(b'data: {"choices": [{"index": 0, "delta": {"content": "part "}}]}\n\n')
                        self.wfile.flush()
                        threading.Event().wait(0.05)
                except OSError:
                    closed.set()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        tok = self.unlock_private()[1]["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True, "model": "quick-chat"},
                                  X_Halo_Private=tok)[1])["id"]
        agent = self.app.studio["chat_agent"]
        with P.granted():
            q = halo_chat.run_detached(agent.turn(cid, f"http://127.0.0.1:{srv.server_address[1]}/v1", "dsv41", "quick-chat",
                                                  content="a long answer, please"), {"type": "first"})
        got = []
        while not any(e and e.get("type") == "delta" for e in got):
            got.append(q.get(timeout=20))
        code, body, _ = self.req("POST", "/studio/api/private/lock", {})
        self.assertEqual(code, 200, body)
        self.assertFalse(json.loads(body)["unlocked"])
        while got[-1] is not None:
            got.append(q.get(timeout=20))
        start = next(e for e in got if e and e.get("type") == "start")
        self.assertIn({"type": "error", "error": "Stopped: Private was locked.", "id": start["id"]}, got)
        self.assertTrue(closed.wait(5))  # the engine's connection was closed
        self.assertFalse(self.app.studio["relay"].busy(cid))
        tok = self.unlock_private()[1]["token"]
        msgs = json.loads(self.req("GET", f"/studio/api/chats/{cid}", X_Halo_Private=tok)[1])["messages"]
        a = [m for m in msgs if m["role"] == "assistant"][-1]
        self.assertTrue(a["content"].startswith("part part"), a["content"][:40])
        self.assertEqual(a["meta"]["error"], "Stopped: Private was locked.")
        self.assertTrue(a["meta"]["stopped"])

    def test_a_turns_thread_keeps_the_requests_grant(self):
        self.st.vault.unlock(PASS)
        with P.granted():
            cid = self.st.create_chat(kind="chat", private=True, title="threaded")["id"]

        def gen():
            yield {"title": self.st.chat(cid)["title"]}
        with P.granted():
            q = halo_chat.run_detached(gen(), {"type": "first"})
        got = []
        while True:
            ev = q.get(timeout=10)
            if ev is None:
                break
            got.append(ev)
        self.assertEqual(got, [{"type": "first"}, {"title": "threaded"}])
        q = halo_chat.run_detached(gen(), {"type": "first"})  # without the grant: the thread is locked too
        got = [q.get(timeout=10) for _ in range(3)]
        self.assertEqual(got[1]["type"], "error")


if __name__ == "__main__":
    unittest.main()
