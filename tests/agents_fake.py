"""A stand-in for the chat model, for Agents' tests: an
OpenAI-style /v1/chat/completions that answers the research loop's requests by what each asks for (plan JSON, notes
with exact quotes from the part it was given, a tool call, an outline, sections with citations), and records every
request (body and headers) so the tests can check the cap, Connection: close and effort low.

Modes (FakeModel.mode): "normal"; "exfil" (after reading a page that names collector.example, it asks to read
collector.example with text from its notes in the query, as a model that followed an injected instruction would);
"search" (it searches again before reading). FakeModel.delay_s holds each answer that long (the user-first timing).
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "box", "fake"))
import fake_research  # noqa: E402


class FakeModel:
    def __init__(self, served="dsv41"):
        self.served, self.requests, self.mode, self.delay_s = served, [], "normal", 0.0
        self.token_s, self.dropped = 0.002, 0  # seconds between streamed words; requests the client closed mid-reply
        self.mu = threading.Lock()
        self.active = 0
        fm = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.endswith("/models"):
                    return self.send(200, {"data": [{"id": fm.served}]})
                return self.send(404, {"error": "no"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                with fm.mu:
                    fm.requests.append({"body": body, "headers": dict(self.headers), "t": time.time()})
                    fm.active += 1
                try:
                    if fm.delay_s:
                        time.sleep(fm.delay_s)
                    msg = fm.answer(body)
                finally:
                    with fm.mu:
                        fm.active -= 1
                prompt = sum(len(m.get("content") or "") for m in body.get("messages") or []) // 4
                if body.get("stream"):  # server-sent events (the research loop streams; so does a chat turn)
                    return self.stream(msg, prompt)
                self.send(200, {"id": "x", "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                                "usage": {"prompt_tokens": prompt, "completion_tokens": max(1, len(msg.get("content") or "") // 4),
                                          "prompt_tokens_details": {"cached_tokens": prompt // 2}}})

            def stream(self, msg, prompt):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()

                def put(obj):
                    self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                    self.wfile.flush()
                text = msg.get("content") or ("" if msg.get("tool_calls") else "OK")
                words = text.split(" ") if text else []
                try:
                    for i, w in enumerate(words):
                        put({"choices": [{"index": 0, "delta": {"content": w + (" " if i < len(words) - 1 else "")}}]})
                        time.sleep(fm.token_s)
                    for k, tc in enumerate(msg.get("tool_calls") or []):
                        a = tc["function"]["arguments"]
                        put({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": k, "id": tc["id"], "type": "function",
                                                                                "function": {"name": tc["function"]["name"], "arguments": a[:len(a) // 2]}}]}}]})
                        time.sleep(fm.token_s)
                        put({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": k, "function": {"arguments": a[len(a) // 2:]}}]}}]})
                    put({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                         "usage": {"prompt_tokens": prompt, "completion_tokens": max(1, len(words)), "prompt_tokens_details": {"cached_tokens": prompt // 2}}})
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    with fm.mu:
                        fm.dropped += 1  # the client closed it (an agent request dropped for the user)

            def send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if (self.headers.get("Connection") or "").lower() == "close":
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()

    def answer(self, body):
        return fake_research.answer(body, self.mode)
