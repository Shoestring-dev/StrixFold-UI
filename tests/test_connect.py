"""connect (a StrixFold server that is already running, no halod): what HOST may be, what the stand-in answers and what it
makes of a server that is slow, odd, hostile, away or not the server at all, what it refuses, the model sheet, the
command's words, config.json; stub servers only, no ssh, no boxes."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import contextlib
import http.server
import io
import json
import os
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
for d in ("mac", "box"):
    sys.path.insert(0, os.path.join(ROOT, d))
import connect  # noqa: E402
import halo_chat  # noqa: E402
import halo_control as HC  # noqa: E402

HEALTH = {"status": "ok", "context_length": 63488, "max_batch_size": 4}


class Engine(http.server.BaseHTTPRequestHandler):
    """A StrixFold server's /health and /v1/models; `raw` answers with these very bytes, `hang` sends them slowly."""
    health, models, code, hcode, raw, hang, seen = dict(HEALTH), {"data": [{"id": "dsv41"}]}, 200, 200, None, 0, []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Engine.seen.append(self.path)
        if Engine.raw is not None:
            with contextlib.suppress(OSError):
                for i in range(0, len(Engine.raw), 4096):
                    self.wfile.write(Engine.raw[i:i + 4096])
            return
        if Engine.hang:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n\r\n")
            with contextlib.suppress(OSError):
                for _ in range(int(Engine.hang * 10)):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.1)
            return
        d, code = (Engine.models, Engine.code) if self.path == "/v1/models" else (Engine.health, Engine.hcode)
        body = json.dumps(d).encode()
        self.send_response(code)
        if code == 302:
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Tunnel:
    def __init__(self, state="up", error=None):
        self.state, self.error = state, error


class Base(unittest.TestCase):
    def setUp(self):
        Engine.health, Engine.models, Engine.code, Engine.hcode, Engine.raw, Engine.hang, Engine.seen = dict(HEALTH), {"data": [{"id": "dsv41"}]}, 200, 200, None, 0, []
        self.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        self.engine.daemon_threads = True
        threading.Thread(target=self.engine.serve_forever, daemon=True).start()
        self.addCleanup(self.engine.server_close)
        self.addCleanup(self.engine.shutdown)
        self.addCleanup(setattr, halo_chat, "CONNECTED", None)
        self.tunnel = Tunnel()
        self.stand = connect.Stand(dict(HC.DEFAULTS, endpoint_local_port=self.engine.server_port), "node0", self.tunnel)

    def look(self):
        self.stand.look()
        return self.stand.state()


class What(Base):
    def test_what_a_host_may_be(self):
        for text, want in (("node0", ("node0", 18080)), ("pat@10.0.0.5", ("pat@10.0.0.5", 18080)), ("box.local:18081", ("box.local", 18081))):
            self.assertEqual(connect.parse(text), want)
        for bad in ("", " ", "-oProxyCommand=x", "a b", "x;y", "$(x)", "`x`", "a@b@c", "a@", "node0:", "node0:0", "node0:99999", "node0:x", ".x", "a\nb", "x" * 300, True, None, 5, ["a"]):
            self.assertRaises(ValueError, connect.parse, bad)

    def test_a_running_server_is_a_ready_model_the_app_believes(self):
        st = self.look()
        self.assertEqual((st["state"], st["served_model"], st["profile"]["model_id"], st["settings"]["context"], st["settings"]["streams"]), ("ready", "dsv41", "dsv41", 63488, 4))
        app = HC.App.__new__(HC.App)  # its own check of what answers on the tunnel
        app.cfg, app.pairs, app.fake, app._models_seen = self.stand.cfg, {}, False, {}
        self.assertIsNone(HC.App.model_check(app, st))
        for port in (18080, 18081, 18090, 18095):  # whatever port the server has over there, the app uses the tunnel's end
            self.stand.cfg = dict(self.stand.cfg, connect=f"node0:{port}")
            self.assertEqual(HC.App.local_endpoint(app, self.stand.state()), f"http://127.0.0.1:{self.engine.server_port}/v1")
        Engine.models = {"data": [{"id": "something-else"}]}
        app._models_seen = {}
        self.assertEqual(HC.App.model_check(app, st), "mismatch")

    def test_its_window_decides_the_size(self):
        halo_chat.CONNECTED = self.stand.window
        below = [("quick-chat-16k", "16K", True), (halo_chat.SERVER_64K, "64K", True), ("quick-chat-128k", "128K", True),
                 ("quick-chat-256k", "256K", True), ("quick-chat-512k", "512K", True)]
        for n, k, smaller in ((8192, "8K", 0), (16384, "16K", 0), (63488, "64K", 1), (131072, "128K", 2), (524288, "512K", 4), (1046510, "1M", 5)):
            Engine.health = dict(HEALTH, context_length=n)
            st = self.look()
            self.assertEqual((st["profile"]["id"], st["profile"]["settings"]["context"], halo_chat.context_of("quick-chat")), ("quick-chat", n, n))
            m = halo_chat.models(st)
            self.assertEqual([(x["profile"], x["loaded"], [(c["profile"], c["k"], c["ready"]) for c in x["contexts"]]) for x in m["models"]], [("quick-chat", True, below[:smaller] + [("quick-chat", k, True)])])
            why = halo_chat.too_long({"model": None}, 60_000, st)  # what one message may hold, in the server's own size
            self.assertEqual((why is None, k in (why or k)), (n > 16384, True), why)
        self.tunnel.state = "down"
        st = self.look()
        self.assertEqual(halo_chat.context_of("quick-chat"), 1046510)  # the last it named, while it is away
        self.assertEqual([(c["k"]) for x in halo_chat.models(st)["models"] for c in x["contexts"]], ["16K", "64K", "128K", "256K", "512K", "1M"])
        halo_chat.CONNECTED = None
        self.assertGreater(len(halo_chat.models({"state": "ready", "profile": {"model_id": "dsv41"}})["models"]), 1)  # not connected: every model

    def test_one_failed_health_never_changes_the_window(self):
        self.assertEqual(self.look()["settings"]["context"], 63488)
        halo_chat.CONNECTED = self.stand.window
        for code, health in ((503, {"status": "failed"}), (200, {"status": "warming"}), (200, {"status": "failed", "context_length": 8192}), (500, {})):
            Engine.hcode, Engine.health = code, health
            st = self.look()
            self.assertEqual((st["state"], halo_chat.context_of("quick-chat")), ("failed", 63488), (code, health))
        Engine.hcode, Engine.health = 200, {"status": "ok"}  # it stops saying its window: the one it last named
        self.assertEqual(self.look()["settings"]["context"], 63488)
        fresh = connect.Stand(self.stand.cfg, "node0", self.tunnel)  # ready, and it never said (an older build): its own default,
        fresh.look()                                                 # so a chat is compacted in time instead of refused for good
        halo_chat.CONNECTED = fresh.window
        self.assertEqual((fresh.state()["settings"]["context"], halo_chat.context_of("quick-chat")), (8192, 8192))

    def test_before_the_server_names_its_window_nothing_is_cut_to_a_smaller_one(self):
        self.tunnel.state = "starting"
        st = self.look()
        halo_chat.CONNECTED = self.stand.window
        self.assertEqual((self.stand.window(), halo_chat.context_of("quick-chat")), (None, 63488))
        self.assertIsNone(halo_chat.too_long({"model": None}, 30_000, st))
        self.assertIn("64K", halo_chat.too_long({"model": None}, 400_000, st))
        self.assertTrue([c["k"] for x in halo_chat.models(st)["models"] for c in x["contexts"]])


class Sizes(Base):
    """Every size up to the server's own window, chosen with no restart, and one line on starting the server bigger."""
    def sheet(self, n):
        Engine.health = dict(HEALTH, context_length=n)
        st = self.look()
        return st, halo_chat.models(st)

    def test_every_size_up_to_the_servers_own(self):
        halo_chat.CONNECTED = self.stand.window
        st, m = self.sheet(524288)
        sizes = m["models"][0]["contexts"]
        self.assertEqual([(c["k"], c["context"]) for c in sizes], [("16K", 16384), ("64K", 63488), ("128K", 131072), ("256K", 262144), ("512K", 524288)])
        self.assertEqual((sizes[-1]["profile"], sizes[-1]["note"]), ("quick-chat", "What your server has"))
        self.assertTrue(all(c["ready"] for c in sizes))
        self.assertEqual(m["models"][0]["more"], "For more, raise CONTEXT in my.conf on both boxes, then restart.")
        self.assertNotIn(halo_chat.SERVER_64K, m["legacy"])  # the page keeps the connect-only 64K apart from the server's
        for pid, n in (("quick-chat-16k", 16384), (halo_chat.SERVER_64K, 63488), ("quick-chat-256k", 262144), ("quick-chat", 524288), (None, 524288)):
            chat = {"model": pid}
            self.assertEqual((halo_chat.context_of(halo_chat.profile_of(chat)), halo_chat.chat_window(chat, 524288)), (n, n), pid)
            self.assertEqual(halo_chat.need(chat)[1], "dsv41")
        self.assertFalse(halo_chat.size_ready({"k": "1M", "profile": "quick-chat-1m"}, st)[0])
        self.assertIn("Choose 64K", halo_chat.too_long({"model": "quick-chat-16k"}, 100_000, st))

    def test_a_size_the_server_no_longer_holds_shows_as_its_own(self):
        halo_chat.CONNECTED = self.stand.window
        st, m = self.sheet(131072)
        self.assertEqual({k: v for k, v in m["legacy"].items() if k.startswith("quick-chat")},
                         {"quick-chat-128k": "quick-chat", "quick-chat-256k": "quick-chat", "quick-chat-512k": "quick-chat", "quick-chat-1m": "quick-chat"})
        self.assertEqual([halo_chat.shown_size(p, 131072) for p in ("quick-chat-16k", halo_chat.SERVER_64K, "quick-chat-1m")],
                         ["quick-chat-16k", halo_chat.SERVER_64K, "quick-chat"])
        self.assertEqual(halo_chat.chat_window({"model": "quick-chat-1m"}, 131072), 131072)  # it sends, in the server's window

    def test_a_blip_never_takes_the_sizes_away(self):
        halo_chat.CONNECTED = self.stand.window
        self.sheet(524288)
        Engine.hcode, Engine.health = 503, {"status": "failed"}
        st = self.look()
        self.assertEqual(st["state"], "failed")
        self.assertEqual([halo_chat.size_ready({"profile": p}, st)[0] for p in ("quick-chat-16k", "quick-chat-256k", "quick-chat-1m")], [True, True, False])

    def test_before_the_server_names_its_window_the_line_says_so(self):
        self.tunnel.state = "starting"
        st = self.look()
        halo_chat.CONNECTED = self.stand.window
        more = halo_chat.models(st)["models"][0]["more"]
        self.assertTrue(more.startswith("Your server hasn't said its window yet."), more)

    def test_a_small_server_offers_only_what_fits(self):
        halo_chat.CONNECTED = self.stand.window
        for n, want in ((8192, ["8K"]), (16384, ["16K"]), (63488, ["16K", "64K"]), (100000, ["16K", "64K", "98K"])):
            self.assertEqual([c["k"] for c in self.sheet(n)[1]["models"][0]["contexts"]], want, n)

    def test_away_from_a_server_the_64k_is_the_64k(self):
        halo_chat.CONNECTED = None
        chat = {"model": halo_chat.SERVER_64K}
        self.assertEqual((halo_chat.profile_of(chat), halo_chat.context_of(halo_chat.SERVER_64K)), ("quick-chat", 63488))
        self.assertIsNone(halo_chat.models({"state": "ready", "profile": {"model_id": "dsv41"}})["models"][0].get("more"))


class ServedLabels(Base):
    def test_what_strixfold_serves_is_labelled_as_its_context(self):
        """/health says CONTEXT in whole 2,048-token chunks less 18: a 64K server is 63,470, and reads 64K, once."""
        halo_chat.CONNECTED = self.stand.window
        for served, k in ((8174, "8K"), (16366, "16K"), (63470, "64K"), (131054, "128K"), (524270, "512K"), (1046510, "1M")):
            Engine.health = dict(HEALTH, context_length=served)
            ks = [c["k"] for c in halo_chat.models(self.look())["models"][0]["contexts"]]
            self.assertEqual((ks[-1], ks.count(k)), (k, 1), served)


class Wrong(Base):
    def test_only_the_tunnels_own_end_is_believed(self):
        for state, error, words in (("blocked", None, "in use by another program"), ("down", "Connection refused", "Connection refused"), ("starting", None, "Connecting"),
                                    ("refreshing", None, "can't reach")):
            self.tunnel.state, self.tunnel.error = state, error
            st = self.look()  # (a program on that port answers like a StrixFold server all the while)
            self.assertEqual((st["state"], st["served_model"], words in st["reason"]), ("failed", None, True), st["reason"])
        self.assertEqual(Engine.seen, [])  # and isn't even asked
        self.tunnel.state = "up"
        self.assertEqual(self.look()["state"], "ready")
        self.tunnel.state = "down"  # known at once, before the next look
        self.assertEqual((self.stand.state()["state"], self.stand.state()["run_id"]), ("failed", None))

    def test_odd_and_hostile_answers(self):
        for models in ([], {"data": 5}, {"data": None}, {"data": ["x"]}, {"data": [{"id": 7}]}, {"data": [{"id": ""}]}, {"data": []}, 5, None, "x"):
            Engine.models = models
            self.assertEqual(self.look()["state"], "failed", models)
        Engine.models = {"data": [{"id": "dsv41"}]}
        for health in ([], 5, None, "ok", {"status": ["ok"]}):
            Engine.health = health
            self.assertEqual(self.look()["state"], "failed", health)
        for n in (True, 10 ** 30, -5, 0, "63488", None, 1.5, [1]):
            Engine.health = dict(HEALTH, context_length=n, max_batch_size=n)
            fresh = connect.Stand(self.stand.cfg, "node0", self.tunnel)
            fresh.look()
            self.assertEqual((fresh.state()["settings"]["context"], fresh.state()["settings"]["streams"]), (8192, 1), n)
        Engine.health = dict(HEALTH)
        for raw in (b"", b"nonsense\r\n", b"HTTP/1.1 abc OK\r\n\r\n{}", b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n{", b"HTTP/1.1 200 OK\r\n\r\n" + b"[" * 60000,
                    b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * (1 << 20), b"HTTP/1.1 200 OK\r\n\r\n\xff\xfe", b"HTTP/1.1 200 OK\r\nContent-Length: -4\r\n\r\n{}"):
            Engine.raw = raw
            self.assertEqual(self.look()["state"], "failed", raw[:40])
        Engine.raw, Engine.code, Engine.seen = None, 302, []
        self.assertEqual((self.look()["state"], Engine.seen), ("failed", ["/v1/models"]))  # no redirect is followed
        Engine.code = 401
        self.assertIn("API key", self.look()["reason"])
        Engine.code = 200
        self.assertEqual(self.look()["state"], "ready")

    def test_a_slow_server_never_holds_the_app_up(self):
        self.assertEqual(self.look()["state"], "ready")
        Engine.hang = 30
        keep, connect.DEADLINE_S = connect.DEADLINE_S, 1
        self.addCleanup(setattr, connect, "DEADLINE_S", keep)
        t = threading.Thread(target=lambda: connect.get(self.engine.server_port, "/v1/models", 1), daemon=True)
        t0 = time.monotonic()
        t.start()
        for _ in range(50):
            self.stand.state(), self.stand.profiles(), self.stand.window()
        self.assertLess(time.monotonic() - t0, 0.5)  # what the app asks is answered from the last look
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 3)

    def test_the_deadline_covers_everything_it_may_send(self):
        def drip(conn, parts):
            with contextlib.suppress(OSError):
                conn.recv(4096)
                for p in parts:
                    conn.sendall(p)
                    time.sleep(0.05)
        for parts in ([b"HTTP/1.1 200 OK\r\n"] + [b"X: y\r\n"] * 400, [b"H"] + [b"T"] * 400, [b"HTTP/1.1 100 Continue\r\n\r\n"] * 400,
                      [b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"] + [b"0"] * 400, [b"HTTP/1.1 200 OK\r\n\r\n"] + [b" "] * 400):
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                s.listen()
                threading.Thread(target=lambda: drip(s.accept()[0], parts), daemon=True).start()
                t0 = time.monotonic()
                self.assertEqual(connect.get(s.getsockname()[1], "/v1/models", 1), (None, None), parts[0])
                self.assertLess(time.monotonic() - t0, 2.5, parts[0])

    def test_a_look_that_is_old_no_longer_counts(self):
        self.assertEqual(self.look()["state"], "ready")
        self.stand.at -= connect.STALE_S + 1
        self.assertEqual((self.stand.state()["state"], "stopped answering" in self.stand.state()["reason"]), ("failed", True))

    def test_nothing_there(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.stand.port = s.getsockname()[1]  # nothing listens
            st = self.look()
        self.assertEqual((st["state"], "first_light.sh up" in st["reason"]), ("failed", True))


class Served(Base):
    def setUp(self):
        super().setUp()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), connect.Handler)
        self.srv.daemon_threads, self.srv.stand = True, self.stand
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def ask(self, path, body=None, host="127.0.0.1", method=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.server_port}{path}", body, {"Host": host}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_what_only_halod_can_do_is_refused_in_words(self):
        self.stand.look()
        self.assertEqual(self.ask("/api/state")[1]["state"], "ready")
        for path in ("/api/start", "/api/stop", "/api/restart", "/api/hold", "/api/profiles", "/api/images/start"):
            code, d = self.ask(path, b"{}")
            self.assertEqual((code, "first_light.sh" in d["error"]), (409, True), path)
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.ask("/api/profiles", b"{}", method=method)[0], 409)
        for path in ("/api/ping", "/api/profiles", "/api/requests?limit=4", "/api/events", "/api/locks", "/api/metrics", "/api/logs"):
            self.assertEqual(self.ask(path)[0], 200, path)
        self.assertEqual(self.ask("/api/conf")[0], 404)
        for host in ("evil.example", "127.0.0.1.evil.example"):  # a page elsewhere whose name points here
            self.assertEqual((self.ask("/api/state", host=host)[0], self.ask("/api/start", b"{}", host=host)[0]), (403, 403))
        for n in (b"-5", b"abc", b"99999999999"):
            with socket.create_connection(("127.0.0.1", self.srv.server_port), timeout=5) as s:
                s.sendall(b"POST /api/start HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: " + n + b"\r\n\r\n")
                self.assertIn(b"409", s.recv(200), n)

    def test_team_keeps_its_cap_and_runs_on_the_served_model(self):
        """Team works connected: the cap set before stays, and agents use the model the server serves."""
        import chat_team
        t = chat_team.Team.__new__(chat_team.Team)

        class Store:
            def kv(self, k, d=None):
                return {"c1": 3}
        t.s = Store()
        for cfg, model in (({}, chat_team.MODEL_ID), ({"connect": "node0"}, "dsv41")):
            t.app = type("A", (), {"cfg": cfg, "halod_state": lambda self: {"state": "ready", "profile": {"model_id": "dsv41"}}})()
            self.assertEqual((t.cap("c1"), t.model_id()), (3, model))


class Routes(unittest.TestCase):
    """Team and Private through the page's own routes, with and without a connection."""
    def setUp(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("hc_test_studio_c", os.path.join(ROOT, "tests", "test_studio.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.ts = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ts)
        self.ts.Http.setUp(self)
        self.ts.Http.unlock(self)

    def req(self, *a, **k):
        return self.ts.Http.req(self, *a, **k)

    def ask(self, *a):
        code, body, _ = self.req(*a)
        return code, json.loads(body)

    def test_a_smaller_size_is_chosen_with_no_restart(self):
        cid = self.ask("POST", "/studio/api/chats", {"kind": "chat", "title": "x"})[1]["id"]
        own = halo_chat._builtin()["quick-chat"]
        st = {"connected": "node0", "state": "ready", "profile": dict(own, settings=dict(own["settings"], context=524288))}
        self.app.cfg["connect"] = "node0"
        self.app.halod_state = lambda fresh=False: st
        halo_chat.CONNECTED = lambda: 524288
        self.addCleanup(setattr, halo_chat, "CONNECTED", None)
        code, c = self.ask("PUT", f"/studio/api/chats/{cid}", {"model": "quick-chat-128k"})
        self.assertEqual(code, 200, c)
        w = self.ask("GET", f"/studio/api/chats/{cid}")[1]["window"]
        self.assertEqual((w["k"], w["chosen"], w["profile"], w["loaded"], w["loaded_profile"]),
                         ("128K", 131072, "quick-chat-128k", 131072, "quick-chat-128k"))  # served as it is: no restart offered
        self.assertEqual(self.ask("PUT", f"/studio/api/chats/{cid}", {"model": halo_chat.SERVER_64K})[0], 200)
        self.assertEqual(self.ask("GET", f"/studio/api/chats/{cid}")[1]["window"]["k"], "64K")
        code, c = self.ask("PUT", f"/studio/api/chats/{cid}", {"model": "quick-chat-1m"})
        self.assertEqual(code, 400)
        self.assertIn("your server's window", c["error"])
        self.assertEqual(self.ask("PUT", f"/studio/api/chats/{cid}", {"model": "quick-chat-512k"})[0], 200)
        st["profile"]["settings"]["context"] = 131072  # the server is started again, smaller
        halo_chat.CONNECTED = lambda: 131072
        w = self.ask("GET", f"/studio/api/chats/{cid}")[1]["window"]
        self.assertEqual((w["k"], w["profile"], w["loaded_profile"]), ("128K", "quick-chat", "quick-chat"))  # its own: no restart
        self.assertEqual(self.ask("PUT", f"/studio/api/chats/{cid}", {"model": "quick-chat-16k"})[0], 200)
        w = self.ask("GET", f"/studio/api/chats/{cid}")[1]["window"]
        self.assertEqual((w["k"], w["profile"], w["loaded_profile"]), ("16K", "quick-chat-16k", "quick-chat-16k"))

    def test_away_from_the_server_the_64k_starts_as_the_64k(self):
        self.app.halod_state = lambda fresh=False: {"state": "idle"}
        sent = []
        self.app.halod_post = lambda path, body=None, *a, **k: (sent.append((path, body)) or (True, {"ok": True}))
        code, r = self.ask("POST", "/studio/api/chat/model/start", {"profile": halo_chat.SERVER_64K})
        self.assertEqual(code, 200, r)
        self.assertEqual(sent, [("/api/start", {"profile": "quick-chat", "replace": False})])

    def test_team_and_private_say_so(self):
        cid = self.ask("POST", "/studio/api/chats", {"kind": "chat", "title": "x"})[1]["id"]
        self.assertNotIn("connected", str(self.ask("POST", "/studio/api/chats", {"kind": "chat", "private": True, "title": "x"})[1]))
        self.app.cfg["connect"] = "node0"
        self.assertEqual(self.ask("POST", "/studio/api/chats", {"kind": "chat", "private": True, "title": "x"}), (400, {"error": connect.PRIVATE}))
        if self.app.studio.get("team") is not None:  # Team works connected, on the server's streams
            self.assertEqual(self.ask("PUT", f"/studio/api/chats/{cid}/team", {"cap": 3})[0], 200)
            self.assertEqual(self.ask("PUT", f"/studio/api/chats/{cid}/team", {"cap": 0})[0], 200)
        self.assertEqual(self.ask("POST", "/studio/api/chats", {"kind": "chat", "title": "y"})[0], 200)


class Start(Base):
    def app(self, value):
        made = []

        class T:
            state, error = "starting", None

            def __init__(self, name, host, lport, rport, opts, *a, **k):
                made.append((name, host, lport, rport, opts))

            def run(self):
                pass
        app = HC.App.__new__(HC.App)
        app.cfg = dict(HC.DEFAULTS, connect=value, endpoint_local_port=self.engine.server_port)
        app.halod = f"http://127.0.0.1:{app.cfg['halod_local_port']}"  # halod's own, which something else may hold
        app.fake, app.halod_token = False, None
        app.start_runner = app.wake_watch = lambda: None
        keep, HC.Tunnel, sec = HC.Tunnel, T, HC.COUSTEAU_SECTION
        HC.COUSTEAU_SECTION = False
        try:
            app.start()
        finally:
            HC.Tunnel, HC.COUSTEAU_SECTION = keep, sec
        self.addCleanup(app.stand.server_close)
        self.addCleanup(app.stand.shutdown)
        return app, made

    def test_one_tunnel_and_no_halod(self):
        app, made = self.app("pat@box:18081")
        self.assertEqual([m[:4] for m in made], [("endpoint", "pat@box", self.engine.server_port, 18081)])
        for o in ("ControlMaster=no", "ForwardAgent=no", "StrictHostKeyChecking=yes", "BatchMode=yes"):
            self.assertIn(o, made[0][4])
        self.assertEqual(app.stand.server_address[0], "127.0.0.1")
        self.assertEqual(app.halod, f"http://127.0.0.1:{app.stand.server_address[1]}")  # the stand-in itself is asked, before anything else starts
        app.stand.stand.tunnel.state = "up"
        app.stand.stand.look()
        app._st = (0.0, None)
        self.assertEqual((HC.App.halod_state(app, fresh=True)["connected"], HC.App.halod_check(app)), ("pat@box", True))
        HC.App.fetch_halod_token(app)  # never reads a token over ssh
        self.assertEqual((app.halod_token, app.cfg["endpoint_port"]), ("connect", HC.DEFAULTS["endpoint_port"]))

    def test_a_connect_that_is_no_host_says_so_and_starts(self):
        for bad in (True, "-oProxyCommand=x", {"a": 1}, "a b"):
            app, made = self.app(bad)
            self.assertEqual(made, [])
            self.assertIn("isn't a host", json.dumps(app.tunnels[0].view()))
            app.stand.stand.look()
            self.assertEqual((app.stand.stand.state()["state"], "isn't a host" in app.stand.stand.state()["reason"]), ("failed", True))


class Command(unittest.TestCase):
    def run_(self, *results, port=18080):
        it = iter(results)

        def fake(argv, **k):
            self.assertEqual(argv[0], "ssh")
            for o in ("ForwardAgent=no", "ControlMaster=no") + (() if "-G" in argv else ("BatchMode=yes",)):
                self.assertIn(o, argv)
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return subprocess.CompletedProcess(argv, r[0], r[1], r[2])
        keep, connect.subprocess.run = connect.subprocess.run, fake
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                return connect.check("pat@box", port), out.getvalue()
        finally:
            connect.subprocess.run = keep

    def test_it_says_what_to_do(self):
        ok = (0, b"", b"")
        g = (0, b"user pat\nhostname 10.0.0.5\nport 22\n", b"")
        for said in (b"pat@box: Permission denied (publickey,password).\n", b"Received disconnect from 10.0.0.5 port 22:2: Too many authentication failures\n"):
            why = self.run_((255, b"", said), g)[0]
            for words in ("ssh-keygen -t ed25519 -f ~/.ssh/strixfold", "asks for a passphrase", "-o PreferredAuthentications=keyboard-interactive,password pat@10.0.0.5", "HostName 10.0.0.5",
                          "User pat", "IdentitiesOnly yes", "UseKeychain yes", "bin/halo-control connect strixfold"):
                self.assertIn(words, why)
            self.assertNotIn("Port ", why)
        why = self.run_((255, b"", b"Permission denied\n"), (0, b"user sam\nhostname 10.0.0.6\nport 2222\nproxyjump door\n", b""), port=18081)[0]  # an alias, another ssh port, another server port
        for words in ("ProxyJump door", "password -p 2222 -o ProxyJump=door sam@10.0.0.6", "HostName 10.0.0.6", "User sam", "Port 2222", "connect strixfold:18081"):
            self.assertIn(words, why)
        self.assertNotIn("ssh-keygen", self.run_((255, b"", b"ssh: Could not resolve hostname box: nodename nor servname provided\n"))[0])
        self.assertIn("can't reach", self.run_((255, b"", b"ssh: connect to host box port 22: Operation timed out\n"))[0])
        self.assertIn("can't reach", self.run_(subprocess.TimeoutExpired("ssh", 30))[0])
        self.assertIn("Log in once by hand", self.run_((255, b"", b"Host key verification failed.\n"))[0])
        for bad in ((0, b"", b""), (7, b"\n000", b"curl: (7) Failed\n"), (0, b'{"data": []}\n200', b""), (0, b'{"data": [{"id": 7}]}\n200', b""), (0, b"x\n200", b""), (0, b'{"data": 5}\n200', b""),
                    (0, b'{"data": [{"id": "dsv41"}]}\n500', b""), subprocess.TimeoutExpired("ssh", 40)):
            self.assertIn("no StrixFold server", self.run_(ok, bad)[0], bad)
        self.assertIn("API key", self.run_(ok, (22, b"{}\n401", b""))[0])
        self.assertIn("no curl", self.run_(ok, (127, b"", b"bash: curl: command not found\n"))[0])
        why, said = self.run_(ok, (0, b'{"data": [{"id": "dsv41\\u001b[31m"}]}\n200', b""))
        self.assertEqual((why, "\x1b" in said, "dsv41" in said), (None, False, True))

    def test_it_remembers_and_forgets_and_keeps_the_rest_of_the_settings(self):
        keep, HC.CONF_DIR = HC.CONF_DIR, tempfile.mkdtemp()
        p, real = os.path.join(HC.CONF_DIR, "config.json"), os.path.join(tempfile.mkdtemp(), "mine.json")
        check, connect.check = connect.check, lambda host, port: None
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        try:
            with quiet:
                self.assertEqual(connect.main(["connect", "node0"]), 0)  # no file yet
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
                os.remove(p)
                with open(real, "w") as f:
                    json.dump({"ui_port": 8711}, f)
                os.chmod(real, 0o640)
                os.symlink(real, p)
                for args, want in ((["connect", "node0"], "node0"), (["connect", "node0:18081"], "node0:18081")):
                    self.assertEqual(connect.main(args), 0)
                    with open(p) as f:
                        self.assertEqual(json.load(f), {"ui_port": 8711, "connect": want})
                self.assertEqual((os.path.islink(p), os.stat(real).st_mode & 0o777), (True, 0o640))
                connect.check = lambda host, port: "no"
                self.assertEqual((connect.main(["connect", "other"]), connect.main(["connect", "-x"]), connect.main(["connect"])), (1, 2, 2))
                with open(p) as f:
                    self.assertEqual(json.load(f)["connect"], "node0:18081")  # a check that failed changes nothing
                self.assertEqual(connect.main(["disconnect"]), 0)
                with open(p) as f:
                    self.assertEqual(json.load(f), {"ui_port": 8711})
                connect.check = lambda host, port: None
                for broken in ("{", "[]", "5"):
                    with open(real, "w") as f:
                        f.write(broken)
                    self.assertEqual((connect.main(["connect", "node0"]), connect.main(["disconnect"])), (1, 1))
                    with open(real) as f:
                        self.assertEqual(f.read(), broken)  # never overwritten
        finally:
            HC.CONF_DIR, connect.check = keep, check


if __name__ == "__main__":
    unittest.main()
