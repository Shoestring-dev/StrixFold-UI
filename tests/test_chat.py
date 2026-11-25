"""Chats (mac/chats.py and the Mac server's /local/chats routes): the store, the streaming relay against a small
OpenAI-style SSE server (events, what is sent upstream, stop, edit, regenerate), and the routes' guards."""
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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import chats  # noqa: E402
import halo_control as HC  # noqa: E402
import agents  # noqa: E402



def unlocked(app, tmp):
    """Studio's session for the test client (Lab chat and agents live in the private store)."""
    import studio
    st = studio.Store(os.path.join(tmp, "StrixFoldUI-test"))
    app.studio = {"store": st, "auth": studio.Auth(st)}
    app.studio["auth"].setup("correct horse", True)
    tok, _ = app.studio["auth"].new_session(True, "test", "127.0.0.1")
    return f"{studio.COOKIE}={tok}"

class SSE:
    """Streams 'one two three ...' (N words) as OpenAI chunks, delay seconds apart, with usage at the end."""

    def __init__(self, n=6, delay=0.0, reasoning=False, status=200):
        self.bodies, self.n, self.delay, self.reasoning, self.status = [], n, delay, reasoning, status
        self.model_id = "dsv41"
        me = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):  # /v1/models: the model this server answers with (App.model_check reads it)
                out = json.dumps({"object": "list", "data": [{"id": me.model_id, "object": "model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                me.bodies.append(body)
                if me.status != 200:
                    out = json.dumps({"error": {"message": "sampling without top_k"}}).encode()
                    self.send_response(me.status)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    return self.wfile.write(out)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    if me.reasoning:
                        self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"thinking it over"}}]}\n\n')
                    for i in range(me.n):
                        d = json.dumps({"choices": [{"index": 0, "delta": {"content": f"w{i} "}}]})
                        self.wfile.write(f"data: {d}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(me.delay)
                    d = json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 12, "completion_tokens": me.n}})
                    self.wfile.write(f"data: {d}\n\ndata: [DONE]\n\n".encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"


class StoreAndRelay(unittest.TestCase):
    def setUp(self):
        self.s = chats.Store(os.path.join(tempfile.mkdtemp(), "chats.sqlite"))
        self.rl = chats.Relay()

    def run_relay(self, srv, cid, content="hello", mode="send", attachments=None):
        return list(chats.relay(self.s, self.rl, cid, srv.url, "dsv41", content, mode, attachments, "DeepSeek V4.1 Flash"))

    def test_store(self):
        c = self.s.create()
        self.assertEqual(c["title"], "New chat")
        self.s.update(c["id"], title="Plans", system="Answer briefly.", settings={"temperature": 0.7, "top_k": 30, "thinking": True})
        c = self.s.get(c["id"])
        self.assertEqual((c["title"], c["system"], c["settings"]), ("Plans", "Answer briefly.", {"temperature": 0.7, "top_k": 30, "thinking": True}))
        for bad in ({"temperature": 3}, {"top_k": 99}, {"max_tokens": 5}, {"profile": "../x"}):
            with self.assertRaises(ValueError):
                self.s.update(c["id"], settings=bad)
        with self.assertRaises(ValueError):
            self.s.update(c["id"], title="  ")
        self.s.add(c["id"], "user", "find the needle")
        self.assertEqual([x["id"] for x in self.s.list("needle")], [c["id"]])
        self.assertEqual(self.s.list("nothing here"), [])
        self.assertIn("## You\n\nfind the needle", self.s.export_md(c["id"]))
        self.s.delete(c["id"])
        with self.assertRaises(KeyError):
            self.s.get(c["id"])

    def test_stream_and_store(self):
        srv = SSE(n=5)
        c = self.s.create()
        ev = self.run_relay(srv, c["id"], "What is 2+2?", attachments=[{"name": "a.py", "text": "print(1)"}])
        kinds = [e["type"] for e in ev]
        self.assertEqual(kinds[:2], ["user", "start"])
        self.assertEqual(kinds[-1], "done")
        self.assertEqual("".join(e["text"] for e in ev if e["type"] == "delta"), "w0 w1 w2 w3 w4 ")
        b = srv.bodies[0]
        self.assertEqual((b["temperature"], b["max_tokens"], b["stream"], b["model"]), (0, 4096, True, "dsv41"))
        self.assertNotIn("top_k", b)
        self.assertIn("```", b["messages"][-1]["content"])  # the attachment goes into the message as a fence
        c = self.s.get(c["id"])
        self.assertEqual(c["title"], "What is 2+2?")  # the first message names the chat
        a = c["messages"][-1]
        self.assertEqual((a["role"], a["content"], a["meta"]["usage"]["completion_tokens"], a["meta"]["finish"]), ("assistant", "w0 w1 w2 w3 w4 ", 5, "stop"))
        self.assertEqual(c["messages"][0]["meta"]["attachments"], ["a.py"])

    def test_sampling_thinking_and_system(self):
        srv = SSE(n=2, reasoning=True)
        c = self.s.create(system="Be brief.", settings={"temperature": 0.8, "thinking": True, "max_tokens": 900})
        ev = self.run_relay(srv, c["id"])
        b = srv.bodies[0]
        self.assertEqual((b["temperature"], b["top_k"], b["max_tokens"]), (0.8, 40, 900))  # top_k always with temperature
        self.assertEqual(b["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual(b["messages"][0], {"role": "system", "content": "Be brief."})
        self.assertIn("reasoning", [e["type"] for e in ev])
        self.assertEqual(self.s.get(c["id"])["messages"][-1]["reasoning"], "thinking it over")

    def test_stop_keeps_the_partial_answer(self):
        srv = SSE(n=200, delay=0.02)
        c = self.s.create()
        out = []
        t = threading.Thread(target=lambda: out.extend(self.run_relay(srv, c["id"])))
        t.start()
        time.sleep(0.4)
        self.assertTrue(self.rl.busy(c["id"]))
        self.assertTrue(self.rl.stop(c["id"]))
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertTrue(out[-1]["stopped"])
        a = self.s.get(c["id"])["messages"][-1]
        self.assertTrue(a["meta"]["stopped"])
        self.assertTrue(0 < len(a["content"]) < len(" ".join(f"w{i}" for i in range(200))))
        self.assertFalse(self.rl.busy(c["id"]))

    def test_edit_and_regenerate(self):
        srv = SSE(n=2)
        c = self.s.create()
        self.run_relay(srv, c["id"], "first")
        self.run_relay(srv, c["id"], "second")
        self.run_relay(srv, c["id"], None, mode="regenerate")
        msgs = self.s.get(c["id"])["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant"])
        self.assertEqual(srv.bodies[-1]["messages"][-1]["content"], "second")
        self.run_relay(srv, c["id"], "second, edited", mode="edit")
        msgs = self.s.get(c["id"])["messages"]
        self.assertEqual([m["content"] for m in msgs if m["role"] == "user"], ["first", "second, edited"])
        self.assertEqual(len(msgs), 4)
        with self.assertRaises(ValueError):
            self.run_relay(srv, self.s.create()["id"], None, mode="regenerate")
        with self.assertRaises(ValueError):
            self.run_relay(srv, c["id"], "   ")

    def test_model_error_is_reported(self):
        srv = SSE(status=500)
        c = self.s.create()
        ev = self.run_relay(srv, c["id"])
        err = [e for e in ev if e["type"] == "error"]
        self.assertTrue(err and "HTTP 500" in err[0]["error"] and "top_k" in err[0]["error"])
        self.assertIn("HTTP 500", self.s.get(c["id"])["messages"][-1]["meta"]["error"])


class Routes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="hc-chat-")
        HC.CONF_DIR = tmp
        cfg = dict(HC.DEFAULTS, ui_port=0, halod_local_port=1, lan=False)
        self.app = HC.App(cfg)
        self.app.runner = agents.Runner(os.path.join(tmp, "agents"), "http://127.0.0.1:1/v1", lambda: None)
        self.app.chats = chats.Store(os.path.join(tmp, "chats.sqlite"))
        self.app.relay = chats.Relay()
        self.addCleanup(self.app.runner.close)
        self.cookie = unlocked(self.app, tmp)
        self.sse = SSE(n=4)
        self.serving = {"state": "ready", "endpoint": "http://127.0.0.1:18080/v1", "served_model": "dsv41", "run_id": "r1",
                        "profile": {"id": "agent-coding", "model": "DeepSeek-V4.1-Flash-1017"}}
        self.state = None
        self.app.halod_state = lambda fresh=False: self.state
        self.app.local_endpoint = lambda st=None: self.sse.url
        HC.Handler.A = self.app
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), HC.Handler)
        self.port = self.srv.server_address[1]
        cfg["ui_port"] = self.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.host = f"127.0.0.1:{self.port}"

    def req(self, method, path, body=None, token=True, **h):
        headers = {"Host": self.host, "Content-Type": "application/json", "Cookie": self.cookie}
        if token:
            headers["X-Halo-UI-Token"] = self.app.ui_token
        headers.update({k.replace("_", "-"): v for k, v in h.items()})
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", None if body is None else json.dumps(body).encode(), headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_guards_and_stream(self):
        code, body, _ = self.req("POST", "/local/chats", {})
        cid = json.loads(body)["id"]
        self.assertEqual(self.req("POST", f"/local/chats/{cid}/send", {"content": "hi"}, token=False)[0], 403)
        self.assertEqual(self.req("POST", f"/local/chats/{cid}/send", {"content": "hi"}, Origin="http://evil.example")[0], 403)
        self.assertEqual(self.req("POST", f"/local/chats/{cid}/send", {"content": "hi"}, Host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", f"/local/chats/{cid}", None, Host="evil.example")[0], 403)
        code, body, _ = self.req("POST", f"/local/chats/{cid}/send", {"content": "hi"})
        self.assertEqual(code, 409)  # nothing serving
        self.assertIn("no model is serving", json.loads(body)["error"])
        self.state = self.serving
        code, body, _ = self.req("POST", f"/local/chats/{cid}/send", {"content": "   "})
        self.assertEqual(code, 400)
        code, body, hdr = self.req("POST", f"/local/chats/{cid}/send", {"content": "hi there"})
        self.assertEqual(code, 200)
        self.assertTrue(hdr["Content-Type"].startswith("text/event-stream"))
        events = [json.loads(l[6:]) for l in body.decode().split("\n") if l.startswith("data: ")]
        self.assertEqual([e["type"] for e in events][:2], ["user", "start"])
        self.assertEqual(events[-1]["type"], "done")
        code, body, _ = self.req("GET", f"/local/chats/{cid}")
        c = json.loads(body)
        self.assertEqual([m["role"] for m in c["messages"]], ["user", "assistant"])
        self.assertEqual(c["messages"][1]["meta"]["model"], "DeepSeek-V4.1-Flash-1017")
        code, body, hdr = self.req("GET", f"/local/chats/{cid}/export")
        self.assertEqual(code, 200)
        self.assertIn("attachment", hdr["Content-Disposition"])
        self.assertIn(b"hi there", body)
        code, body, _ = self.req("GET", "/local/chats?q=there")
        self.assertEqual([c["id"] for c in json.loads(body)["chats"]], [cid])
        self.assertEqual(self.req("DELETE", f"/local/chats/{cid}", {})[0], 200)
        self.assertEqual(self.req("GET", f"/local/chats/{cid}")[0], 404)

    def test_never_a_model_halod_did_not_start(self):
        """The tunnel forwards to whatever listens on the box's port: when it answers with another model than the one
        halod started (a team bench on 18080), or halod has no run, the chat refuses and sends nothing."""
        cid = json.loads(self.req("POST", "/local/chats", {})[1])["id"]
        self.state = dict(self.serving, served_model="dsv41-unc")  # halod started the uncensored model; the tunnel says dsv41
        code, body, _ = self.req("POST", f"/local/chats/{cid}/send", {"content": "hi"})
        self.assertEqual((code, json.loads(body)["state"]), (409, "mismatch"))
        self.state = dict(self.serving, run_id=None)
        self.app._models_seen.clear()
        code, body, _ = self.req("POST", f"/local/chats/{cid}/send", {"content": "hi"})
        self.assertEqual((code, json.loads(body)["state"]), (409, "not_ready"))
        self.assertEqual(self.sse.bodies, [])  # nothing reached the model
        self.state = self.serving
        self.app._models_seen.clear()
        self.assertEqual(self.req("POST", f"/local/chats/{cid}/send", {"content": "hi"})[0], 200)

    def test_closing_the_page_stops_the_model(self):
        self.sse.n, self.sse.delay = 300, 0.02
        self.state = self.serving
        cid = json.loads(self.req("POST", "/local/chats", {})[1])["id"]
        import socket
        body = json.dumps({"content": "long"}).encode()
        sk = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        sk.sendall((f"POST /local/chats/{cid}/send HTTP/1.1\r\nHost: {self.host}\r\nContent-Type: application/json\r\n"
                    f"X-Halo-UI-Token: {self.app.ui_token}\r\nCookie: {self.cookie}\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
        got = b""
        while b"delta" not in got:
            got += sk.recv(4096)
        sk.close()  # the tab went away
        for _ in range(100):
            if not self.app.relay.busy(cid):
                break
            time.sleep(0.1)
        self.assertFalse(self.app.relay.busy(cid))
        a = self.app.chats.get(cid)["messages"][-1]
        self.assertTrue(a["meta"]["stopped"])


if __name__ == "__main__":
    unittest.main()
