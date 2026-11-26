"""The private store (mac/studio.py) and its routes (mac/studio_http.py): the store's modes, the passcode and devices,
the routes' guards, the chats, dictation and the tailnet page."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import gzip
import json
import os
import math
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "box", "fake"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mediainfo  # noqa: E402
from test_halod import free_port, kill_worlds  # noqa: E402

import chats  # noqa: E402
import halo_chat  # noqa: E402
import halo_control as HC  # noqa: E402
import studio  # noqa: E402
import studio_http  # noqa: E402
import studio_transcribe  # noqa: E402


FIX = os.path.join(ROOT, "box", "fake", "fixtures")
CANARY = "zebra-canary-7f3a"


# ---------------------------------------------------------------- the Mac side
class Store(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(tempfile.mkdtemp(prefix="hs-store-"), "StrixFoldUI-test")
        self.s = studio.Store(self.root)

    def test_the_chat_rev_follows_what_the_page_shows(self):
        """An open page reloads a chat when its rev changes: a new message, a reply growing, a new version, a waiting
        message."""
        s = self.s
        c = s.create_chat()["id"]
        other = s.create_chat()["id"]
        revs = [s.chat_rev(c)]
        lst = [s.list_rev()]

        def changed(what):
            r = s.chat_rev(c)
            self.assertNotEqual(r, revs[-1], what)
            revs.append(r)
        u = s.add_message(c, "user", "a fox")
        changed("a message")
        a = s.add_message(c, "assistant", "")
        changed("a reply")
        s.set_message(a, content="Making it")
        changed("the reply grows")
        s.new_version(c, u)
        changed("a new version")
        oid = s.enqueue(c, "later", {})
        changed("a waiting message")
        s.unqueue(oid)
        changed("the waiting message went")
        s.add_message(other, "user", "elsewhere")
        self.assertEqual(s.chat_rev(c), revs[-1])  # another chat's change is not this one's
        self.assertNotEqual(s.list_rev(), lst[0])
        self.assertIsNone(s.chat_rev("nope"))

    def modes(self):
        out = {}
        for d, dirs, files in os.walk(self.root):
            out[d] = stat.S_IMODE(os.stat(d).st_mode)
            for f in files:
                out[os.path.join(d, f)] = stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode)
        return out

    def test_private_modes(self):
        u = self.s.add_upload(mediainfo.test_pattern(64, 36), "a.png")
        os.chmod(self.root, 0o755)  # a copy or a restore loosened it: the next start puts it back
        studio.Store(self.root)
        for p, m in self.modes().items():
            self.assertEqual(m, 0o700 if os.path.isdir(p) else 0o600, p)
        self.assertEqual(u["kind"], "image")


    def test_uploads_are_checked_by_their_bytes(self):
        with self.assertRaises(ValueError):
            self.s.add_upload(b"#!/bin/sh\necho hi\n", "x.png")
        meta = mediainfo.test_pattern(16, 16)
        # a PNG with a text chunk loses it on the way in
        import struct
        import zlib
        chunk = b"tEXt" + b"prompt\x00" + CANARY.encode()
        with_text = meta[:33] + struct.pack(">I", len(chunk) - 4) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF) + meta[33:]
        self.assertEqual(mediainfo.info(with_text)["text_chunks"], 1)
        u = self.s.add_upload(with_text, "t.png")
        data = open(self.s.path("refs", f"{u['id']}.png"), "rb").read()
        self.assertNotIn(CANARY.encode(), data)
        v = self.s.add_upload(open(os.path.join(FIX, "pattern-9x16.mp4"), "rb").read(), "clip.mp4")
        self.assertEqual((v["kind"], v["width"], v["height"]), ("video", 352, 640))

    def test_uploads_lose_identifying_metadata(self):
        import struct
        sp = tempfile.mkdtemp(prefix="hs-meta-")
        open(os.path.join(sp, "p.png"), "wb").write(mediainfo.test_pattern(320, 180, 1))
        subprocess.run(["sips", "-s", "format", "jpeg", os.path.join(sp, "p.png"), "--out", os.path.join(sp, "p.jpg")], capture_output=True)
        subprocess.run(["sips", "-s", "format", "heic", os.path.join(sp, "p.png"), "--out", os.path.join(sp, "p.heic")], capture_output=True)
        j = open(os.path.join(sp, "p.jpg"), "rb").read()
        exif = b"Exif\x00\x00GPSLatitude 37.7749 GPSLongitude -122.4194 Make Phone"
        jj = j[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + j[2:]
        u = self.s.add_upload(jj, "IMG_0001.jpg")
        kept = open(self.s.path("refs", f"{u['id']}.jpeg"), "rb").read()
        self.assertNotIn(b"GPSLatitude", kept)
        self.assertEqual((u["kind"], u["width"]), ("image", 320))
        h = self.s.add_upload(open(os.path.join(sp, "p.heic"), "rb").read(), "IMG_0002.HEIC")
        self.assertEqual((h["type"], h["kind"], h["width"]), ("jpeg", "image", 320))  # a phone photo becomes a JPEG
        self.assertEqual([f for f in os.listdir(self.s.path("refs")) if f.endswith((".in", ".jpg")) and f.startswith("t")], [])
        v = open(os.path.join(FIX, "pattern-16x9.mp4"), "rb").read()
        mv = [b for b in mediainfo.boxes(v) if b[0] == b"moov"][0]
        inner = b"\xa9xyz+37.7749-122.4194/"
        inner = struct.pack(">I", 4 + len(inner)) + inner
        ub = struct.pack(">I", 8 + len(inner)) + b"udta" + inner
        moov = v[mv[1] - 8:mv[2]]
        v2 = v[:mv[1] - 8] + struct.pack(">I", len(moov) + len(ub)) + moov[4:] + ub + v[mv[2]:]
        w = self.s.add_upload(v2, "IMG_0003.MOV")
        kept = open(self.s.path("refs", f"{w['id']}.mp4"), "rb").read()
        self.assertNotIn(b"37.7749", kept)
        self.assertEqual((w["kind"], mediainfo.info(kept)["frames"], mediainfo.info(kept)["audio"]), ("video", 50, True))

    def test_a_portrait_photo_stays_upright(self):
        import struct
        sp = tempfile.mkdtemp(prefix="hs-orient-")
        open(os.path.join(sp, "p.png"), "wb").write(mediainfo.test_pattern(320, 180, 1))
        subprocess.run(["sips", "-s", "format", "jpeg", os.path.join(sp, "p.png"), "--out", os.path.join(sp, "p.jpg")], capture_output=True)
        j = open(os.path.join(sp, "p.jpg"), "rb").read()
        tiff = (b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 2) + struct.pack("<HHIHH", 0x0112, 3, 1, 6, 0)
                + struct.pack("<HHII", 0x010F, 2, 6, 38) + struct.pack("<I", 0) + b"Phone\x00GPSLatitude 37.7749")
        exif = b"Exif\x00\x00" + tiff
        jj = j[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + j[2:]
        u = self.s.add_upload(jj, "IMG_0004.jpg")
        kept = open(self.s.path("refs", f"{u['id']}.jpeg"), "rb").read()
        self.assertEqual(mediainfo.jpeg_orientation(kept), 6)  # rotate 90: kept
        self.assertNotIn(b"GPSLatitude", kept)
        self.assertNotIn(b"Phone", kept)

    def test_turn_versions(self):
        c = self.s.create_chat()
        u1 = self.s.add_message(c["id"], "user", "a fox")
        a1 = self.s.add_message(c["id"], "assistant", "first take")
        g, v = self.s.new_version(c["id"], u1)
        self.assertEqual((g, v), (u1, 1))
        self.s.add_message(c["id"], "user", "a fox", version=(g, v))
        self.s.add_message(c["id"], "assistant", "second take", version=(g, v))
        vis = [m["content"] for m in self.s.messages(c["id"], visible=True)]
        self.assertEqual(vis, ["a fox", "second take"])
        self.s.show_version(c["id"], g, 0)
        self.assertEqual([m["content"] for m in self.s.messages(c["id"], visible=True)], ["a fox", "first take"])
        g2, v2 = self.s.new_version(c["id"], u1)  # a third take from the first one: nothing is lost
        self.assertEqual((g2, v2), (g, 2))
        self.assertEqual(len(self.s.messages(c["id"])), 4)
        self.s.add_message(c["id"], "user", "now at night")
        with self.assertRaises(ValueError):  # an earlier turn's versions are fixed once the chat went on
            self.s.show_version(c["id"], g, 1)

    def test_settings_are_checked(self):
        with self.assertRaises(ValueError):
            self.s.update_settings({"initials": "four"})
        self.assertEqual(self.s.update_settings({"initials": "ab", "unknown": 1})["initials"], "AB")
        # the Mac's idle lock: 5 min by default, off or 1/5/15/30
        self.assertEqual(self.s.settings()["idle_lock_min"], 5)
        for bad in (7, True, "5", -1, None):
            with self.assertRaises(ValueError):
                self.s.update_settings({"idle_lock_min": bad})
        self.assertEqual([self.s.update_settings({"idle_lock_min": m})["idle_lock_min"] for m in (0, 15, 1)], [0, 15, 1])


class Auth(unittest.TestCase):
    def setUp(self):
        self.s = studio.Store(os.path.join(tempfile.mkdtemp(prefix="hs-auth-"), "s"))
        self.a = studio.Auth(self.s)

    def test_setup_only_on_the_mac(self):
        with self.assertRaises(PermissionError):
            self.a.setup("123456", local=False)
        with self.assertRaises(ValueError):
            self.a.setup("123", local=True)
        self.a.setup("correct horse", local=True)
        rec = self.s.kv("passcode")
        self.assertNotIn("correct horse", json.dumps(rec))
        with self.assertRaises(PermissionError):
            self.a.setup("another one", local=True)

    def test_wrong_passcodes_slow_down_then_lock_the_lan(self):
        self.a.setup("correct horse", local=True)
        for _ in range(4):
            self.assertFalse(self.a.verify("nope", "192.168.1.20", False))
        self.assertFalse(self.a.verify("nope", "192.168.1.20", False))
        with self.assertRaises(PermissionError):  # now it has to wait
            self.a.verify("correct horse", "192.168.1.20", False)
        self.assertTrue(self.a.verify("correct horse", "192.168.1.21", False))  # another device is not held up
        for i in range(20):  # 20 more from all over the LAN (the 5 above count too)
            try:
                self.a.verify("nope", f"192.168.1.{100 + i}", False)
            except PermissionError:
                break
        self.assertTrue(self.a.lan_locked)
        with self.assertRaises(PermissionError):  # past 20 an hour: the LAN is locked out until the Mac unlocks
            self.a.verify("correct horse", "192.168.1.99", False)
        self.assertTrue(self.a.verify("correct horse", "127.0.0.1", True))
        self.assertTrue(self.a.verify("correct horse", "192.168.1.99", False))

    def test_one_row_per_device(self):
        self.a.setup("correct horse", local=True)
        t1, _ = self.a.new_session(True, "Safari on iPhone", "100.64.0.5")
        t2, _ = self.a.new_session(True, "Safari on iPhone", "100.64.0.5", old_token=t1)  # unlocking again replaces
        self.a.new_session(False, "Safari on iPhone", "100.64.0.5")
        self.a.new_session(True, "Chrome on Mac", "127.0.0.1")
        devs = self.a.devices()
        self.assertEqual(sorted((d["label"], d["sessions"]) for d in devs), [("Chrome on Mac", 1), ("Safari on iPhone", 2)])
        self.assertIsNone(self.a.device(t1))
        phone = [d for d in devs if d["label"].startswith("Safari")][0]
        self.a.revoke(phone["id"])
        self.assertEqual([d["label"] for d in self.a.devices()], ["Chrome on Mac"])
        self.assertIsNone(self.a.device(t2))

    def test_sessions_expire_and_change_revokes(self):
        self.a.setup("correct horse", local=True)
        tok, did = self.a.new_session(False, "Safari on iPhone", "192.168.1.20")
        tok2, did2 = self.a.new_session(True, "Mac", "127.0.0.1")
        self.assertTrue(self.a.device(tok))
        self.s.x("UPDATE devices SET last_seen=? WHERE id=?", (time.time() - studio.SESSION_IDLE_S - 5, did))
        self.assertIsNone(self.a.device(tok))  # 12 h idle
        self.s.x("UPDATE devices SET last_seen=? WHERE id=?", (time.time() - studio.SESSION_IDLE_S - 5, did2))
        self.assertTrue(self.a.device(tok2))  # a paired device stays
        self.a.change("correct horse", "battery staple", "127.0.0.1", True)
        self.assertIsNone(self.a.device(tok2))
        self.assertIsNone(self.a.device("x" * 200))
        self.assertEqual(studio.cookie_value("a=b; halo_studio=XYZ; c=d"), "XYZ")
        self.assertEqual(studio.parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(studio.parse_range("bytes=-100", 1000), (900, 999))
        self.assertIs(studio.parse_range("bytes=2000-", 1000), False)


class Http(unittest.TestCase):
    """The routes' guards: a session on every request, LAN setup refused."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="hs-http-")
        HC.CONF_DIR = tmp
        # no live port anywhere: the endpoint tunnels and halod of the real dashboard are never reached from here
        cfg = dict(HC.DEFAULTS, ui_port=0, halod_local_port=1, endpoint_local_port=9, endpoint2_local_port=9)
        self.app = HC.App(cfg)
        self.app.open_key = "test-open-key-0123456789abcdef"  # what serve() makes (halo_control.write_open_key)
        self.app.local_endpoint = lambda st=None: "http://127.0.0.1:9/v1"
        self.app.relay = chats.Relay()
        self.app.runner = None
        st = studio.Store(os.path.join(tmp, "StrixFoldUI-test"))
        rl = chats.Relay()
        import idle_engine
        eng, ag = idle_engine.IdleEngine(), None
        ca = halo_chat.ChatAgent(st, rl)
        import outbox
        ob = outbox.Outbox(st, ag, eng, lambda fresh=False: {"state": "idle"}, lambda st_: "", lambda pid: None, chat_agent=ca,
                           verify=lambda s_: self.app.model_check(s_), relay=rl)
        self.app.studio = {"store": st, "auth": studio.Auth(st), "engine": eng, "relay": rl, "agent": ag, "outbox": ob, "chat_agent": ca}
        self.app.halod_state = lambda fresh=False: {"state": "idle"}
        HC.Handler.A = self.app
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), HC.Handler)
        self.port = self.srv.server_address[1]
        cfg["ui_port"] = self.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.host = f"127.0.0.1:{self.port}"
        self.cookie = None
        self.png = mediainfo.test_pattern(48, 27)

    def req(self, method, path, body=None, raw=None, cookie=True, **h):
        headers = {"Host": self.host}
        if method != "GET":
            headers.update({"X-Halo-UI-Token": self.app.ui_token, "Content-Type": "application/json", "Origin": f"http://{self.host}"})
        if cookie and self.cookie:
            headers["Cookie"] = f"{studio.COOKIE}={self.cookie}"
        headers.update({k.replace("_", "-"): v for k, v in h.items()})
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data, headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def unlock(self):
        code, body, h = self.req("POST", "/studio/api/auth/setup", {"passcode": "correct horse", "key": self.app.open_key})
        self.assertEqual(code, 200, body)
        self.cookie = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])


    def server(self, handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]


    def test_the_idle_lock_is_the_macs_only(self):
        """/studio/api/auth gives this Mac the idle limit (studio.js locks and blurs after it); the tailnet page (the
        phone) gets 0, so it never locks that way."""
        self.assertEqual(json.loads(self.req("GET", "/studio/api/auth")[1])["idle_lock_min"], 5)
        call = self.tailnet()
        code, _, body = call("GET", "/studio/api/auth")
        self.assertEqual((code, json.loads(body)["idle_lock_min"]), (200, 0))
        self.app.studio["store"].update_settings({"idle_lock_min": 15})
        self.assertEqual(json.loads(self.req("GET", "/studio/api/auth")[1])["idle_lock_min"], 15)
        self.assertEqual(json.loads(call("GET", "/studio/api/auth")[2])["idle_lock_min"], 0)

    def tailnet(self):
        self.unlock()  # the passcode is set on the Mac itself
        HC._ts.update(t=time.time(), name="mac.tail0000.ts.net", ready=True)
        port = self.server(HC.TailnetHandler)

        def call(method, path, body=None, raw=False, **h):
            hd = {"Host": "mac.tail0000.ts.net"}
            if method != "GET":
                hd.update({"Content-Type": "application/json", "X-Halo-UI-Token": self.app.tailnet_token, "Origin": "https://mac.tail0000.ts.net"})
            hd.update({k.replace("_", "-"): v for k, v in h.items()})
            r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", None if body is None else json.dumps(body).encode(), hd, method=method)
            try:
                with urllib.request.urlopen(r, timeout=10) as f:
                    return f.status, f.headers, f.read()
            except urllib.error.HTTPError as e:
                return e.code, e.headers, e.read()
        return call

    def test_the_tailnet_reaches_studio_only(self):
        call = self.tailnet()
        code, h, page = call("GET", "/")
        self.assertEqual(code, 200)
        self.assertNotIn(self.app.ui_token.encode(), page)  # never the dashboard's token
        self.assertIn(self.app.tailnet_token.encode(), page)
        for word in (b'id="page-models"', b'id="page-agents"', b'id="chat-list"', b"/ui/app.js", b"/ui/chat.js"):
            self.assertNotIn(word, page)
        for path in ("/local/status", "/local/chats", "/local/agents", "/api/state", "/api/profiles", "/api/images", "/ui/app.js",
                     "/ui/chat.js", "/ui/index.html", "/favicon.ico", "/ui/../halo_control.py"):
            self.assertEqual(call("GET", path)[0], 403, path)
        for path in ("/local/agents", "/api/start", "/local/chats", "/local/tunnels/restart", "/api/stop"):
            self.assertEqual(call("POST", path, {})[0], 403, path)
            self.assertEqual(call("POST", path, {}, X_Halo_UI_Token=self.app.ui_token)[0], 403, path)
        for asset in ("/ui/studio.js", "/ui/studio-shell.js", "/ui/studio.css", "/ui/md.js"):
            self.assertEqual(call("GET", asset)[0], 200, asset)
        self.assertEqual(call("GET", "/", Host=f"127.0.0.1:{self.port}")[0], 403)  # only the tailnet name
        # the dashboard's token opens nothing on the tailnet, not even the chats
        self.assertEqual(call("POST", "/studio/api/auth/unlock", {"passcode": "correct horse"}, X_Halo_UI_Token=self.app.ui_token)[0], 403)

    def test_the_home_screen_app(self):
        """the user: a private link that works on the phone and the desktop: StrixFold UI installs as an app (manifest,
        icons, Apple's tags, no service worker) on both listeners; the tailnet's static files stay a fixed short
        list."""
        call = self.tailnet()
        full = {
            "app.css", "chat.css", "studio.css", "halo-chat.css", "md.js", "studio-shell.js", "studio.js", "halo-chat.js", "mark.svg",
            "fonts/InterTight-normal.woff2", "motion.js", "halo-agents.js", "polish.css", "theme.js", "side-collapse.js",
            "manifest.webmanifest", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"}
        self.assertEqual(HC.TailnetHandler.ASSETS, full)
        code, h, body = call("GET", "/ui/manifest.webmanifest")
        self.assertEqual((code, h["Content-Type"]), (200, "application/manifest+json"))
        m = json.loads(body)
        self.assertEqual((m["name"], m["short_name"], m["start_url"], m["scope"], m["display"]), ("StrixFold UI", "StrixFold UI", "/#/talk", "/", "standalone"))
        for icon in m["icons"]:
            code, h, png = call("GET", icon["src"])
            inf = mediainfo.info(png)
            self.assertEqual((code, h["Content-Type"], f"{inf['width']}x{inf['height']}"), (200, "image/png", icon["sizes"]), icon)
        self.assertEqual(sorted(i.get("purpose", "any") for i in m["icons"]), ["any", "any", "maskable"])
        code, h, png = call("GET", "/ui/apple-touch-icon.png")
        self.assertEqual((code, mediainfo.info(png)["width"]), (200, 180))
        for path in ("/ui/app.js", "/ui/index.html", "/ui/studio-shell.html", "/ui/icon-1024.png", "/sw.js", "/apple-touch-icon.png"):
            self.assertEqual(call("GET", path)[0], 403, path)
        page = call("GET", "/")[2]
        for tag in (b'<link rel="manifest" href="/ui/manifest.webmanifest">', b'<link rel="apple-touch-icon" href="/ui/apple-touch-icon.png">',
                    b'name="apple-mobile-web-app-capable" content="yes"', b'name="apple-mobile-web-app-title" content="StrixFold UI"',
                    b"viewport-fit=cover", b'name="theme-color" content="#000000" media="(prefers-color-scheme: dark)"'):
            self.assertIn(tag, page)
        self.assertNotIn(b"serviceWorker", page)
        # the LAN dashboard links no manifest: an app added from http://<Mac>:8711 would open pages the LAN refuses
        lan = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.port}/", headers={"Host": self.host}), timeout=10).read()
        self.assertNotIn(b'rel="manifest"', lan)
        self.assertNotIn(b"apple-mobile-web-app-capable", lan)
        self.assertIn(b"viewport-fit=cover", lan)
        self.assertNotIn("serviceWorker", open(os.path.join(ROOT, "mac", "ui", "studio.js")).read())

    def test_the_tailnet_name_follows_a_rename(self):
        """A rename in Tailscale (the Mac as "halo") is followed from Tailscale's own status; config.json's
        studio_tailnet_hosts adds names to accept (only host names: anything else in it is ignored)."""
        call = self.tailnet()
        self.assertEqual(call("GET", "/", Host="halo.tail0000.ts.net")[0], 403)
        self.app.cfg["studio_tailnet_hosts"] = ["Halo.tail0000.ts.net.", "evil/host", "*.ts.net"]
        self.addCleanup(self.app.cfg.pop, "studio_tailnet_hosts", None)
        self.assertEqual(call("GET", "/", Host="halo.tail0000.ts.net")[0], 200)
        self.assertEqual(call("GET", "/", Host="halo.tail0000.ts.net:443")[0], 200)
        self.assertEqual(call("GET", "/", Host="mac.tail0000.ts.net")[0], 200)  # Tailscale's own name still
        self.assertEqual(call("GET", "/", Host="evil")[0], 403)
        self.assertEqual(call("POST", "/studio/api/auth/unlock", {"passcode": "correct horse"}, Host="halo.tail0000.ts.net",
                              Origin="https://halo.tail0000.ts.net")[0], 200)
        self.app.cfg["studio_tailnet_hosts"] = []
        HC._ts.update(t=time.time(), name="halo.tail0000.ts.net", ready=True)  # Tailscale now reports the new name
        self.assertEqual(call("GET", "/", Host="halo.tail0000.ts.net")[0], 200)
        self.assertEqual(call("GET", "/", Host="mac.tail0000.ts.net")[0], 403)

    def test_tailnet_https_only_its_name_and_a_secure_cookie(self):
        call = self.tailnet()
        self.assertEqual(call("POST", "/studio/api/auth/setup", {"passcode": "another one"})[0], 403)  # setup stays on the Mac
        self.assertEqual(call("POST", "/studio/api/auth/unlock", {"passcode": "correct horse"}, Origin="http://mac.tail0000.ts.net")[0], 403)
        code, h, _ = call("POST", "/studio/api/auth/unlock", {"passcode": "correct horse", "remember": True},
                          X_Forwarded_For="100.64.0.7", Tailscale_User_Login="owner@example.com", User_Agent="Mozilla/5.0 (iPhone) Safari/605")
        self.assertEqual(code, 200)
        for flag in ("HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(flag, h["Set-Cookie"])
        dev = [d for d in self.app.studio["auth"].devices() if d["client"] == "100.64.0.7"]
        self.assertEqual(len(dev), 1)
        self.assertIn("owner@example.com", dev[0]["label"])  # the real device, not 127.0.0.1

    def test_tailnet_upload_with_the_https_origin(self):
        """A browser on https://<tailnet name> uploads a reference (the live app is not fake: no test-port allowance)."""
        self.assertFalse(self.app.fake)
        self.unlock()
        name = "my-mac.example-tailnet.ts.net"
        HC._ts.update(t=time.time(), name=name, ready=True)
        port = self.server(HC.TailnetHandler)
        hd = {"Host": name, "Origin": f"https://{name}", "X-Halo-UI-Token": self.app.tailnet_token}

        def post(path, data, ctype, extra=None):
            r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data, dict(hd, **{"Content-Type": ctype}, **(extra or {})), method="POST")
            try:
                with urllib.request.urlopen(r, timeout=10) as f:
                    return f.status, f.headers, f.read()
            except urllib.error.HTTPError as e:
                return e.code, e.headers, e.read()
        code, h, _ = post("/studio/api/auth/unlock", json.dumps({"passcode": "correct horse"}).encode(), "application/json", {"X-Forwarded-For": "100.64.0.7"})
        self.assertEqual(code, 200)
        ck = h["Set-Cookie"].split(";")[0]
        code, _, body = post("/studio/api/uploads", self.png, "image/png", {"Cookie": ck, "X-Filename": "ref.png"})
        self.assertEqual((code, json.loads(body)["kind"]), (200, "image"))
        self.assertEqual(post("/studio/api/uploads", self.png, "image/png", {"Cookie": ck, "Origin": f"http://{name}"})[0], 403)


    def test_open_elsewhere_links_are_plain_addresses(self):
        """The "Open elsewhere" block: on the Mac, the dashboard and the app's tailnet address for the
        phone; on the tailnet page, the home-network dashboard by address and Bonjour name. Plain URLs, never a
        token."""
        self.app.cfg["lan"] = True
        HC._lan_cache.update(t=time.time(), names={"my-mac.local", "192.168.1.10"})
        self.addCleanup(HC._lan_cache.update, t=0.0, names=set())
        call = self.tailnet()
        code, body, _ = self.req("GET", "/studio/api/state")
        self.assertEqual(json.loads(body)["links"], {"here": "mac", "dashboard": f"http://127.0.0.1:{self.port}/",
                                                     "phone": "https://mac.tail0000.ts.net/#/talk"})
        for tok in (self.app.ui_token, self.app.tailnet_token):
            self.assertNotIn(tok.encode(), body)
        HC._ts.update(t=time.time(), ready=False)  # serve off: no phone address to offer
        self.assertIsNone(json.loads(self.req("GET", "/studio/api/state")[1])["links"]["phone"])
        HC._ts.update(t=time.time(), ready=True)
        code, h, ck = call("POST", "/studio/api/auth/unlock", {"passcode": "correct horse"}, X_Forwarded_For="100.64.0.7")
        self.assertEqual(code, 200)
        code, _, body = call("GET", "/studio/api/state", Cookie=h["Set-Cookie"].split(";")[0])
        self.assertEqual(json.loads(body)["links"], {"here": "tailnet", "home": [f"http://192.168.1.10:{self.port}/",
                                                                                 f"http://my-mac.local:{self.port}/"]})
        for tok in (self.app.ui_token, self.app.tailnet_token):
            self.assertNotIn(tok.encode(), body)
        code, _, page = call("GET", "/")
        self.assertIn(b'id="s-else"', page)
        self.assertNotIn(self.app.ui_token.encode(), page)

    def test_state_carries_the_open_chats_rev(self):
        self.unlock()
        c = json.loads(self.req("POST", "/studio/api/chats", {})[1])
        chat = json.loads(self.req("GET", f"/studio/api/chats/{c['id']}")[1])
        st = json.loads(self.req("GET", f"/studio/api/state?chat={c['id']}")[1])
        self.assertEqual(st["rev"]["chat"], chat["rev"])
        self.assertTrue(st["rev"]["list"])
        self.app.studio["store"].add_message(c["id"], "user", "from the other device")
        st2 = json.loads(self.req("GET", f"/studio/api/state?chat={c['id']}")[1])
        self.assertNotEqual(st2["rev"]["chat"], chat["rev"])
        self.assertNotEqual(st2["rev"]["list"], st["rev"]["list"])
        self.assertIsNone(json.loads(self.req("GET", "/studio/api/state")[1])["rev"]["chat"])

    def test_page_files_are_kept_and_asked_again(self):
        """A /ui/ file: no-cache with an ETag of its bytes, 304 for a browser that has them, gzip when asked (the same
        bytes once unzipped, its own ETag); the page itself and the API stay no-store."""
        code, body, h = self.req("GET", "/ui/halo-chat.js")
        self.assertEqual((code, h["Cache-Control"]), (200, "no-cache"))
        tag = h["ETag"]
        self.assertEqual(self.req("GET", "/ui/halo-chat.js")[2]["ETag"], tag)
        code, again, h2 = self.req("GET", "/ui/halo-chat.js", If_None_Match=tag)
        self.assertEqual((code, again, h2["ETag"]), (304, b"", tag))
        code, zipped, hz = self.req("GET", "/ui/halo-chat.js", Accept_Encoding="gzip")
        self.assertEqual((code, hz["Content-Encoding"]), (200, "gzip"))
        self.assertEqual(gzip.decompress(zipped), body)
        self.assertNotEqual(hz["ETag"], tag)
        self.assertEqual(self.req("GET", "/ui/halo-chat.js", If_None_Match=hz["ETag"], Accept_Encoding="gzip")[0], 304)
        self.assertEqual(self.req("GET", "/ui/halo-chat.js", If_None_Match='"stale"')[0], 200)
        self.assertEqual(self.req("GET", "/")[2]["Cache-Control"], "no-store")
        self.assertEqual(self.req("GET", "/studio/api/auth")[2]["Cache-Control"], "no-store")

    def test_a_chats_model_and_effort(self):
        """A chat's model is one of the chat profiles and its effort low, high or extra."""
        self.unlock()
        ch = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "qwen38-flash-next", "effort": "high"})[1])
        self.assertEqual((ch["kind"], ch["title"], ch["model"], ch["effort"]), ("chat", "New chat", "qwen38-flash-next", "high"))
        ids = lambda kind: [c["id"] for c in json.loads(self.req("GET", "/studio/api/chats" + (f"?kind={kind}" if kind else ""))[1])["chats"]]  # noqa: E731
        self.assertEqual(ids("chat"), [ch["id"]])
        self.assertEqual(self.req("PUT", f"/studio/api/chats/{ch['id']}", {"model": "agent-coding"})[0], 400)  # a coding profile: not a chat model
        self.assertEqual(self.req("PUT", f"/studio/api/chats/{ch['id']}", {"effort": "max"})[0], 400)
        self.assertEqual(self.req("PUT", f"/studio/api/chats/{ch['id']}", {"project_id": "0" * 16})[0], 404)
        c = json.loads(self.req("PUT", f"/studio/api/chats/{ch['id']}", {"model": "unc-chat", "effort": "extra"})[1])
        self.assertEqual((c["model"], c["effort"]), ("unc-chat", "extra"))

    def test_projects(self):
        self.unlock()
        self.assertEqual(self.req("POST", "/studio/api/projects", {"name": " "})[0], 400)
        p = json.loads(self.req("POST", "/studio/api/projects", {"name": "Acme", "instructions": "British English."})[1])
        ch = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "project_id": p["id"]})[1])
        d = json.loads(self.req("GET", f"/studio/api/projects/{p['id']}")[1])
        self.assertEqual((d["name"], d["instructions"], [c["id"] for c in d["chats"]]), ("Acme", "British English.", [ch["id"]]))
        self.assertEqual(json.loads(self.req("GET", "/studio/api/projects")[1])["projects"][0]["chats"], 1)
        self.assertEqual(json.loads(self.req("PUT", f"/studio/api/projects/{p['id']}", {"name": "Acme sites"})[1])["name"], "Acme sites")
        self.assertEqual(json.loads(self.req("PUT", f"/studio/api/chats/{ch['id']}", {"project_id": None})[1])["project_id"], None)
        self.req("PUT", f"/studio/api/chats/{ch['id']}", {"project_id": p["id"]})
        self.assertEqual(self.req("DELETE", f"/studio/api/projects/{p['id']}")[0], 200)
        self.assertIsNone(json.loads(self.req("GET", f"/studio/api/chats/{ch['id']}")[1])["project_id"])  # the chat stays
        self.assertEqual(self.req("GET", f"/studio/api/projects/{p['id']}")[0], 404)


    def test_a_chat_message_waits_for_its_model(self):
        """The chat's model is not the one serving: the message waits (the outbox), the chat is named from it at once."""
        self.unlock()
        self.app.halod_state = lambda fresh=False: {"state": "ready", "profile": {"id": "unc-chat", "model_id": "dsv41-unc"}}
        ch = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "quick-chat"})[1])
        code, body, _ = self.req("POST", f"/studio/api/chats/{ch['id']}/send", {"content": "What is a monad?", "effort": "high"})
        self.assertEqual(code, 202, body)
        self.assertEqual((json.loads(body)["why"], json.loads(body)["model"]), ("other", "quick-chat"))
        c = json.loads(self.req("GET", f"/studio/api/chats/{ch['id']}")[1])
        self.assertEqual((c["title"], [w["content"] for w in c["waiting"]], c["waiting"][0]["meta"]["effort"]), ("What is a monad?", ["What is a monad?"], "high"))
        self.assertEqual(self.req("POST", f"/studio/api/chats/{ch['id']}/send", {"mode": "regenerate"})[0], 409)


    def sse(self, body):
        """The data: events of a send's answer."""
        return [json.loads(x[6:]) for x in body.decode().split("\n\n") if x.startswith("data: ")]


    def test_text_files_for_chats(self):
        self.unlock()
        self.assertEqual(self.req("POST", "/studio/api/uploads", raw=b"name,score\n", Content_Type="text/csv")[0], 415)  # not a chat's: images and sound
        code, body, _ = self.req("POST", "/studio/api/uploads", raw="name,score\nSam,9\n".encode(), Content_Type="text/csv", X_Filename="scores.csv",
                                 X_Upload_For="chat")
        u = json.loads(body)
        self.assertEqual((code, u["kind"], u["name"]), (200, "text", "scores.csv"))
        self.assertEqual(self.app.studio["store"].upload_text(u["id"]), "name,score\nSam,9\n")
        # any file: what cannot be read comes as its details; Windows-1252 text is read
        code, body, _ = self.req("POST", "/studio/api/uploads", raw=b"\x00\x01\x02 binary", Content_Type="application/octet-stream",
                                 X_Upload_For="chat", X_Filename="blob.bin")
        self.assertEqual((code, json.loads(body)["detail"]), (200, "10 bytes · content not readable"))
        code, body, _ = self.req("POST", "/studio/api/uploads", raw=b"latin-1 caf\xe9 not utf-8", Content_Type="text/plain", X_Upload_For="chat")
        self.assertEqual(self.app.studio["store"].upload_text(json.loads(body)["id"]), "latin-1 caf\u00e9 not utf-8")


    def test_audio_and_video_in_a_chat_come_with_their_transcript(self):
        """Any file: an attached audio or video file stays as it is and the chat's model
        reads its transcript (the local Whisper worker; a stand-in here), its first ten minutes; a tone is no speech."""
        import array
        import math
        import wave
        tmp = tempfile.mkdtemp(prefix="hs-media-")
        fake = os.path.join(tmp, "worker.py")
        open(fake, "w").write("import json, sys, wave\nprint(json.dumps({'ready': True}), flush=True)\n"
                              "for line in sys.stdin:\n    w = wave.open(json.loads(line)['wav'], 'rb')\n"
                              "    print(json.dumps({'text': 'we ship on Friday, %d frames' % w.getnframes()}), flush=True)\n")
        T = studio_transcribe.Transcriber(self.app.studio["store"], worker_cmd=[sys.executable, fake])
        self.addCleanup(T.close)
        self.unlock()

        def speech(seconds, rate=16000):
            a = array.array("h")
            for i in range(int(rate * seconds)):
                a.append(int(9000 * math.sin(2 * math.pi * 220 * i / rate)) if (i // int(rate * 0.15)) % 2 == 0 else 40)
            p = os.path.join(tmp, f"s{seconds}.wav")
            with wave.open(p, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
                w.writeframes(a.tobytes())
            return open(p, "rb").read()
        up = lambda data, name, ctype: json.loads(self.req("POST", "/studio/api/uploads", raw=data, Content_Type=ctype, X_Upload_For="chat",  # noqa: E731
                                                           X_Filename=name)[1])
        u = up(speech(3), "memo.wav", "audio/wav")  # no transcriber yet: said so on the chip (its length is read anyway)
        self.assertEqual((u["kind"], u["detail"]), ("audio", "audio · 0:03 · not transcribed (dictation is not set up)"))
        self.app.studio["transcriber"] = T
        u = up(speech(3), "memo.wav", "audio/wav")
        self.assertEqual((u["kind"], u["detail"]), ("audio", "audio · 0:03 · 6 words"))
        self.assertIn("we ship on Friday, 48000 frames", self.app.studio["store"].upload_text(u["id"]))
        old = studio_transcribe.MEDIA_SECONDS
        studio_transcribe.MEDIA_SECONDS = 1
        self.addCleanup(setattr, studio_transcribe, "MEDIA_SECONDS", old)
        u = up(speech(3), "long.wav", "audio/wav")
        self.assertEqual(u["detail"], "audio · 0:03 · 6 words · first 0:01 transcribed")
        self.assertIn("16000 frames", self.app.studio["store"].upload_text(u["id"]))
        self.assertIn("(the transcript covers the first 0:01 of 0:03)", self.app.studio["store"].upload_text(u["id"]))
        studio_transcribe.MEDIA_SECONDS = old
        v = up(open(os.path.join(FIX, "pattern-16x9.mp4"), "rb").read(), "clip.mp4", "video/mp4")  # a steady tone: no speech
        self.assertEqual((v["kind"], v["detail"]), ("video", "video · 0:02 · no speech found"))


    def test_the_tailnet_reaches_the_chat_only_with_a_session(self):
        call = self.tailnet()
        for path in ("/studio/api/projects", "/studio/api/chat/models", "/studio/api/chats?kind=all"):
            self.assertEqual(call("GET", path)[0], 401, path)
        self.assertEqual(call("POST", "/studio/api/chat/model/start", {"profile": "quick-chat"})[0], 401)
        self.assertEqual(call("POST", "/studio/api/transcribe/warm", {})[0], 401)
        code, h, _ = call("POST", "/studio/api/auth/unlock", {"passcode": "correct horse"}, X_Forwarded_For="100.64.0.7")
        ck = h["Set-Cookie"].split(";")[0]
        for path in ("/studio/api/projects", "/studio/api/chat/models", "/studio/api/chats?kind=all"):
            self.assertEqual(call("GET", path, Cookie=ck)[0], 200, path)
        self.assertEqual(call("POST", "/studio/api/projects", {"name": "x"}, Cookie=ck, Origin="http://mac.tail0000.ts.net")[0], 403)  # https only
        code, _, page = call("GET", "/")
        self.assertIn(b"/ui/halo-chat.js", page)
        self.assertNotIn(self.app.ui_token.encode(), page)
        self.assertEqual(call("GET", "/ui/halo-chat.js")[0], 200)
        self.assertEqual(call("GET", "/ui/chat.js")[0], 403)  # the dashboard's own chat stays off the tailnet

    def test_dictation(self):
        """The microphone's recording becomes text on this Mac (a fake worker stands in for mlx-whisper): behind the
        session and the token, audio only, two minutes at most, and the audio is gone from the store afterwards."""
        import wave
        tmp = tempfile.mkdtemp(prefix="hs-mic-")
        fake = os.path.join(tmp, "worker.py")
        open(fake, "w").write("import json, os, sys, wave\nprint(json.dumps({'ready': True}), flush=True)\n"
                              "for line in sys.stdin:\n    p = json.loads(line)['wav']\n"
                              "    w = wave.open(p, 'rb')\n"
                              "    print(json.dumps({'text': 'heard %d frames at %d Hz' % (w.getnframes(), w.getframerate())}), flush=True)\n")
        st = self.app.studio["store"]
        T = studio_transcribe.Transcriber(st, worker_cmd=[sys.executable, fake])
        self.addCleanup(T.close)

        def wav(seconds, rate=48000):
            """A speech-like stereo clip: a tone in 150 ms bursts with pauses between (syllables over a floor)."""
            import array
            import math
            p = os.path.join(tmp, f"{seconds}.wav")
            a = array.array("h")
            for i in range(int(rate * seconds)):
                v = int(9000 * math.sin(2 * math.pi * 220 * i / rate)) if (i // int(rate * 0.15)) % 2 == 0 else 40
                a.extend((v, v))
            with wave.open(p, "wb") as w:
                w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
                w.writeframes(a.tobytes())
            return open(p, "rb").read()
        self.assertEqual(self.req("POST", "/studio/api/transcribe", raw=wav(2), Content_Type="audio/wav")[0], 401)  # locked
        self.unlock()
        self.assertEqual(self.req("POST", "/studio/api/transcribe", raw=wav(2), Content_Type="audio/wav")[0], 503)  # not set up
        self.app.studio["transcriber"] = T
        code, body, _ = self.req("POST", "/studio/api/transcribe", raw=wav(2), Content_Type="audio/wav")
        self.assertEqual((code, json.loads(body)["text"]), (200, "heard 32000 frames at 16000 Hz"))  # afconvert: 16 kHz mono
        self.assertEqual(self.req("POST", "/studio/api/transcribe", raw=wav(2), Content_Type="audio/wav", X_Halo_UI_Token="nope")[0], 403)
        self.assertEqual(self.req("POST", "/studio/api/transcribe", raw=b"hello", Content_Type="text/plain")[0], 415)
        code, body, _ = self.req("POST", "/studio/api/transcribe", raw=wav(125, 8000), Content_Type="audio/wav")
        self.assertEqual((code, json.loads(body)["error"]), (400, "recordings up to two minutes"))
        code, body, _ = self.req("POST", "/studio/api/transcribe", raw=b"not audio at all " * 64, Content_Type="audio/webm")
        self.assertEqual(code, 400)
        self.assertIn("cannot be read on the Mac", json.loads(body)["error"])
        # iPhone Safari's own format, AAC in MP4: afconvert writes an extensible WAV from it, read as plain 16-bit PCM
        m4a = os.path.join(tmp, "clip.m4a")
        open(os.path.join(tmp, "src.wav"), "wb").write(wav(3))
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", os.path.join(tmp, "src.wav"), m4a], check=True, capture_output=True)
        code, body, _ = self.req("POST", "/studio/api/transcribe", raw=open(m4a, "rb").read(), Content_Type="audio/mp4")
        self.assertEqual(code, 200, body)
        self.assertRegex(json.loads(body)["text"], r"^heard 4[78]\d{3} frames at 16000 Hz$")
        code, body, _ = self.req("POST", "/studio/api/transcribe", raw=b"", Content_Type="audio/mp4")
        self.assertEqual((code, json.loads(body)["error"]), (400, "nothing was recorded"))
        silent = os.path.join(tmp, "silent.wav")
        with wave.open(silent, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x00\x00" * 32000)
        code, body, _ = self.req("POST", "/studio/api/transcribe", raw=open(silent, "rb").read(), Content_Type="audio/wav")
        self.assertEqual((code, json.loads(body)["text"]), (200, ""))  # silence: no words, never what Whisper invents
        hiss = os.path.join(tmp, "hiss.wav")
        import random
        with wave.open(hiss, "wb") as w:  # steady room noise, loud enough to pass the level gate: still no words
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"".join(int(random.gauss(0, 400)).to_bytes(2, "little", signed=True) for _ in range(32000)))
        code, body, _ = self.req("POST", "/studio/api/transcribe", raw=open(hiss, "rb").read(), Content_Type="audio/wav")
        self.assertEqual((code, json.loads(body)["text"]), (200, ""))
        sys.path.insert(0, os.path.join(ROOT, "mac"))
        import transcribe_worker as TW
        self.assertEqual(TW.heard({"segments": [{"text": " Thank you.", "no_speech_prob": 0.9, "avg_logprob": -0.3}]}), "")
        self.assertEqual(TW.heard({"segments": [{"text": " Hi there.", "no_speech_prob": 0.7, "avg_logprob": -1.4},
                                                {"text": " Book the room.", "no_speech_prob": 0.02, "avg_logprob": -0.2}]}), "Book the room.")
        self.assertEqual([f for f in os.listdir(st.path("refs")) if f.startswith("a")], [])  # the audio is gone
        self.assertEqual(json.loads(self.req("POST", "/studio/api/transcribe/warm", {})[1]), {"ok": True})

    def test_a_switch_names_what_it_stops_and_never_cuts_an_answer(self):
        """Review: a switch checks everything a model change would cut (an answer being written anywhere,
        agent sessions) and refuses then; otherwise the bar can name what stops and what waits."""
        self.unlock()
        self.app.halod_state = lambda fresh=False: {"state": "ready", "run_id": "r1", "profile": {"id": "unc-chat", "model_id": "dsv41-unc"}}
        started = []
        self.app.halod_post = lambda path, body: (started.append(body) or True, {"ok": True})
        st = self.app.studio["store"]
        c = st.create_chat(kind="chat", model="unc-chat")
        st.enqueue(c["id"], "later", {})
        waits = "1 waiting message"
        chk = json.loads(self.req("GET", "/studio/api/chat/switch?profile=quick-chat")[1])
        self.assertEqual((chk["blockers"], chk["stops"], chk["want"]), ([], "DeepSeek uncensored", "DeepSeek V4.1"))
        self.assertEqual(chk["waits"], [f"{waits} for DeepSeek uncensored will wait until it is back"])
        relay = self.app.studio["relay"]
        relay.active["x"] = {"conn": None, "stop": threading.Event()}  # a chat answering
        code, body, _ = self.req("POST", "/studio/api/chat/model/start", {"profile": "quick-chat", "switch": True})
        self.assertEqual((code, json.loads(body)["blockers"], started), (409, ["1 answer is being written"], []))
        relay.active.clear()
        self.assertEqual(self.req("POST", "/studio/api/chat/model/start", {"profile": "quick-chat", "switch": True})[0], 200)

    def test_the_window_chip_and_compact_now(self):
        """The context chip's data (the chat's window: the size chosen, the loaded size, how full), a size change that
        restarts the same model under the Switch rules, and Compact now (202, then a summary in the chat; 409 when the
        chat's model is not serving)."""
        self.unlock()

        class Model(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"data": [{"id": "dsv41"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                out = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Summary: the plan is FACT-x."}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out if not b.get("stream") else b"")
        port = self.server(Model)
        st = self.app.studio["store"]
        ch = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "quick-chat"})[1])
        for i in range(8):
            st.add_message(ch["id"], "user", f"question {i} FACT-x", {})
            st.add_message(ch["id"], "assistant", f"answer {i}", {"usage": {"prompt_tokens": 900 + i, "completion_tokens": 40}} if i == 7 else {})
        self.app.halod_state = lambda fresh=False: {"state": "idle"}
        w = json.loads(self.req("GET", f"/studio/api/chats/{ch['id']}")[1])["window"]
        self.assertEqual((w["k"], w["chosen"], w["loaded"], w["used"], w["measured"]), ("64K", 63488, None, 947, True))
        code, body, _ = self.req("POST", f"/studio/api/chats/{ch['id']}/compact", {})
        self.assertEqual((code, json.loads(body)["why"]), (409, "idle"))  # the summary needs the chat's model
        # the same model loaded with the 16K window: the chip's size change is a restart
        self.app.halod_state = lambda fresh=False: {"state": "ready", "run_id": "r1", "served_model": "dsv41",
                                                    "profile": {"id": "quick-chat-16k", "model_id": "dsv41", "settings": {"context": 16384}}}
        self.app.local_endpoint = lambda st_=None: f"http://127.0.0.1:{port}/v1"
        w = json.loads(self.req("GET", f"/studio/api/chats/{ch['id']}")[1])["window"]
        self.assertEqual((w["loaded"], w["loaded_profile"], w["context"]), (16384, "quick-chat-16k", 16384))
        sw = json.loads(self.req("GET", "/studio/api/chat/switch?profile=quick-chat")[1])
        self.assertEqual((sw["restarts"], sw["stops"]), ("This restarts DeepSeek V4.1 with a 64K window, about a minute.", None))
        self.assertEqual(json.loads(self.req("PUT", f"/studio/api/chats/{ch['id']}", {"model": "quick-chat-16k"})[1])["model"], "quick-chat-16k")
        self.assertEqual(self.req("PUT", f"/studio/api/chats/{ch['id']}", {"model": "quick-chat-2m"})[0], 400)  # never free-form
        code, body, _ = self.req("PUT", f"/studio/api/chats/{ch['id']}", {"model": "quick-chat-128k"})  # proven, but no long kit here
        self.assertEqual((code, json.loads(body)["error"]), (400, "128K is not offered yet: needs a deploy with the long RoPE kit."))
        code, body, _ = self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "unc-chat-512k"})  # nor as a new chat's
        self.assertEqual((code, json.loads(body)["error"]), (400, "512K is not offered yet: needs a deploy with the long RoPE kit."))
        self.assertEqual(self.req("POST", "/studio/api/chat/model/start", {"profile": "quick-chat-512k"})[0], 400)  # no long kit here
        self.app.halod_state = lambda fresh=False: {"state": "idle", "limits": {"unc-chat-512k": 1114063}}  # the team's kit
        new = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "unc-chat-512k"})[1])
        self.assertEqual(new["model"], "unc-chat-512k")
        self.app.halod_state = lambda fresh=False: {"state": "ready", "run_id": "r1", "served_model": "dsv41",
                                                    "profile": {"id": "quick-chat-16k", "model_id": "dsv41", "settings": {"context": 16384}}}
        code, body, _ = self.req("POST", f"/studio/api/chats/{ch['id']}/compact", {})
        self.assertEqual(code, 202)
        t0 = time.time()
        while time.time() - t0 < 10 and not [m for m in st.messages(ch["id"]) if m["role"] == "summary"]:
            time.sleep(0.05)
        summ = [m for m in st.messages(ch["id"]) if m["role"] == "summary"]
        self.assertEqual((len(summ), summ[0]["content"], summ[0]["meta"]["manual"], summ[0]["meta"]["covered"]),
                         (1, "Summary: the plan is FACT-x.", True, 12))
        self.assertEqual(len(st.messages(ch["id"])), 17)  # nothing deleted
        self.assertEqual(json.loads(self.req("GET", f"/studio/api/chats/{ch['id']}")[1])["window"]["summaries"], 1)

    def test_a_compaction_is_busy_everywhere_and_stop_ends_it(self):
        """With a Compact now running, a switch saw no blockers and a send to the same chat was
        accepted. A compaction now holds the chat: the switch names it, a send is refused, the outbox
        waits; Stop ends it and nothing half-made is kept."""
        self.unlock()
        gate = threading.Event()

        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"data": [{"id": "dsv41"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                gate.wait(15)  # the summary takes its time
                out = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Summary."}}]}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass
        port = self.server(Slow)
        self.addCleanup(gate.set)
        st = self.app.studio["store"]
        ch = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "quick-chat"})[1])
        for i in range(8):
            st.add_message(ch["id"], "user", f"q{i}", {})
            st.add_message(ch["id"], "assistant", f"a{i}", {})
        self.app.halod_state = lambda fresh=False: {"state": "ready", "run_id": "r1", "served_model": "dsv41",
                                                    "profile": {"id": "quick-chat", "model_id": "dsv41", "settings": {"context": 63488}}}
        self.app.local_endpoint = lambda st_=None: f"http://127.0.0.1:{port}/v1"
        self.app.relay = chats.Relay()
        self.assertEqual(self.req("POST", f"/studio/api/chats/{ch['id']}/compact", {})[0], 202)
        relay = self.app.studio["relay"]
        t0 = time.time()
        while time.time() - t0 < 5 and not studio_http.compacting(self.app.studio, ch["id"]):
            time.sleep(0.02)
        sw = json.loads(self.req("GET", "/studio/api/chat/switch?profile=unc-chat")[1])
        self.assertEqual(sw["blockers"], ["1 conversation is being compacted"])
        code, body, _ = self.req("POST", f"/studio/api/chats/{ch['id']}/send", {"content": "hello"})
        self.assertEqual((code, json.loads(body)["error"]), (409, "this chat is being compacted; send when it has finished, or stop it"))
        self.assertEqual(self.req("POST", f"/studio/api/chats/{ch['id']}/compact", {})[0], 409)  # not twice
        self.assertTrue(json.loads(self.req("GET", f"/studio/api/chats/{ch['id']}")[1])["window"]["compacting"])
        self.assertTrue(json.loads(self.req("POST", f"/studio/api/chats/{ch['id']}/stop", {})[1])["stopped"])
        t0 = time.time()
        while time.time() - t0 < 5 and relay.busy(ch["id"]):
            time.sleep(0.02)
        self.assertFalse(relay.busy(ch["id"]))
        self.assertEqual([m for m in st.messages(ch["id"]) if m["role"] == "summary"], [])  # stopped: nothing kept
        self.assertEqual(json.loads(self.req("GET", "/studio/api/chat/switch?profile=unc-chat")[1])["blockers"], [])

    def test_two_sends_and_a_compaction_at_once_one_gets_the_chat(self):
        """A send and Compact now looked at relay.busy, asked halod (which takes a moment), and
        only then registered, so two at once both got through. The chat is claimed first now: of three at once one
        gets it and nothing is left held. Compact that loses is told it is busy (409); a send that loses to Compact is
        too, and one that loses to a send waits in the outbox as a message sent while it answers (202, 38ccad7)."""
        self.unlock()
        gate = threading.Event()

        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"data": [{"id": "dsv41"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                gate.wait(15)
                out = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Summary."}}]}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass
        port = self.server(Slow)
        self.addCleanup(gate.set)
        st = self.app.studio["store"]
        ch = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "quick-chat"})[1])
        for i in range(8):
            st.add_message(ch["id"], "user", f"q{i}", {})
            st.add_message(ch["id"], "assistant", f"a{i}", {})
        ready = {"state": "ready", "run_id": "r1", "served_model": "dsv41",
                 "profile": {"id": "quick-chat", "model_id": "dsv41", "settings": {"context": 63488}}}

        def slow_state(fresh=False):
            time.sleep(0.3)  # halod's answer takes a moment: the old gap between the check and the registration
            return ready
        self.app.halod_state = slow_state
        self.app.local_endpoint = lambda st_=None: f"http://127.0.0.1:{port}/v1"
        self.app.relay = chats.Relay()
        relay = self.app.studio["relay"]
        bar, codes = threading.Barrier(3), {}

        def go(name, path, body):
            bar.wait(5)
            codes[name] = self.req("POST", path, body)[0]
        ts = [threading.Thread(target=go, args=(n, f"/studio/api/chats/{ch['id']}/{p}", b), daemon=True)
              for n, p, b in (("one", "send", {"content": "one"}), ("two", "send", {"content": "two"}), ("compact", "compact", {}))]
        for t in ts:
            t.start()
        t0 = time.time()
        while time.time() - t0 < 10 and len(codes) < 2:  # the two that don't get the chat answer at once
            time.sleep(0.02)
        self.assertGreaterEqual(len(codes), 2, codes)
        if [codes.get("one"), codes.get("two")].count(409) < 2:  # a send got it (else Compact did: both sends are 409)
            # Compact is told it is busy; the other send waits as a message sent while it answers (38ccad7)
            self.assertEqual(codes.get("compact"), 409, codes)
            self.assertIn(202, (codes.get("one"), codes.get("two")), codes)
            self.assertEqual([w["meta"].get("steer") for w in st.waiting(ch["id"])], [True])
        gate.set()
        for t in ts:
            t.join(15)
        self.assertEqual(sorted(codes.values()), [202, 409, 409] if codes["compact"] == 202 else [200, 202, 409], codes)
        t0 = time.time()
        while time.time() - t0 < 5 and relay.busy(ch["id"]):
            time.sleep(0.02)
        self.assertFalse(relay.busy(ch["id"]))  # the winner gave it back; the losers never took it
        sent = [m["content"] for m in st.messages(ch["id"]) if m["role"] == "user" and m["content"] in ("one", "two")]
        self.assertEqual(len(sent), 1 if codes["compact"] != 202 else 0, sent)
        # a refusal after the claim (a message too big for the window) gives the chat back too
        self.app.halod_state = lambda fresh=False: ready
        self.assertEqual(self.req("POST", f"/studio/api/chats/{ch['id']}/send", {"content": "x" * 400_000})[0], 400)
        self.assertFalse(relay.busy(ch["id"]))

    def test_a_message_bigger_than_the_model_is_refused(self):
        self.unlock()
        ch = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "model": "quick-chat-16k"})[1])
        code, body, _ = self.req("POST", f"/studio/api/chats/{ch['id']}/send", {"content": "x" * 60000})
        d = json.loads(body)
        self.assertEqual((code, d["why"]), (400, "too_long"))
        self.assertIn("Choose 64K in the composer", d["error"])  # a bigger window of the same model
        self.assertEqual(json.loads(self.req("GET", f"/studio/api/chats/{ch['id']}")[1])["waiting"], [])  # not kept either


    def test_the_forwarded_address_is_the_last_hop(self):
        call = self.tailnet()
        code, _, _ = call("POST", "/studio/api/auth/unlock", {"passcode": "correct horse"}, X_Forwarded_For="203.0.113.9, 100.64.0.7")
        self.assertEqual(code, 200)
        clients = {d["client"] for d in self.app.studio["auth"].devices()}
        self.assertIn("100.64.0.7", clients)  # the hop tailscale serve added, not what the client claimed
        self.assertNotIn("203.0.113.9", clients)

    def test_tailnet_throttle_is_per_device(self):
        call = self.tailnet()
        for _ in range(5):
            call("POST", "/studio/api/auth/unlock", {"passcode": "wrong one"}, X_Forwarded_For="100.64.0.7")
        code, _, body = call("POST", "/studio/api/auth/unlock", {"passcode": "correct horse"}, X_Forwarded_For="100.64.0.7")
        self.assertEqual(code, 403)
        self.assertIn(b"try again", body)  # that phone waits
        self.assertEqual(call("POST", "/studio/api/auth/unlock", {"passcode": "correct horse"}, X_Forwarded_For="100.64.0.9")[0], 200)

    def test_page_csp_allows_media(self):
        code, body, h = self.req("GET", "/")
        self.assertIn("media-src 'self' blob:", h["Content-Security-Policy"])
        self.assertIn("img-src 'self' data: blob:", h["Content-Security-Policy"])


class Startup(unittest.TestCase):
    """The server as the user starts it: `halo_control.py serve` run as a script (tests that import the module miss
    script-level faults, as a class defined after the __main__ block did), with the store and the tailnet
    listener up; both answer, then it stops cleanly."""

    def test_the_script_starts_and_serves(self):
        tmp = tempfile.mkdtemp(prefix="hs-start-")
        ports = [free_port() for _ in range(6)]
        env = dict(os.environ, HALO_FAKE_ROOT=os.path.join(tmp, "fake"), HALO_STUDIO_ROOT=os.path.join(tmp, "StrixFoldUI-test"))
        log = open(os.path.join(tmp, "log"), "w")
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "mac", "halo_control.py"), "serve", "--fake", "--port", str(ports[0]),
                              "--endpoint-port", str(ports[1]), "--halod-port", str(ports[2]), "--qwen-port", str(ports[3]),
                              "--tailnet-port", str(ports[4])], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            ok = False
            for _ in range(100):
                if p.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{ports[0]}/studio/api/auth",
                                                                       headers={"Host": f"127.0.0.1:{ports[0]}"}), timeout=2) as r:
                        ok = json.load(r)["local"] is True
                        break
                except (urllib.error.URLError, OSError):
                    time.sleep(0.2)
            self.assertTrue(ok, open(os.path.join(tmp, "log")).read()[-1500:])
            if os.path.exists(HC.DEFAULTS["tailscale"]):  # the tailnet listener exists and refuses a foreign Host
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{ports[4]}/", headers={"Host": "evil.example"}), timeout=5)
                self.assertEqual(cm.exception.code, 403)
        finally:
            p.terminate()
            p.wait(30)
            subprocess.run(["pkill", "-f", tmp], capture_output=True)


class Pages(unittest.TestCase):
    def test_the_tailnet_page_has_what_studio_js_reaches_for(self):
        """Every element the tailnet page's scripts look up by id is on that page (or made by the script), unless the
        lookup is guarded: a missing one made every state poll throw on the phone, so waiting messages stopped
        updating there."""
        import re
        page = HC.studio_page("TOKEN")
        for f in ("studio.js", "studio-shell.js", "halo-chat.js", "halo-agents.js"):
            js = open(os.path.join(ROOT, "mac", "ui", f)).read()
            ids = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js)) | set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)', js))
            made = set(re.findall(r'\.id = "([A-Za-z0-9_-]+)"', js))
            guarded = set()
            self.assertEqual(sorted(i for i in ids - made - guarded if f'id="{i}"' not in page), [], f)


class ChatHistory(unittest.TestCase):
    def test_history_files_projects_and_trimming(self):
        s = studio.Store(os.path.join(tempfile.mkdtemp(prefix="hs-chat-"), "S"))
        ag = halo_chat.ChatAgent(s, chats.Relay())
        p = s.save_project(name="Acme", instructions="Plain words.")
        c = s.create_chat(kind="chat", project_id=p["id"])
        f = s.add_upload(b"def f():\n    return 1\n", "f.py", text_ok=True)
        img = s.add_upload(mediainfo.test_pattern(16, 16), "a.png")
        s.add_message(c["id"], "user", "explain", {"attachments": [{"id": f["id"], "kind": "text", "name": "f.py"}, {"id": img["id"], "kind": "image", "name": "a.png"}]})
        s.add_message(c["id"], "assistant", "It returns 1.", {})
        s.add_message(c["id"], "assistant", "", {"error": "the model endpoint did not answer"})  # a failed answer is left out
        msgs, dropped = ag.history(s.chat(c["id"]), 16384)
        self.assertIn('This chat belongs to the project "Acme"', msgs[0]["content"])
        self.assertIn("Plain words.", msgs[0]["content"])
        self.assertIn("```f.py\ndef f():\n    return 1\n\n```".replace("```f.py", "``` f.py"), msgs[1]["content"])
        self.assertIn("this model reads text only", msgs[1]["content"])
        self.assertEqual(([m["role"] for m in msgs], dropped), (["system", "user", "assistant"], 0))
        for i in range(40):  # far more than a 4k context holds: the oldest go, the history still starts with the user
            s.add_message(c["id"], "user", f"question {i} " + "x" * 400, {})
            s.add_message(c["id"], "assistant", f"answer {i} " + "y" * 400, {})
        msgs, dropped = ag.history(s.chat(c["id"]), 4096)
        self.assertGreater(dropped, 0)
        self.assertEqual(msgs[1]["role"], "user")
        self.assertIn("question 39", msgs[-2]["content"])
        self.assertLessEqual(sum(len(m["content"]) for m in msgs), 4096 * 3.2)


class Naming(unittest.TestCase):
    def test_never_two_chats_called_chat(self):
        """Review: the dashboard's own chat (LAN, no passcode) and StrixFold UI's private chat must not both be
        "Chat": the dashboard's is Lab chat (private, behind the passcode); the section switch says StrixFold UI."""
        page = open(os.path.join(ROOT, "mac", "ui", "index.html")).read()
        self.assertIn('id="mode-chat" class="on" aria-current="page">Lab chat<', page)
        self.assertIn('id="mode-studio">StrixFold UI<', page)
        self.assertIn("Lab chat: the dashboard's own chat, kept in the private store behind the passcode", page)
        self.assertNotIn(">Chat<", page)
        shell = HC.studio_page("TOKEN")
        self.assertNotIn("Lab chat", shell)  # the tailnet page has StrixFold UI's chat only


class SideCollapse(unittest.TestCase):
    def test_the_sidebar_folds_away_on_wide_screens_only(self):
        """The sidebar folds away and comes back. Both shells
        carry Collapse sidebar (beside search) and Show sidebar (top left of the main area), side-collapse.js loads in
        <head> so a folded page never paints the sidebar first, the choice is kept per browser (wrapped), Cmd+\\
        toggles it, phones keep their drawer (the buttons exist from 1024 px only) and reduced motion has no move.
        tools/shots/side_collapse_shots.py checks the look on a fake app."""
        import re
        js = open(os.path.join(ROOT, "mac", "ui", "side-collapse.js")).read()
        css = open(os.path.join(ROOT, "mac", "ui", "chat.css")).read()
        for page in (open(os.path.join(ROOT, "mac", "ui", "index.html")).read(), HC.studio_page("TOKEN")):
            self.assertIn('id="side-collapse" aria-label="Collapse sidebar" aria-controls="side" aria-expanded="true"', page)
            self.assertIn('id="side-show" aria-label="Show sidebar" aria-controls="side"', page)
            self.assertLess(page.index('id="side-collapse"'), page.index('id="side-close"'))  # in the sidebar's header
            self.assertLess(page.index('<header class="topbar">'), page.index('id="side-show"'))  # the main area's top left
            head = page[:page.index("</head>")]
            self.assertIn('<script src="/ui/side-collapse.js"></script>', head)  # not deferred: before the first paint
        self.assertIn("side-collapse.js", HC.TailnetHandler.ASSETS)
        self.assertIn('localStorage.getItem(KEY) === "1"', js)
        for call in re.findall(r"[^\n]*localStorage\.[a-zA-Z]+\([^\n]*", js):
            self.assertIn("try {", call)  # a private window throws: never a broken page
        self.assertIn('e.key === "\\\\" && (e.metaKey || e.ctrlKey)', js)
        self.assertIn('document.getElementById(folded ? "side-show" : "side-collapse")', js)  # focus follows the toggle
        wide = css[css.index("@media (min-width: 1024px) {\n  .side-collapse"):]
        self.assertIn(".side-collapse, .side-show { display: none; }", css)  # below 1024 px: the phone's drawer only
        self.assertIn("html.side-collapsed .app { grid-template-columns: 0px minmax(0, 1fr); }", wide)
        self.assertIn("transition: grid-template-columns .14s", wide)  # quick: under 150 ms
        self.assertIn("@media (min-width: 1024px) and (prefers-reduced-motion: reduce)", css)
        node = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)
        if node:
            r = subprocess.run([node, "--check", os.path.join(ROOT, "mac", "ui", "side-collapse.js")], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)


class SwipeZone(unittest.TestCase):
    def test_the_drawer_swipe_leaves_the_edge_to_safari(self):
        """Re-review: a touch at x=6 opened the drawer and fought Safari's back swipe. The swipe starts only
        between 24 px from the edge and 40% of the width (studio.js swipeZone, run in node)."""
        node = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)
        if not node:
            self.skipTest("node is not installed")
        js = open(os.path.join(ROOT, "mac", "ui", "studio.js")).read()
        block = js[js.index("/* swipe: pure"):js.index("/* swipe: end */")]
        r = subprocess.run([node, "-e", block + "console.log(JSON.stringify([0, 6, 23, 24, 60, 156, 157, 300].map((x) => swipeZone(x, 390))));"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(r.stdout), [False, False, False, True, True, True, False, False])
        self.assertIn("!swipeZone(t.clientX, window.innerWidth)", js)  # and the drawer's touchstart asks it


class PullToRefresh(unittest.TestCase):
    """The pull-down gesture's logic (studio.js pullStep), run in node: it fires only from the top, past the threshold,
    with one finger, not from a blocked start (composer, popover, viewer, drawer), not after a swipe or an upward scroll."""

    def test_the_gesture(self):
        node = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)
        if not node:
            self.skipTest("node is not installed")
        js = open(os.path.join(ROOT, "mac", "ui", "studio.js")).read()
        block = js[js.index("/* pull: pure"):js.index("/* pull: end */")]
        script = block + """
const run = (evs) => evs.reduce((g, e) => pullStep(g, Object.assign({ x: 100, touches: 1, top: 0 }, e)), null);
const fire = (evs) => !!run(evs.concat([{ type: "end" }])).fire;
console.log(JSON.stringify({
  past: fire([{ type: "start", y: 100 }, { type: "move", y: 120 }, { type: "move", y: 300 }]),
  short: fire([{ type: "start", y: 100 }, { type: "move", y: 180 }]),
  scrolled: fire([{ type: "start", y: 100, top: 40 }, { type: "move", y: 400 }]),
  scrolledSince: fire([{ type: "start", y: 100 }, { type: "move", y: 103, top: 12 }, { type: "move", y: 400 }]),
  blocked: fire([{ type: "start", y: 100, blocked: true }, { type: "move", y: 400 }]),
  pinch: fire([{ type: "start", y: 100 }, { type: "move", y: 200 }, { type: "move", y: 400, touches: 2 }]),
  up: fire([{ type: "start", y: 300 }, { type: "move", y: 250 }, { type: "move", y: 600 }]),
  swipe: fire([{ type: "start", y: 100 }, { type: "move", y: 104, x: 160 }, { type: "move", y: 400 }]),
  back: fire([{ type: "start", y: 100 }, { type: "move", y: 400 }, { type: "move", y: 110 }]),
  half: run([{ type: "start", y: 0 }, { type: "move", y: 46 }]).pull,
  max: run([{ type: "start", y: 0 }, { type: "move", y: 2000 }]).pull,
}));
"""
        r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"past": True, "short": False, "scrolled": False, "scrolledSince": False, "blocked": False,
                                                "pinch": False, "up": False, "swipe": False, "back": False, "half": 20, "max": 116})


def tearDownModule():
    kill_worlds()  # fake ranks and holders of every world this module made, also ones a test left behind


if __name__ == "__main__":
    unittest.main()


