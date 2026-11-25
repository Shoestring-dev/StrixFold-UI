"""The changes under test from the review (chat_tools.EXPERIMENTS, measured by an eval harness): each is off
unless named, and named it changes only what it says. Plus the fixes that needed no test run: arguments read by their
tool's schema (coerce). A stub engine records each request; no model."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import chat_box  # noqa: E402
import chat_tools as T  # noqa: E402
import chats  # noqa: E402
import halo_chat  # noqa: E402
import studio  # noqa: E402


def engine(bodies, script):
    """Each streamed request: the next scripted reply ({"text"} or {"calls": [(name, args)]}); a summary: plain text."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(b)
            r = script.pop(0) if script else {"text": "ok"}
            if not b.get("stream"):  # ({"summary", "finish"} scripts one)
                out = json.dumps({"choices": [{"message": {"content": r.get("summary", "summary")},
                                               "finish_reason": r.get("finish", "stop")}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                return self.wfile.write(out)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            ds = [{"content": r["text"]}] if r.get("text") else []
            ds += [{"tool_calls": [{"index": i, "id": f"c{len(bodies)}{i}", "type": "function",
                                    "function": {"name": n, "arguments": json.dumps(a)}}]} for i, (n, a) in enumerate(r.get("calls") or [])]
            for d in ds:
                self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": d}]}).encode() + b"\n\n")
            fin = "tool_calls" if r.get("calls") else "stop"
            self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": fin}],
                                                    "usage": {"prompt_tokens": 50, "completion_tokens": 3}}).encode() + b"\n\ndata: [DONE]\n\n")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-exp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        keep = {k: os.environ.get(k) for k in ("CHAT_TOOLS_ROOT", "HALO_CHAT_TOOLS_TRANSPORT")}
        os.environ["CHAT_TOOLS_ROOT"] = os.path.join(self.tmp, "toolbox")
        os.environ.pop("HALO_CHAT_TOOLS_TRANSPORT", None)
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None) for k, v in keep.items()])
        chat_box._HEALTH.clear()
        self.addCleanup(lambda: (T.EXPERIMENTS.clear(), T.EXPERIMENTS.update(T.experiments())))  # the app's again
        T.EXPERIMENTS.clear()  # each test names the ones it is about
        self.st = studio.Store(os.path.join(self.tmp, "S"))
        self.agent = halo_chat.ChatAgent(self.st, chats.Relay())
        self.agent.app = types.SimpleNamespace(fake=True)
        self.bodies, self.script = [], []
        self.srv = engine(self.bodies, self.script)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.ep = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def turn(self, c, text="Hello", effort="low", atts=None, profile="unc-chat"):
        evs = list(self.agent.turn(c["id"], self.ep, "dsv41-unc", profile, text, atts, effort=effort))
        self.assertNotIn("error", [e["type"] for e in evs], evs)
        return [b for b in self.bodies if b.get("stream")][-1]

    def chat(self, perm=None):
        c = self.st.create_chat(kind="chat", model="unc-chat")
        if perm:
            self.st.set_perm(c["id"], perm, {})
        return c


class Experiments(Base):
    def test_the_proven_ones_are_on_and_a_name_adds_or_takes_one_away(self):
        self.assertEqual(T.experiments(), {"grounding", "attach_full", "longer_replies", "web_parts"})
        self.assertEqual(T.experiments("think_budget,-grounding"), {"attach_full", "longer_replies", "think_budget", "web_parts"})
        self.assertEqual(T.experiments(" -attach_full , -longer_replies,-grounding,-web_parts,-loop_stop"), set())

    def test_none_when_none_named(self):
        b = self.turn(self.chat())
        self.assertNotIn(halo_chat.GROUNDING, b["messages"][0]["content"])
        self.assertNotIn("(Now:", b["messages"][-1]["content"])
        self.assertLessEqual(b["max_tokens"], 2048)
        self.assertNotIn("thinking_budget", b)

    def test_grounding_gives_the_date_after_their_message_and_the_honesty_rules(self):
        T.EXPERIMENTS.add("grounding")
        b = self.turn(self.chat())
        self.assertIn(halo_chat.GROUNDING, b["messages"][0]["content"])
        self.assertRegex(b["messages"][-1]["content"], r"Hello\n\n\(Now: \w+day \d\d \w+ 20\d\d")
        self.assertNotIn("(Now:", b["messages"][0]["content"])  # never in the cached system prompt

    def test_longer_replies(self):
        T.EXPERIMENTS.add("longer_replies")
        self.assertEqual(self.turn(self.chat())["max_tokens"], halo_chat.LONGER_REPLY)

    def test_think_budget(self):
        T.EXPERIMENTS.add("think_budget")
        b = self.turn(self.chat(), effort="high")
        self.assertEqual((b["thinking_budget"], b["max_tokens"]), (6144, 6144 + 4096))
        b = self.turn(self.chat(), effort="extra")
        self.assertEqual((b["thinking_budget"], b["chat_template_kwargs"]), (16384, {"enable_thinking": True, "reasoning_effort": "max"}))
        self.assertNotIn("thinking_budget", self.turn(self.chat(), effort="low"))  # no thinking: no budget

    def test_attach_full_keeps_an_older_file_whole_while_it_fits(self):
        doc = "".join(f"Line {i}: filler text for the page.\n" for i in range(600)) + "THE-END-MARKER\n"
        for on in (False, True):
            T.EXPERIMENTS.discard("attach_full")
            if on:
                T.EXPERIMENTS.add("attach_full")
            c = self.chat()
            up = self.st.add_upload(doc.encode(), "notes.txt", chat_id=c["id"], text_ok=True)
            self.turn(c, "Read this.", atts=[{"id": up["id"]}])
            b = self.turn(c, "What does the last line say?")
            sent = json.dumps(b["messages"])
            self.assertEqual("THE-END-MARKER" in sent, on)

    def test_attach_full_never_sets_off_a_compaction(self):
        """A whole older file that would take the history past COMPACT_AT is cut as before: else the compaction it set
        off summarised away the file the switch keeps (the review)."""
        T.EXPERIMENTS.add("attach_full")
        cpt = halo_chat.CHARS_PER_TOKEN_GUESS  # (the fake engine's 50 prompt tokens would measure it at 4.5)
        patch = unittest.mock.patch.object(halo_chat, "chars_per_token", lambda chat: cpt)
        patch.start()
        self.addCleanup(patch.stop)
        c = self.chat()
        probe = self.turn(c, "Hi")
        row = self.st.chat(c["id"])
        ctx = [m for m in row["messages"] if m["role"] == "assistant"][-1]["meta"]["context"]
        line = halo_chat.COMPACT_AT * ctx - 2048 - halo_chat.est_tokens(probe["messages"], cpt)
        doc = "Filler text for the page.\n" * int((line - 600) * cpt / 26) + "THE-END-MARKER\n"
        up = self.st.add_upload(doc.encode(), "notes.txt", chat_id=c["id"], text_ok=True)
        self.turn(c, "Read this.", atts=[{"id": up["id"]}])  # the file's own turn: whole, under the line
        evs = list(self.agent.turn(c["id"], self.ep, "dsv41-unc", "unc-chat", "More words. " * int(900 * cpt / 12), None,
                                   effort="low"))
        self.assertNotIn("compacting", [e["type"] for e in evs])
        sent = json.dumps([b for b in self.bodies if b.get("stream")][-1]["messages"])
        self.assertNotIn("THE-END-MARKER", sent)  # cut to OLD_ATTACHMENT_CHARS, as without the switch


class Tools(Base):
    def test_honest_limits_names_what_the_tools_cant_do_only_when_on(self):
        import chat_box
        ctx = types.SimpleNamespace()
        box = chat_box.BoxTools(ctx, web=False)
        self.assertNotIn(chat_box.LIMITS, box.system_note(ctx))
        T.EXPERIMENTS.add("honest_limits")
        self.assertTrue(box.system_note(ctx).endswith(chat_box.LIMITS))
        self.assertNotIn(chat_box.LIMITS, chat_box.BoxTools(ctx, web=True).system_note(ctx))  # its commands can post

    def test_web_parts_reads_a_long_page_part_by_part(self):
        import agents_tools
        fx = os.path.join(self.tmp, "fx")
        os.makedirs(fx)
        url = "https://long.example/page"
        body = "".join(f"Line {i:05d} of the long page.\n" for i in range(1, 2001)) + "THE-LAST-LINE\n"
        with open(os.path.join(fx, "web.json"), "w") as f:
            json.dump({"search_default": [], "pages": {url: {"title": "Long", "date": "2026-10-31", "text": body}}}, f)
        keep = agents_tools.FIXTURES
        agents_tools.FIXTURES = fx
        self.addCleanup(setattr, agents_tools, "FIXTURES", keep)
        T.EXPERIMENTS.add("web_parts")
        made, keep_ts = [], chat_box.toolset
        chat_box.toolset = lambda ctx: made.append(keep_ts(ctx)) or made[-1]
        self.addCleanup(setattr, chat_box, "toolset", keep_ts)
        c = self.chat("box_web")
        self.script += [{"calls": [("web_read", {"url": url})]}, {"calls": [("web_read", {"url": url, "part": "2"})]},
                        {"calls": [("web_read", {"url": url, "part": "9"})]}, {"text": "Done."}]
        b = self.turn(c, "Read it.")
        spec = [s for s in b["tools"] if s["function"]["name"] == "web_read"][0]
        self.assertIn("part", spec["function"]["parameters"]["properties"])
        first, second, last = [m["content"] for m in b["messages"] if m["role"] == "tool"][-3:]
        parts = chat_box.page_parts(body)
        self.assertGreater(len(parts), 2)
        self.assertIn(f"(Part 1 of {len(parts)}. Read on with part 2.)", first)
        self.assertIn("Dated 2026-10-31.", first)
        self.assertNotIn("THE-LAST-LINE", first)
        for seen, part in ((first, parts[0]), (second, parts[1])):  # each part whole, to its last line
            self.assertIn(part.splitlines()[-1], seen)
            self.assertTrue(seen.endswith("</untrusted>"), seen[-200:])
        self.assertIn(f"(Part {len(parts)} of {len(parts)}. There is no part 9: this is the last.)", last)
        self.assertIn("THE-LAST-LINE", last)
        self.assertEqual([x for x in made[0].webtools.calls if x[0] == "web_read"], [("web_read", url)])  # read once

    def test_web_parts_reads_a_page_again_when_asked_for_it_again(self):
        """Only a later part comes from the page this turn read: the page itself asked for again is fetched again (the
        review: a page that changes, as a build's status, came back as its first copy all turn)."""
        import agents_tools
        T.EXPERIMENTS.add("web_parts")
        url, n = "https://status.example/build", []
        def web_read(tools, u):
            n.append(u)
            return {"url": u, "title": "Build", "text": f"build {len(n)}\n" + f"line of {len(n)}\n" * 3000}
        keep = agents_tools.Stub.web_read
        agents_tools.Stub.web_read = web_read
        self.addCleanup(setattr, agents_tools.Stub, "web_read", keep)
        c = self.chat("box_web")
        self.script += [{"calls": [("web_read", {"url": url})]}, {"calls": [("web_read", {"url": url, "part": 2})]},
                        {"calls": [("web_read", {"url": url})]}, {"calls": [("web_read", {"url": url, "part": 1})]},
                        {"calls": [("web_read", {"url": url, "part": 2})]}, {"text": "Done."}]
        b = self.turn(c, "Watch it.")
        got = [m["content"] for m in b["messages"] if m["role"] == "tool"][-5:]
        self.assertEqual([re.search(r"(?:build|line of) (\d)", x).group(1) for x in got], ["1", "1", "2", "3", "3"])
        self.assertEqual(len(n), 3)  # each part 2 came from the read before it

    def test_web_parts_cut_at_a_line_end_and_fit_what_the_loop_passes_on(self):
        body = "".join(f"Line {i:05d} says something about the page.\n" for i in range(1, 3001))
        parts = chat_box.page_parts(body)
        self.assertEqual("".join(parts), body)
        self.assertTrue(all(len(p) <= chat_box.WEB_PART and p.endswith("\n") for p in parts))
        words = "word " * 9000  # no lines: at a word's end
        self.assertTrue(all(p.endswith(" ") for p in chat_box.page_parts(words)[:-1]))
        self.assertEqual("".join(chat_box.page_parts("x" * 25000)), "x" * 25000)  # neither: at the limit
        self.assertEqual(chat_box.page_parts(""), [""])
        url = "https://long.example/" + '"a b<c>' * 400  # the worst a heading and the block's source can be
        page = {"title": "T" * 5000, "url": url, "date": "2026-10-31 " * 20, "text": body}
        for p in range(1, len(parts) + 1):
            self.assertLessEqual(len(chat_box.untrusted(f"web page: {url}", chat_box.web_part(page, url, p))), T.OUT_MODEL)


    def test_read_first_refuses_an_unread_file_until_it_is_read(self):
        for on in (False, True):
            T.EXPERIMENTS.discard("read_first")
            if on:
                T.EXPERIMENTS.add("read_first")
            c = self.chat("box")
            folder = os.path.join(os.environ["CHAT_TOOLS_ROOT"], c["id"])
            os.makedirs(folder)
            with open(os.path.join(folder, "todo.txt"), "w") as f:
                f.write("milk\neggs\nbread\n")
            self.script[:] = [{"calls": [("write_file", {"path": "todo.txt", "content": "only this\n"})]},
                              {"calls": [("edit_file", {"path": "todo.txt", "old": "eggs", "new": "jam"})]},
                              {"calls": [("write_file", {"path": "new.txt", "content": "fresh\n"})]},
                              {"calls": [("read_file", {"path": "todo.txt"})]},
                              {"calls": [("edit_file", {"path": "./todo.txt", "old": "bread", "new": "rice"})]},
                              {"text": "done"}]
            self.turn(c, "Tidy the list.")
            sent = json.dumps(self.bodies[-1]["messages"])
            with open(os.path.join(folder, "todo.txt")) as f:
                got = f.read()
            self.assertEqual(got, "milk\neggs\nrice\n" if on else "only this\n")  # the unread edit refused, the read one made
            self.assertEqual("already exists (3 lines) and you haven't read it" in sent, on)
            self.assertEqual("read todo.txt first (read_file)" in sent, on)
            with open(os.path.join(folder, "new.txt")) as f:
                self.assertEqual(f.read(), "fresh\n")  # a new file needs no read
            del self.bodies[:]

    def test_read_first_leaves_a_missing_file_to_its_own_error_and_counts_owners_attachment_as_read(self):
        T.EXPERIMENTS.add("read_first")
        c = self.chat("box")
        up = self.st.add_upload(b"alpha\nbeta\n", "notes.txt", chat_id=c["id"], text_ok=True)
        self.script[:] = [{"calls": [("edit_file", {"path": "attachments/notes.txt", "old": "beta", "new": "gamma"})]},
                          {"calls": [("edit_file", {"path": "missing.txt", "old": "a", "new": "b"})]},
                          {"text": "done"}]
        self.turn(c, "Change beta to gamma.", atts=[{"id": up["id"]}])
        with open(os.path.join(os.environ["CHAT_TOOLS_ROOT"], c["id"], "attachments", "notes.txt")) as f:
            self.assertEqual(f.read(), "alpha\ngamma\n")  # its text came with the message: no read needed
        sent = json.dumps(self.bodies[-1]["messages"])
        self.assertNotIn("read missing.txt first", sent)
        self.assertIn("That didn't work", sent)  # the edit's own answer: there is no such file

    def test_read_first_keeps_the_latest_chats(self):
        keep = dict(chat_box.READ)
        self.addCleanup(lambda: (chat_box.READ.clear(), chat_box.READ.update(keep)))
        chat_box.READ.clear()
        for i in range(chat_box.READ_CHATS + 5):  # c0-c4 forgotten
            chat_box.mark_read(f"c{i}", "./a.txt")
        chat_box.mark_read("c5", "b.txt")  # the oldest, reading again: now the newest
        chat_box.mark_read("new", "a.txt")  # so c6 goes, not c5
        self.assertEqual(len(chat_box.READ), chat_box.READ_CHATS)
        self.assertFalse(chat_box.was_read("c4", "a.txt") or chat_box.was_read("c6", "a.txt"))
        self.assertTrue(chat_box.was_read("c5", "/work/a.txt") and chat_box.was_read("c5", "b.txt"))
        self.assertEqual(chat_box.SEEN_CHARS, halo_chat.MAX_FILE_CHARS)

    def test_compact_v2_asks_for_the_sections_and_more_room(self):
        for on in (False, True):
            T.EXPERIMENTS.discard("compact_v2")
            if on:
                T.EXPERIMENTS.add("compact_v2")
            c = self.chat()
            for i in range(12):
                self.st.add_message(c["id"], "user" if i % 2 == 0 else "assistant", f"message {i}: the code is HX-{i}", {})
            self.assertIsNotNone(self.agent.compact(c["id"], self.ep, "dsv41-unc", 16384))
            b = [b for b in self.bodies if not b.get("stream")][-1]
            self.assertEqual(b["messages"][0]["content"], halo_chat.SUMMARY_SYSTEM_V2 if on else halo_chat.SUMMARY_SYSTEM)
            self.assertEqual(b["max_tokens"], 16384 // 6 if on else halo_chat.SUMMARY_TOKENS)
        self.assertEqual(halo_chat.summary_form(524288)[1], halo_chat.SUMMARY_TOKENS_V2)  # a sixth, at most 4,000
        v2 = halo_chat.SUMMARY_SYSTEM_V2
        self.assertLess(v2.index("2. Last:"), v2.index("4. Facts:"))  # what carries on first; the list that grows last
        self.assertIn("8. The user's messages:", v2)

    def test_compact_v2_says_when_a_summary_was_cut_at_its_limit(self):
        for on in (False, True):
            T.EXPERIMENTS.discard("compact_v2")
            if on:
                T.EXPERIMENTS.add("compact_v2")
            c = self.chat()
            for i in range(12):
                self.st.add_message(c["id"], "user" if i % 2 == 0 else "assistant", f"message {i}", {})
            self.script[:] = [{"summary": "1. What the user wants: the plan", "finish": "length"}]
            m = self.agent.compact(c["id"], self.ep, "dsv41-unc", 16384)
            self.assertEqual(m["content"].endswith(halo_chat.SUMMARY_CUT), on, m["content"])


class TunnelGap(Base):
    """A turn died at step 80 ("Connection refused"): its tunnel had no connection while a command
    ran, so it counted as unused and was rebuilt, and the next step came a moment before it was back."""
    def gap(self, seconds):
        """The engine's port gone while the turn's tool runs, and back after `seconds` (None: never)."""
        port, keep = self.srv.server_address[1], chat_box.BoxTools.call

        def back():
            time.sleep(seconds)
            srv = ThreadingHTTPServer(("127.0.0.1", port), self.srv.RequestHandlerClass)
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            srv.serve_forever()

        def call(tools, name, args, ctx):
            self.srv.shutdown()
            self.srv.server_close()
            if seconds is not None:
                threading.Thread(target=back, daemon=True).start()
            return keep(tools, name, args, ctx)
        chat_box.BoxTools.call = call
        self.addCleanup(setattr, chat_box.BoxTools, "call", keep)

    def events(self):
        self.script += [{"calls": [("list_dir", {"path": "."})]}, {"text": "All listed."}]
        return list(self.agent.turn(self.chat("box")["id"], self.ep, "dsv41-unc", "unc-chat", "List it.", None, effort="low"))

    def test_a_step_waits_out_a_tunnel_being_rebuilt(self):
        self.gap(1.0)
        evs = self.events()
        self.assertNotIn("error", [e["type"] for e in evs], evs[-3:])
        self.assertIn("All listed.", "".join(e.get("text", "") for e in evs if e["type"] == "delta"))
        self.assertEqual(len([b for b in self.bodies if b.get("stream")]), 2)  # the step reached the engine once

    def test_a_tunnel_that_stays_down_is_still_an_error(self):
        keep, halo_chat.TUNNEL_GAP_S = halo_chat.TUNNEL_GAP_S, 0.5
        self.addCleanup(setattr, halo_chat, "TUNNEL_GAP_S", keep)
        self.gap(None)
        self.assertIn("did not answer", json.dumps(self.events()))

    def test_stop_ends_the_wait_and_only_a_refusal_is_tried_again(self):
        class Conn:
            n = 0

            def __init__(self, err):
                self.err = err

            def connect(self):
                self.n += 1
                raise self.err
        stop, t0 = threading.Event(), time.time()
        threading.Timer(0.3, stop.set).start()
        with self.assertRaises(ConnectionRefusedError):
            halo_chat.connect(Conn(ConnectionRefusedError()), stop)
        self.assertLess(time.time() - t0, 2)
        for err in (ConnectionResetError(), TimeoutError()):
            c = Conn(err)
            with self.assertRaises(type(err)):
                halo_chat.connect(c, threading.Event())
            self.assertEqual(c.n, 1)


class Coerce(Base):
    def test_values_written_as_text_are_read_by_the_schema(self):
        spec = {"function": {"name": "f", "parameters": {"properties": {
            "b": {"type": "boolean"}, "n": {"type": "integer"}, "x": {"type": "number"}, "a": {"type": "array"},
            "s": {"type": "string"}}}}}
        self.assertEqual(T.coerce({"b": "false", "n": "12", "x": "1.5", "a": "[1, 2]", "s": "true", "u": "true"}, spec),
                         {"b": False, "n": 12, "x": 1.5, "a": [1, 2], "s": "true", "u": "true"})
        self.assertEqual(T.coerce({"b": "maybe", "n": "1x", "a": "[oops"}, spec), {"b": "maybe", "n": "1x", "a": "[oops"})
        self.assertEqual(T.coerce({"b": True}, None), {"b": True})

    def test_a_replace_all_written_as_text_replaces_them_all(self):
        c = self.chat("box")
        folder = os.path.join(os.environ["CHAT_TOOLS_ROOT"], c["id"])
        os.makedirs(folder)
        with open(os.path.join(folder, "a.txt"), "w") as f:
            f.write("colour colour\n")
        self.script += [{"calls": [("edit_file", {"path": "a.txt", "old": "colour", "new": "color", "replace_all": "true"})]},
                        {"text": "Done."}]
        self.turn(c, "fix it")
        with open(os.path.join(folder, "a.txt")) as f:
            self.assertEqual(f.read(), "color color\n")


if __name__ == "__main__":
    unittest.main()
