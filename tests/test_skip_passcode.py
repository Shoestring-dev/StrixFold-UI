"""The first screen's Skip for now: no passcode, on this Mac only. It gives this browser a session as a passcode would;
every request still needs one, and the page's token and Origin; other machines, the phone and Private chats still need
a passcode, and setting one later ends every session made without it. test_studio's Http harness (a dashboard on a
free port, no models, no boxes)."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import importlib.machinery
import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import studio  # noqa: E402

KEY = "k3y-of-this-run-0123456789abcdef"


class Skip(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("hc_test_studio_skip", os.path.join(ROOT, "tests", "test_studio.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.ts = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ts)
        self.ts.Http.setUp(self)  # a fresh store: no passcode, no session
        self.auth = self.app.studio["auth"]
        self.app.open_key = KEY  # this run's key (halo_control.write_open_key): only this Mac account can read it
        import halo_control as HC
        import tempfile
        self.open_dir = tempfile.mkdtemp(prefix="open-")
        old = HC.open_dir
        self.addCleanup(setattr, HC, "open_dir", old)
        HC.open_dir = lambda: self.open_dir  # never the real cache folder

    def req(self, *a, **k):
        return self.ts.Http.req(self, *a, **k)

    def view(self):
        return json.loads(self.req("GET", "/studio/api/auth")[1])

    def skip(self, key=None, **h):
        """A page opened with bin/halo-control: this run's key as it is now (each Skip makes a new one)."""
        key = self.app.open_key if key is None else key
        code, body, hd = self.req("POST", "/studio/api/auth/skip", {"key": key}, **h)
        if code == 200:
            self.cookie = hd["Set-Cookie"].split(";")[0].split("=", 1)[1]
        return code, json.loads(body)

    def test_skip_opens_this_mac_with_a_session_and_nothing_else(self):
        self.assertEqual((self.view()["set"], self.view()["skipped"], self.view()["unlocked"]), (False, False, False))
        self.assertEqual(self.req("GET", "/studio/api/chats")[0], 401)
        self.assertEqual(self.skip()[0], 200)
        v = self.view()
        self.assertEqual((v["set"], v["skipped"], v["unlocked"]), (False, True, True))
        self.assertEqual(self.req("GET", "/studio/api/chats")[0], 200)
        # another browser has no session: it asks, as this page does, and gets its own
        self.assertEqual(self.req("GET", "/studio/api/chats", cookie=False)[0], 401)
        self.assertEqual(self.skip()[0], 200)

    def test_skip_keeps_the_write_guards(self):
        code, _ = self.skip(Origin="http://evil.example")
        self.assertEqual(code, 403)
        code, _ = self.skip(X_Halo_UI_Token="wrong")
        self.assertEqual(code, 403)
        self.assertFalse(self.auth.skipped())

    def test_a_skip_makes_a_new_key_so_a_copy_in_history_is_dead(self):
        self.assertEqual(self.skip()[0], 200)
        self.assertNotEqual(self.app.open_key, KEY)
        self.cookie = None
        self.assertEqual(self.skip(key=KEY)[0], 403)  # the old one, from a browser's history
        page = os.path.join(self.open_dir, f"open-{self.app.cfg['ui_port']}.html")
        self.assertIn(f"#k={self.app.open_key}", open(page).read())
        self.assertEqual(os.stat(page).st_mode & 0o777, 0o600)
        self.assertEqual(self.skip(key=self.app.open_key)[0], 200)

    def test_the_first_passcode_needs_the_key_too(self):
        """Before anyone has set one, another account on this Mac could set it first and lock the user out."""
        code, body, _ = self.req("POST", "/studio/api/auth/setup", {"passcode": "someone elses"})
        self.assertEqual(code, 403, body)
        self.assertIn("bin/halo-control", json.loads(body)["error"])
        self.assertFalse(self.auth.is_set())
        code, body, _ = self.req("POST", "/studio/api/auth/setup", {"passcode": "correct horse", "key": KEY})
        self.assertEqual(code, 200, body)

    def test_the_key_page_is_this_accounts_alone_and_never_follows_a_link(self):
        import halo_control as HC
        port = self.app.cfg["ui_port"]
        bait = os.path.join(self.open_dir, "bait")
        os.symlink(bait, os.path.join(self.open_dir, f"open-{port}.html.new"))
        HC.write_open_key(self.app, port)
        page = os.path.join(self.open_dir, f"open-{port}.html")
        self.assertFalse(os.path.islink(page))
        self.assertFalse(os.path.exists(bait))
        self.assertEqual(os.stat(page).st_mode & 0o777, 0o600)
        self.assertIn(self.app.open_key, open(page).read())

    def test_only_with_this_runs_key(self):
        """Another account on this Mac reaches 127.0.0.1 too, and reads the page's token: without the key, which only
        this account can read, it can't skip (and so can't get a session and run commands as the user)."""
        for key in ("", "wrong-key-0123456789abcdefghij", KEY[:-1]):
            code, d = self.skip(key=key)
            self.assertEqual(code, 403, key)
            self.assertIn("bin/halo-control", d["error"])
        self.assertFalse(self.auth.skipped())
        self.app.open_key = None  # a dashboard that made no key: nobody skips, nobody sets the first passcode
        self.assertEqual(self.skip()[0], 403)
        self.assertEqual(self.req("POST", "/studio/api/auth/setup", {"passcode": "correct horse", "key": ""})[0], 403)

    def test_only_on_this_mac_and_never_over_a_passcode(self):
        with self.assertRaises(PermissionError):
            self.auth.skip(False, True, "x", "100.64.0.9")  # the tailnet listener (the phone) and the home network
        self.assertFalse(self.auth.skipped())
        self.assertEqual(self.req("POST", "/studio/api/auth/setup", {"passcode": "correct horse", "key": KEY})[0], 200)
        code, d = self.skip()
        self.assertEqual(code, 403)
        self.assertIn("passcode is set", d["error"])
        self.assertFalse(self.auth.skipped())

    def test_private_chats_need_a_passcode(self):
        self.skip()
        code, body, _ = self.req("POST", "/studio/api/private/unlock", {"passcode": ""})
        self.assertIn(code, (400, 403))  # refused in words, before anything is unlocked
        self.assertIn("no passcode is set yet", json.loads(body)["error"])
        self.assertFalse(self.app.studio["store"].vault.unlocked())

    def test_a_passcode_set_later_ends_every_session_made_without_one(self):
        self.skip()
        first = self.cookie
        self.cookie = None
        self.skip()  # a second browser on this Mac
        second = self.cookie
        self.assertEqual(self.st_devices(), 2)
        self.cookie = first
        code, body, hd = self.req("POST", "/studio/api/auth/set", {"passcode": "correct horse"})
        self.assertEqual(code, 200, body)
        fresh = hd["Set-Cookie"].split(";")[0].split("=", 1)[1]
        v = self.view()
        self.assertEqual((v["set"], v["skipped"]), (True, False))
        for old in (first, second):
            self.cookie = old
            self.assertEqual(self.req("GET", "/studio/api/chats")[0], 401)
        self.cookie = fresh
        self.assertEqual(self.req("GET", "/studio/api/chats")[0], 200)
        self.assertEqual(self.skip()[0], 403)  # and no skipping past it now
        self.assertTrue(self.auth.verify("correct horse", "127.0.0.1", True))

    def test_set_needs_a_session(self):
        self.assertEqual(self.req("POST", "/studio/api/auth/set", {"passcode": "correct horse"}, cookie=False)[0], 401)
        self.assertFalse(self.auth.is_set())

    def test_after_a_skip_no_other_page_can_set_the_first_passcode(self):
        """Or it could lock the user out: every session ends when one is set."""
        self.skip()
        mine = self.cookie
        code, body, _ = self.req("POST", "/studio/api/auth/setup", {"passcode": "someone elses"}, cookie=False)
        self.assertEqual(code, 403, body)
        self.assertFalse(self.auth.is_set())
        self.cookie = mine
        self.assertEqual(self.req("GET", "/studio/api/chats")[0], 200)

    def test_the_view_says_skipped_on_this_mac_only(self):
        import halo_control as HC
        self.skip()
        old = HC.Handler.channel
        self.addCleanup(setattr, HC.Handler, "channel", old)
        HC.Handler.channel = lambda h: "tailnet"
        self.assertFalse(self.view()["skipped"])

    def test_a_first_passcode_ends_every_session_before_it(self):
        token, _ = self.auth.new_session(True, "a stray one", "127.0.0.1")
        self.auth.setup("correct horse", True)
        self.assertIsNone(self.auth.device(token))

    def st_devices(self):
        return self.app.studio["store"].one("SELECT COUNT(*) AS n FROM devices")["n"]


class Page(unittest.TestCase):
    def test_the_first_screen_offers_skip_with_its_warning_on_this_mac_only(self):
        html = open(os.path.join(ROOT, "mac", "ui", "index.html")).read()
        js = open(os.path.join(ROOT, "mac", "ui", "studio.js")).read()
        self.assertIn('id="s-lock-skip"', html)
        self.assertIn("anything running as your Mac account can open your chats; other accounts on this Mac can't", html)
        self.assertIn('$("s-lock-skip-w").hidden = !(setup && a.local && !a.https_only)', js)
        self.assertIn('"/studio/api/auth/skip", { key }', js)
        self.assertIn("showLock.tried", js)  # once per page, never round and round
        theme = open(os.path.join(ROOT, "mac", "ui", "theme.js")).read()
        self.assertIn('sessionStorage.setItem("halo.open-key"', theme)
        self.assertIn('history.replaceState(null, "", location.pathname + location.search + "#/talk")', theme)
        self.assertIn('"/studio/api/auth/set"', js)
        self.assertTrue(studio.Auth.skipped)


if __name__ == "__main__":
    unittest.main()
