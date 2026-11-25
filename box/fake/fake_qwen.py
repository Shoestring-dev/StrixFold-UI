#!/usr/bin/env python3
"""fake_qwen.py ROOT up|down [DIR]: a stand-in for halo-qwen in the fake backend (DIR "kyojin": for the team's
halo-qwen-kyojin, whose server answers /health in place of /ready). `up` writes the node's state
file, starts a small OpenAI-style server on the port in ROOT/qwen/port (/ready, /v1/models, /v1/chat/completions)
and returns once it answers; `down` stops it and writes "released". ROOT/qwen/fail-up makes `up` fail; ROOT/<dir>/up-s
and ROOT/<dir>/down-s hold `up` and `down` that many seconds (screenshots of a slow swap)."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

root, cmd = sys.argv[1], sys.argv[2]
q = os.path.join(root, sys.argv[3] if len(sys.argv) > 3 else "qwen")
os.makedirs(q, exist_ok=True)
port = int(open(os.path.join(q, "port")).read())
READY = "/health" if os.path.basename(q) == "kyojin" else "/ready"  # Kyojin answers /health only
PIDS = ["server.pid"]


# DIR "kyojin": one node per box, each with its own run dir (ROOT/<box>/kyojin: state, alive, hold, idle-stop)
PER_BOX = [os.path.join(root, b, "kyojin") for b in ("box1", "box2")] if os.path.basename(q) == "kyojin" else []


def state(s):
    for d in [q] + PER_BOX:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "state"), "w") as f:
            f.write(s + "\n")


def serve():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == READY:
                # as Kyojin's own (tools/qwen/serve.py on box1): its source and whether the vision tower is loaded
                return self.send_json({"ready": True, "status": "ok", "source": "kyojin", "vision": True}
                                      if os.path.basename(q) == "kyojin" else {"ready": True})
            if self.path == "/v1/models":
                return self.send_json({"data": [{"id": "qwen3.8-flash-next-fake"}]})
            if self.path == "/metrics":
                n = os.path.getsize(os.path.join(q, "rank.log")) if os.path.exists(os.path.join(q, "rank.log")) else 0
                b = (f"# TYPE llamacpp:requests_processing gauge\nllamacpp:requests_processing 0\n"
                     f"llamacpp:tokens_predicted_total {n}\n").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if body.get("model") != "qwen3.8-flash-next-fake":
                return self.send_json({"error": "unknown model"}, 404)
            with open(os.path.join(q, "rank.log"), "a") as f:
                pics = sum(1 for m in body.get("messages") or [] if isinstance(m.get("content"), list)
                           for x in m["content"] if isinstance(x, dict) and x.get("type") == "image_url")
                f.write(f"request max_tokens={body.get('max_tokens')} thinking={(body.get('chat_template_kwargs') or {}).get('enable_thinking', True)}"
                        + (f" images={pics}" if pics else "") + "\n")
            # like Gufo: thinking is on unless the request turns it off, and a short reply cap is spent thinking
            if (body.get("chat_template_kwargs") or {}).get("enable_thinking", True) and body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for d in ({"reasoning_content": "The user asks for the capital of France. "}, {"content": "Paris."}):
                    self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": d}]}).encode() + b"\n\n")
                last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 12}}
                self.wfile.write(b"data: " + json.dumps(last).encode() + b"\n\ndata: [DONE]\n\n")
                return
            if (body.get("chat_template_kwargs") or {}).get("enable_thinking", True):
                msg = {"role": "assistant", "content": "", "reasoning_content": "The user asks for the capital of France."}
                return self.send_json({"id": "x", "choices": [{"index": 0, "message": msg, "finish_reason": "length"}],
                                       "usage": {"prompt_tokens": 20, "completion_tokens": body.get("max_tokens") or 8}})
            if body.get("stream"):  # as Gufo streams: content chunks, a last chunk with the finish reason, [DONE]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for w in ("The capital ", "of France ", "is Paris."):
                    self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": w}}]}).encode() + b"\n\n")
                last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 8}}
                self.wfile.write(b"data: " + json.dumps(last).encode() + b"\n\ndata: [DONE]\n\n")
                return
            self.send_json({"id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": "The capital of France is Paris."},
                                                    "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 8}})
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    for d in [q] + PER_BOX:
        open(os.path.join(d, "alive"), "w").close()
    srv.serve_forever()


if cmd == "serve":
    serve()
elif cmd == "up":
    state("waiting for box1's lock")
    time.sleep(float(open(os.path.join(q, "up-s")).read() or 0.5) if os.path.exists(os.path.join(q, "up-s")) else 0.5)
    if os.path.exists(os.path.join(q, "fail-up")):
        state("rank 0 exited (fake failure)")
        print("halo-qwen: rank 0 FAILED (fake)")
        sys.exit(1)
    state("locked")
    for d in PER_BOX:  # as the node does once it holds the lock: a hold or an idle marker belongs to an earlier run
        for f in ("hold", "idle-stop"):
            if os.path.exists(os.path.join(d, f)):
                os.unlink(os.path.join(d, f))
    with open(os.path.join(q, "env"), "w") as f:  # what halo-qwen-ENGINE was given (the profile's window and sessions)
        f.write(" ".join(f"{k}={v}" for k, v in sorted(os.environ.items()) if k.startswith("KQ_")) + "\n")
    p = subprocess.Popen([sys.executable, __file__, root, "serve", os.path.basename(q)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    open(os.path.join(q, "server.pid"), "w").write(str(p.pid))
    state("rank 0 running (fake)")
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{READY}", timeout=1).read()
            print("halo-qwen: one-box Qwen is up (fake)")
            sys.exit(0)
        except OSError:
            time.sleep(0.1)
    sys.exit(1)
elif cmd == "down":
    if os.path.exists(os.path.join(q, "down-s")):
        time.sleep(float(open(os.path.join(q, "down-s")).read() or 0))
    for f in PIDS:
        try:
            os.kill(int(open(os.path.join(q, f)).read()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    for f in ["alive"] + PIDS:
        for d in [q] + PER_BOX:
            try:
                os.unlink(os.path.join(d, f))
            except OSError:
                pass
    state("released (thunderbolt-net restored)")
    print("halo-qwen: down (fake)")
