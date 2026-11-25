"""Chat permissions and tools: the tool loop in ChatAgent._turn, the
container levels (mac/chat_box.py with box/halo-tools/chat-tools run on this Mac against a temp folder: no Toolbox, no
sandbox, test commands only), taps, the level read again before every call, Stop, the audit file, the routes' checks,
and the sandbox and gate as written for Toolbox. A stub engine scripts the model's replies; nothing reaches a model, a
box, the internet or port 18080."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import chat_box  # noqa: E402
import chat_tools as T  # noqa: E402
import chats  # noqa: E402
import halo_chat  # noqa: E402
import mediainfo  # noqa: E402
import studio  # noqa: E402

TOOLS_DIR = os.path.join(ROOT, "box", "halo-tools")


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), os.path.join(TOOLS_DIR, name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def engine(script, bodies):
    """A stub OpenAI endpoint: each request takes the next scripted reply: {"text"} or {"calls": [(name, args)]}."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(b)
            r = script.pop(0) if script else {"text": "(no more script)"}
            if r.get("hook"):  # something that happens while the model answers (the user sends a message)
                r["hook"]()
            if r.get("status"):
                out = json.dumps({"error": {"message": r.get("error", "refused")}}).encode()
                self.send_response(r["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                return self.wfile.write(out)
            if not b.get("stream"):  # a request that doesn't stream (a summary): one JSON answer
                out = json.dumps({"choices": [{"message": {"content": r.get("text") or ""}, "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                return self.wfile.write(out)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = []
            if r.get("text"):
                chunks.append({"content": r["text"]})
            chunks += [{"content": t} for t in r.get("texts") or []]  # words in several deltas
            for i, (name, args) in enumerate(r.get("calls") or []):
                a = args if isinstance(args, str) else json.dumps(args)  # a string: the arguments as the model wrote them
                chunks.append({"tool_calls": [{"index": i, "id": f"call_{len(bodies)}_{i}", "function": {"name": name, "arguments": a[:5]}}]})
                chunks.append({"tool_calls": [{"index": i, "function": {"arguments": a[5:]}}]})
            for d in chunks:
                self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": d}]}).encode() + b"\n\n")
                self.wfile.flush()
            fin = r.get("finish") or ("tool_calls" if r.get("calls") else "stop")
            last = {"choices": [{"index": 0, "delta": {}, "finish_reason": fin}], "usage": r.get("usage") or {"prompt_tokens": 900, "completion_tokens": 9}}
            if r.get("engine"):
                last["tensorfold"] = r["engine"]
            self.wfile.write(b"data: " + json.dumps(last).encode() + b"\n\ndata: [DONE]\n\n")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-tools-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.box_root = os.path.join(self.tmp, "toolbox")
        old = {k: os.environ.get(k) for k in ("CHAT_TOOLS_ROOT", "HALO_CHAT_TOOLS_TRANSPORT")}
        os.environ["CHAT_TOOLS_ROOT"] = self.box_root
        os.environ.pop("HALO_CHAT_TOOLS_TRANSPORT", None)
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None) for k, v in old.items()])
        chat_box._HEALTH.clear()
        self.st = studio.Store(os.path.join(self.tmp, "S"))
        self.relay = chats.Relay()
        self.agent = halo_chat.ChatAgent(self.st, self.relay)
        self.agent.app = types.SimpleNamespace(fake=True)
        self.script, self.bodies = [], []
        self.srv = engine(self.script, self.bodies)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.ep = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def chat(self, perm="box", model="quick-chat"):
        c = self.st.create_chat(kind="chat", model=model)
        self.st.set_perm(c["id"], perm, {})
        return c

    def turn(self, c, text="do it", atts=None, tools=True, profile="quick-chat", effort=None):
        return list(self.agent.turn(c["id"], self.ep, "served", profile, text, atts, effort=effort, tools=tools))

    def answer(self, c):
        return [m for m in self.st.messages(c["id"]) if m["role"] == "assistant"][-1]


class Loop(Base):
    def test_a_container_turn_runs_a_command_and_answers(self):
        c = self.chat("box")
        self.script += [{"text": "Let me check.", "calls": [("run_command", {"command": "echo hi > a.txt; cat a.txt; echo err >&2"})]},
                        {"text": "The file says hi."}]
        evs = self.turn(c)
        kinds = [e["type"] for e in evs]
        self.assertEqual(kinds[:2], ["user", "start"])
        for k in ("tool", "tool_out", "tool_done", "delta", "done"):
            self.assertIn(k, kinds)
        self.assertNotIn("error", kinds, evs)
        done = next(e for e in evs if e["type"] == "tool_done")
        self.assertEqual((done["status"], done["exit"]), ("ok", 0))
        self.assertTrue(done["line"].startswith("Ran: echo hi > a.txt"), done["line"])
        self.assertIn("hi\n", done["out"])
        # the model was offered the container's tools, read its note, and got the result back as a tool message
        first, second = self.bodies
        self.assertEqual({t["function"]["name"] for t in first["tools"]},
                         {"run_command", "read_file", "write_file", "edit_file", "list_dir", "search", "show_file", "update_plan"})
        self.assertIn("Toolbox", first["messages"][0]["content"])
        self.assertIn("no internet", first["messages"][0]["content"])
        said, res = second["messages"][-2], second["messages"][-1]
        self.assertEqual((said["role"], said["tool_calls"][0]["function"]["name"]), ("assistant", "run_command"))
        self.assertEqual((res["role"], res["tool_call_id"]), ("tool", said["tool_calls"][0]["id"]))
        self.assertIn("exit 0", res["content"])
        self.assertIn("hi", res["content"])
        m = self.answer(c)
        self.assertEqual(m["content"], "Let me check.\n\nThe file says hi.")
        self.assertEqual([list(p)[0] for p in m["meta"]["parts"]], ["text", "tool", "text"])
        self.assertEqual((m["meta"]["level"], m["meta"]["calls"], m["meta"]["finish"]), ("box", 1, "stop"))
        self.assertFalse(m["meta"].get("partial"))
        self.assertTrue(os.path.exists(os.path.join(self.box_root, c["id"], "a.txt")))
        # the audit: one line, with the command
        lines = [json.loads(x) for x in open(self.st.path("chat-tools", f"{c['id']}.jsonl"))]
        self.assertEqual([(x["tool"], x["status"], x["level"], x["args"]["command"][:7]) for x in lines], [("run_command", "ok", "box", "echo hi")])
        # the next turn reads what was done, one line per tool, never the output
        hist = self.agent.history(self.st.chat(c["id"]), 16384)[0]
        self.assertIn("(Tools used for this answer: Ran: echo hi > a.txt", hist[-1]["content"])

    def test_no_tools_means_no_tools(self):
        c = self.chat("none")
        self.script.append({"text": "Plain answer."})
        evs = self.turn(c)
        self.assertNotIn("tools", self.bodies[-1])
        m = self.answer(c)
        self.assertEqual((m["content"], m["meta"].get("parts")), ("Plain answer.", None))
        self.assertNotIn("tool", [e["type"] for e in evs])
        # "send without tools" for one message of a container chat
        c2 = self.chat("box")
        self.script.append({"text": "No tools this time."})
        self.turn(c2, tools=False)
        self.assertNotIn("tools", self.bodies[-1])

    def test_lowering_the_level_stops_the_next_call(self):
        """the user lowers the level while the turn runs (between two calls of one reply): the second call doesn't run."""
        c = self.chat("box")
        self.script += [{"calls": [("run_command", {"command": "echo one"}), ("run_command", {"command": "echo two > two.txt"})]},
                        {"text": "Done."}]
        evs = []
        for e in self.agent.turn(c["id"], self.ep, "served", "quick-chat", "go"):
            evs.append(e)
            if e["type"] == "tool_done" and len([x for x in evs if x["type"] == "tool_done"]) == 1:
                self.st.set_perm(c["id"], "none", {})
        dones = [e for e in evs if e["type"] == "tool_done"]
        self.assertEqual([d["status"] for d in dones], ["ok", "refused"])
        self.assertIn("permissions are now No tools", dones[1]["line"])
        self.assertFalse(os.path.exists(os.path.join(self.box_root, c["id"], "two.txt")))
        self.assertIn("didn't run", self.bodies[-1]["messages"][-1]["content"])

    def test_a_plan_owner_can_watch(self):
        """update_plan (chat_plan: Claude Code's to-do list): offered with the tools, its checklist reaches the
        page with the step and stays in the answer; a plan that isn't one is refused in words."""
        import chat_plan
        c = self.chat("box")
        steps = [{"step": "Read the logs", "status": "done"}, {"step": "Fix the bug", "status": "in_progress"},
                 {"step": "Run the tests", "status": "pending"}]
        self.script += [{"calls": [("update_plan", {"steps": steps})]}, {"text": "Fixed."}]
        evs = self.turn(c)
        self.assertIn("update_plan", [t["function"]["name"] for t in self.bodies[0]["tools"]])
        done = [e for e in evs if e["type"] == "tool_done"]
        self.assertEqual(done[0]["plan"], steps)
        self.assertEqual(done[0]["line"], "Plan: 1 of 3 done · Fix the bug")
        part = next(p for p in self.answer(c)["meta"]["parts"] if p.get("tool"))
        self.assertEqual(part["tool"]["plan"], steps)
        self.assertIn("[in_progress] Fix the bug", json.dumps(self.bodies[1]["messages"][-1]))
        r = chat_plan.Plan().call("update_plan", {"steps": "do it"}, None)
        self.assertEqual((r["status"], r["text"]), ("failed", "Not updated: steps must be a list of {step, status}."))
        self.assertIsNone(chat_plan.toolset(types.SimpleNamespace(chat={"perm": "box"}, level="none")))  # the turn's level
        many = chat_plan.clean([{"step": f"s{i}" + "x" * 300, "status": "odd"} for i in range(30)])
        self.assertEqual((len(many), len(many[0]["step"]), many[0]["status"]), (chat_plan.MAX_STEPS, chat_plan.STEP_CHARS, "pending"))

    def test_the_plan_is_never_offered_at_level_none_nor_counted_as_a_repeat(self):
        """Review: update_plan went by the chat's stored level, so Send without tools still offered it whenever
        the PDF tools came along; and the same plan sent again counted toward REPEAT_LIMIT, which ended the answer
        early."""
        c = self.chat("box")
        self.script += [{"text": "A PDF it is."}, {"text": "Hi."}]
        self.turn(c, "make it a PDF later")  # the chat names a PDF: make_pdf is offered from now on (chat_docs)
        self.turn(c, "just say hi", tools=False)
        names = [t["function"]["name"] for t in self.bodies[-1].get("tools") or []]
        self.assertEqual(names, ["make_pdf"])
        self.bodies.clear()
        steps = [{"step": "Read the logs", "status": "in_progress"}, {"step": "Fix it", "status": "pending"}]
        self.script += [{"calls": [("update_plan", {"steps": steps})]}] * 4 + [{"text": "Done."}]
        evs = self.turn(self.chat("box"))
        self.assertEqual(len(self.bodies), 5)
        self.assertNotIn(halo_chat.FINAL_REPEATS, json.dumps(self.bodies[-1]["messages"][-1]))
        self.assertFalse([e for e in evs if e["type"] == "tool_done" and "same result" in e["line"]])

    def test_the_last_answer_with_no_room_for_the_tools_answers_without_them(self):
        """The last answer keeps its tools in the request (the prompt's start stays) while they fit; when the answer
        fits but the tools beside it don't (a tool's output was cut to fit, then the turn's limit came), it goes
        without them (review: it ended on "This is longer than ... window. Split it into parts.", with no answer)."""
        probe = self.chat("box")
        self.script.append({"text": "ok", "usage": {"prompt_tokens": 100, "completion_tokens": 2}})
        self.turn(probe)
        b = self.bodies.pop()
        est1, tools_tok = halo_chat.est_tokens(b["messages"], 3.0), int(len(json.dumps(b["tools"])) / 3.0)
        # step 1: ~600 tokens of room; the last step's messages are ~120 tokens more: ~480 with the tools (< MIN_REPLY),
        # ~1,400 without them (the box level's specs are ~940 tokens)
        ctx = halo_chat.EXTENT_PAD + halo_chat.SAFETY_TOKENS + est1 + tools_tok + 600
        old = T.MAX_STEPS
        T.MAX_STEPS = 1
        self.addCleanup(setattr, T, "MAX_STEPS", old)
        c = self.chat("box")
        self.script += [{"calls": [("list_dir", {"path": "."})], "usage": {"prompt_tokens": 100, "completion_tokens": 9}},
                        {"text": "Here is what I found.", "usage": {"prompt_tokens": 100, "completion_tokens": 9}}]
        evs = list(self.agent.turn(c["id"], self.ep, "served", "quick-chat", "do it", None, served_context=ctx))
        self.assertEqual([e["error"] for e in evs if e["type"] == "error"], [])
        self.assertEqual(self.answer(c)["content"], "Here is what I found.")
        self.assertEqual(["tools" in b for b in self.bodies], [True, False])
    def test_each_chat_starts_its_system_text_with_its_own_line(self):
        """Chats share the rest of the system text and the tools; the engine let a new chat resume in place over another
        chat's kept rows (the other then read 25,826 tokens from 0). A first line of its own keeps them apart."""
        c1, c2 = self.chat("box"), self.chat("box")
        self.script += [{"text": "One."}, {"text": "Two."}, {"text": "Three."}]
        self.turn(c1); self.turn(c2); self.turn(c1, "again")
        s1, s2, s3 = (b["messages"][0]["content"] for b in self.bodies)
        self.assertTrue(s1.startswith(f"(Chat {c1['id'][:8]}.)\n"), s1[:40])
        self.assertTrue(s2.startswith(f"(Chat {c2['id'][:8]}.)\n"))
        self.assertNotEqual(s1.split("\n", 1)[0], s2.split("\n", 1)[0])
        self.assertEqual(s1, s3)  # the same chat's start never changes

    def test_a_bigger_pool_is_not_one_chats_window(self):
        """On the main boxes' 1M pool a chat compacts where it does today (its chosen window, at least 256K), so one chat
        can't fill the pool; a chat that chose 1M gets it; a smaller pair still bounds it."""
        cw = halo_chat.chat_window
        self.assertEqual(cw({"model": "unc-chat"}, 1048576), 262144)          # a 64K chat on the 1M main boxes
        self.assertEqual(cw({"model": "unc-chat-256k"}, 1048576), 262144)
        self.assertEqual(cw({"model": "unc-chat-1m"}, 1048576), 1048576)      # chosen: it gets the pool
        self.assertEqual(cw({"model": "unc-chat-1m"}, 262144), 262144)        # never past what the pair serves
        self.assertEqual(cw({"model": "unc-chat"}, 63488), 63488)

    def test_steps_run_out_then_one_answer_without_tools(self):
        c = self.chat("box")
        old = T.MAX_STEPS
        T.MAX_STEPS = 3
        self.addCleanup(setattr, T, "MAX_STEPS", old)
        self.script += [{"calls": [("list_dir", {"path": "."})]}] * 3 + [{"text": "Here is what I found."}]
        evs = self.turn(c)
        self.assertEqual(len(self.bodies), 4)
        # the answer without tools still offers them: the prompt's start (system words, then the tools) stays the same,
        # so the engine's prompt cache keeps the conversation (a lead's 131K read again from nothing, 143 s)
        self.assertEqual((self.bodies[-1]["tools"], self.bodies[-1]["messages"][0]), (self.bodies[-2]["tools"], self.bodies[-2]["messages"][0]))
        ask = self.bodies[-1]["messages"][-1]["content"]
        self.assertIn(halo_chat.FINAL_SPENT, ask)
        self.assertIn("don't say what you will do next", ask)
        self.assertEqual(self.answer(c)["content"], "Here is what I found.")
        why = halo_chat.NOTE_SPENT.format(steps=3, calls=T.MAX_CALLS, mins=T.TURN_WALL_S // 60)
        self.assertEqual([e["text"] for e in evs if e["type"] == "note"], [why])
        self.assertIn({"note": why}, self.answer(c)["meta"]["parts"])

    def test_the_same_call_with_the_same_result_three_times_ends_the_tools(self):
        # DeepSeek wrote "Let me search for release notes." and its calls over and over
        c = self.chat("box")
        self.script += [{"text": "Let me look.", "calls": [("list_dir", {"path": "."}), ("run_command", {"command": "echo hi"})]},
                        {"text": "Let me look.", "calls": [("list_dir", {"path": "."}), ("run_command", {"command": "echo  HI"})]},
                        {"text": "Let me look.", "calls": [("list_dir", {"path": "."}), ("list_dir", {"path": "."})]},
                        {"text": "The folder is empty."}, {"text": "never asked"}]
        evs = self.turn(c)
        self.assertEqual(len(self.bodies), 4)
        self.assertIn("tools", self.bodies[-1])  # still offered (the prompt's start stays); the ask says nothing runs
        self.assertIn(halo_chat.FINAL_REPEATS, self.bodies[-1]["messages"][-1]["content"])
        lines = [e["line"] for e in evs if e["type"] == "tool_done"]
        same = [ln.endswith(" · same result as before") for ln in lines]
        # "echo  HI" is the same call, case and spacing aside, but its result differs: never a repeat
        self.assertEqual(same, [False, False, True, False, True, True])
        tool_msgs = [m["content"] for m in self.bodies[-1]["messages"] if m["role"] == "tool"]
        self.assertEqual([t == halo_chat.REPEATED for t in tool_msgs], same)
        self.assertIn("HI", tool_msgs[3])
        self.assertEqual([e["text"] for e in evs if e["type"] == "note"], [halo_chat.NOTE_REPEATS])
        m = self.answer(c)
        self.assertEqual(m["content"], "Let me look.\n\nLet me look.\n\nLet me look.\n\nThe folder is empty.")
        self.assertEqual(m["meta"]["parts"][-2], {"note": halo_chat.NOTE_REPEATS})
        with open(self.st.path("chat-tools", f"{c['id']}.jsonl")) as f:
            self.assertEqual([bool(json.loads(ln).get("repeat")) for ln in f], same)

    def test_a_turn_with_tools_samples_with_a_fresh_seed(self):
        c = self.chat("box")
        self.script += [{"calls": [("list_dir", {"path": "."})]}, {"text": "ok"}]
        self.turn(c)
        for b in self.bodies:
            self.assertEqual((b["temperature"], b["top_k"]), (T.SAMPLED["temperature"], T.SAMPLED["top_k"]))
        self.assertNotEqual(self.bodies[0]["seed"], self.bodies[1]["seed"])
        self.assertNotIn("sampled_off", self.answer(c)["meta"])
        self.bodies.clear()
        self.turn(c, tools=False)  # a message sent without tools stays greedy, as before
        self.assertEqual(self.bodies[0]["temperature"], 0)
        self.assertNotIn("top_k", self.bodies[0])

    def test_an_engine_that_wont_sample_gets_greedy_for_the_rest_of_the_turn(self):
        c = self.chat("box")
        self.script += [{"status": 400, "error": "top_k is not supported"}, {"calls": [("list_dir", {"path": "."})]}, {"text": "ok"}]
        evs = self.turn(c)
        self.assertNotIn("error", [e["type"] for e in evs])
        self.assertEqual(len(self.bodies), 3)
        self.assertIn("top_k", self.bodies[0])
        for b in self.bodies[1:]:
            self.assertEqual(b["temperature"], 0)
            self.assertFalse({"top_k", "seed"} & set(b))
        self.assertTrue(self.answer(c)["meta"]["sampled_off"])
        self.assertEqual(self.answer(c)["content"], "ok")

    def test_stop_ends_a_long_command(self):
        c = self.chat("box")
        self.script += [{"calls": [("run_command", {"command": "sleep 30"})]}, {"text": "never"}]
        evs, t0 = [], time.time()
        for e in self.agent.turn(c["id"], self.ep, "served", "quick-chat", "wait"):
            evs.append(e)
            if e["type"] == "tool":
                threading.Timer(0.5, self.relay.stop, args=(c["id"],)).start()
        self.assertLess(time.time() - t0, 15)
        done = next(e for e in evs if e["type"] == "tool_done")
        self.assertEqual(done["status"], "stopped")
        m = self.answer(c)
        self.assertTrue(m["meta"]["stopped"])
        self.assertEqual(len(self.bodies), 1)  # nothing asked of the model after Stop
        self.assertFalse(self.relay.busy(c["id"]))

    def test_a_refused_path_and_an_unknown_tool(self):
        c = self.chat("box")
        self.script += [{"calls": [("read_file", {"path": "../../etc/passwd"}), ("format_disk", {})]}, {"text": "ok"}]
        evs = self.turn(c)
        dones = [e for e in evs if e["type"] == "tool_done"]
        self.assertEqual([d["status"] for d in dones], ["refused", "refused"])
        self.assertIn("without ..", self.bodies[-1]["messages"][-2]["content"])
        self.assertIn("There is no tool called format_disk", self.bodies[-1]["messages"][-1]["content"])

    def test_attachments_go_into_the_container_and_a_picture_comes_back(self):
        c = self.chat("box")
        txt = self.st.save_text("name,score\nSam,9\n", "scores.csv", c["id"])
        pic = self.st.add_media(mediainfo.test_pattern(64, 48, 3), "board.png", c["id"])
        self.script += [{"calls": [("run_command", {"command": "ls attachments; cp attachments/board.png out.png"}),
                                   ("show_file", {"path": "out.png"})]}, {"text": "Shown."}]
        evs = self.turn(c, "use these", [{"id": txt["id"]}, {"id": pic["id"]}])
        notes = [e["text"] for e in evs if e["type"] == "note"]
        self.assertEqual(notes, ["Copied into the container: attachments/scores.csv, attachments/board.png"])
        dones = [e for e in evs if e["type"] == "tool_done"]
        self.assertIn("board.png", dones[0]["out"])
        self.assertTrue(dones[1]["shown"].startswith("u"))
        self.assertEqual(self.st.upload(dones[1]["shown"])["kind"], "image")
        parts = self.answer(c)["meta"]["parts"]
        self.assertEqual(parts[0], {"note": "Copied into the container: attachments/scores.csv, attachments/board.png"})

    def test_the_thinking_retry_and_an_engine_refusal(self):
        c = self.chat("box")
        self.script += [{"status": 400, "error": "tools with thinking are not supported"}, {"text": "Answered without thinking."}]
        evs = self.turn(c, effort="high")
        self.assertNotIn("error", [e["type"] for e in evs])
        self.assertEqual([b["chat_template_kwargs"]["enable_thinking"] for b in self.bodies], [True, False])
        self.assertTrue(self.answer(c)["meta"]["thinking_off"])


class StopsShort(Base):
    """A reply that stops short: sent back to do what it announced, carried on when cut off, at most halo_chat.NUDGES
    times."""

    def test_an_announced_step_is_taken(self):
        c = self.chat("box")
        self.script += [{"text": "I read the task. Let me look at the file:"},
                        {"text": "Reading it now.", "calls": [("run_command", {"command": "echo hi"})]},
                        {"text": "The file says hi."}]
        evs = self.turn(c)
        self.assertEqual(len(self.bodies), 3)
        self.assertEqual(self.bodies[1]["messages"][-1], {"role": "user", "content": halo_chat.NUDGE["said"]})
        self.assertEqual(self.bodies[1]["messages"][-2]["content"], "I read the task. Let me look at the file:")
        self.assertIn("tool_done", [e["type"] for e in evs])
        text = self.answer(c)["content"]
        self.assertIn("Let me look at the file:\n\nReading it now.", text)  # its held words show once it carries on
        self.assertTrue(text.endswith("The file says hi."), text)
        self.assertEqual(self.answer(c)["meta"].get("nudges"), 1)

    def test_a_false_alarm_ends_quietly(self):
        c = self.chat("box")
        self.script += [{"text": "Everything passed. Now I will sum up: it all works."}, {"text": "DONE"}]
        evs = self.turn(c)
        self.assertEqual(len(self.bodies), 2)
        self.assertEqual(self.answer(c)["content"], "Everything passed. Now I will sum up: it all works.")  # no DONE shown
        self.assertNotIn("DONE", "".join(e.get("text", "") for e in evs if e["type"] == "delta"))

    def test_calls_past_the_per_reply_cap_are_answered_not_dropped(self):
        c = self.chat("box")
        six = [("run_command", {"command": f"echo {i}"}) for i in range(T.STEP_CALLS + 2)]
        self.script += [{"calls": six}, {"text": "Ran four of them."}]
        evs = self.turn(c)
        self.assertEqual(sum(e["type"] == "tool_done" for e in evs), T.STEP_CALLS)  # only the cap's worth ran
        msgs = self.bodies[1]["messages"]
        said = [m for m in msgs if m.get("tool_calls")][-1]
        self.assertEqual(len(said["tool_calls"]), T.STEP_CALLS + 2)  # every call it made stays in its reply
        results = [m for m in msgs if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], [x["id"] for x in said["tool_calls"]])  # each answered
        self.assertEqual([m["content"] for m in results[T.STEP_CALLS:]], [halo_chat.OVER.format(n=T.STEP_CALLS)] * 2)
        self.assertEqual(self.answer(c)["meta"]["over"], 2)

    def test_a_shown_command_is_stopping_short_and_an_offer_is_not(self):
        """Review "model": it says what it will run and shows the command instead of running it (the page shows
        what looks like a step, then nothing); and the live study's one false alarm, an offer ("say so and I'll")."""
        yes = ["The config looks off. Let me check the logs:\n\n```bash\ntail -50 ~/x.log\n```",
               "I'll run the tests:\n```\npython3 -m unittest\n```\n"]
        no = ["Fixed. To start it yourself, run:\n\n```bash\n./start.sh\n```",
              "Let's look at an example:\n```python\nprint(1)\n```",
              "Here's what I'll change:\n```diff\n- a\n+ b\n```",
              "Let me explain with a snippet:\n```js\nlet x = 1\n```",
              "All 8 tests pass:\n```\nRan 8 tests in 0.001s\nOK\n```",
              "Here's the fix I made:\n```python\ndef tax(self):\n    return 1\n```",
              "Tests pass. The test is still named test_parse; if you expected it renamed too, say so and I'll change it.",
              "Done. Say the word and I'll make the rounding half-up too."]
        self.assertEqual([T.announces(t) for t in yes], [True] * len(yes))
        self.assertEqual([T.announces(t) for t in no], [False] * len(no))

    def test_a_cut_off_reply_carries_on(self):
        c = self.chat("box")
        self.script += [{"text": "The first half and", "finish": "length"}, {"text": " the second half."}]
        self.turn(c)
        self.assertEqual(self.bodies[1]["messages"][-1]["content"], halo_chat.NUDGE["cut"])
        self.assertEqual(self.answer(c)["content"], "The first half and the second half.")

    def test_at_most_twice_in_a_row_then_it_answers_without_tools(self):
        c = self.chat("box")
        self.script += [{"text": "Let me check:"}, {"text": "Let me check again:"}, {"text": "Let me check once more:"},
                        {"text": "I couldn't run the check; the folder is as it was."}]
        evs = self.turn(c)
        # sent back twice, then (never end on a promise) one answer without tools, with a note why
        self.assertEqual(len(self.bodies), 4)
        self.assertEqual(["tools" in b for b in self.bodies], [True, True, True, True])  # offered, never run (its start stays)
        self.assertIn(halo_chat.FINAL_STALLED, self.bodies[3]["messages"][-1]["content"])
        self.assertEqual(self.bodies[3]["messages"][-2]["content"], "Let me check once more:")
        self.assertEqual([e["text"] for e in evs if e["type"] == "note"], [halo_chat.NOTE_STALLED])
        self.assertTrue(self.answer(c)["content"].endswith("I couldn't run the check; the folder is as it was."))
        self.assertIn(T.ACT_NOW, self.bodies[0]["messages"][0]["content"])
        d = self.chat("none")
        n = len(self.bodies)
        self.script += [{"text": "Let me think about that:"}]
        self.turn(d, tools=False)
        self.assertEqual(len(self.bodies), n + 1)  # a chat without tools is never sent back
        self.assertNotIn(T.ACT_NOW, self.bodies[-1]["messages"][0]["content"])

    def test_what_counts_as_announcing(self):
        yes = ["Let me run the tests now.", "Now I will check the file:", "Fixed.\n\nNext, I'll update the README.",
               "Now let me look at the logs...", "I'm going to read config.py to see how the port is set.",
               "Fixed the import. Running the tests now.", "Checking the config.", "Now installing the deps", "On it.",
               "OK, rebuilding with the new flag."]
        no = ["All 12 tests pass. Let me know if you want more.", "Done. The server is up.", "Should I commit this?",
              "I'll leave it running for you.", "The build took 4 s and everything is green.", "",
              "Running the tests showed 3 failures, all in parse(); I fixed them and everything passes now, as shown in the "
              "output above with the full counts.", "The server is running on port 8080.", "Checking it by hand gives the same 42?"]
        self.assertEqual([T.announces(t) for t in yes], [True] * len(yes))
        self.assertEqual([T.announces(t) for t in no], [False] * len(no))
        self.assertEqual([T.said_done(t) for t in ("DONE", "Done.", "", "Done. Also rewrote the whole parser and its tests.")],
                         [True, True, True, False])

    def test_the_allowance_starts_afresh_after_a_call(self):
        """Two stalls early and one late in a long task: the late one is sent back too (the third stall of
        a 96-step allowance ended the answer on "Let me look")."""
        c = self.chat("box")
        self.script += [{"text": "Let me check:"}, {"text": "Let me check again:"},
                        {"calls": [("list_dir", {"path": "."})]},
                        {"text": "Now let me look at the logs."}, {"calls": [("run_command", {"command": "echo logs"})]},
                        {"text": "The folder is empty and the logs say logs."}]
        evs = self.turn(c)
        self.assertEqual(len(self.bodies), 6)
        self.assertTrue(all("tools" in b for b in self.bodies))
        self.assertEqual(self.bodies[4]["messages"][-1]["content"], halo_chat.NUDGE["said"])
        self.assertEqual(len([e for e in evs if e["type"] == "tool_done"]), 2)
        self.assertNotIn("note", [e["type"] for e in evs])
        self.assertEqual(self.answer(c)["meta"]["nudges"], 3)
        self.assertTrue(self.answer(c)["content"].endswith("The folder is empty and the logs say logs."))

    def test_the_answer_without_tools_that_still_calls_is_asked_once_more(self):
        """At the limits the last request asks for the answer (its tools still offered, so the prompt's start stays), but
        DeepSeek may call one anyway: it is dropped, never run, and it is asked once more, then without the tools in view
        (it ended on "Let me check X." with nothing after the tool cards)."""
        c = self.chat("box")
        old = T.MAX_STEPS
        T.MAX_STEPS = 1
        self.addCleanup(setattr, T, "MAX_STEPS", old)
        self.script += [{"calls": [("list_dir", {"path": "."})]},
                        {"text": "Let me check one more thing.", "calls": [("list_dir", {"path": "."})]},
                        {"text": "The folder is empty."}, {"text": "never asked"}]
        evs = self.turn(c)
        self.assertEqual(len(self.bodies), 3)
        self.assertEqual(["tools" in b for b in self.bodies], [True, True, False])
        self.assertEqual(self.bodies[2]["messages"][-1]["content"], halo_chat.NUDGE["final"])
        self.assertEqual(len([e for e in evs if e["type"] == "tool_done"]), 1)  # the dropped call never ran
        self.assertTrue(self.answer(c)["content"].endswith("The folder is empty."))
        # once only: an empty answer after that stands
        self.bodies.clear()
        self.script[:] = [{"calls": [("list_dir", {"path": "."})]}, {"text": ""}, {"text": ""}, {"text": "never asked"}]
        self.turn(c)
        self.assertEqual(len(self.bodies), 3)
        # a real answer with a call after it: the call is dropped, the answer stands (never asked twice)
        self.bodies.clear()
        self.script[:] = [{"calls": [("list_dir", {"path": "."})]},
                          {"text": "The folder is empty, so there is nothing to clean.", "calls": [("list_dir", {"path": "."})]},
                          {"text": "never asked"}]
        self.turn(c)
        self.assertEqual(len(self.bodies), 2)
        self.assertTrue(self.answer(c)["content"].endswith("The folder is empty, so there is nothing to clean."))

    def test_a_cut_off_call_never_runs_and_the_next_reply_gets_more_room(self):
        """A call cut off by the reply cap (a whole file in write_file) ran with its arguments cut (the content gone), or
        was asked to "carry on" in the middle of its markup."""
        T.EXPERIMENTS.discard("longer_replies")  # (with it, a tool step starts at TOOL_REPLY_MAX: no more room to give)
        self.addCleanup(T.EXPERIMENTS.update, T.experiments())
        c = self.chat("box")
        cut = ("Writing it.\n\n<｜DSML｜ calls>\n<｜DSML｜ invoke name=\"write_file\">\n<｜DSML｜ parameter name=\"path\" "
               "string=\"true\">a.py</｜DSML｜ parameter>\n<｜DSML｜ parameter name=\"content\" string=\"true\">print(1)\nprint(2")
        self.script += [{"text": cut, "finish": "length"},
                        {"calls": [("write_file", {"path": "a.py", "content": "print(1)\nprint(2)\n"})]}, {"text": "Wrote a.py."}]
        evs = self.turn(c)
        self.assertEqual([e["line"] for e in evs if e["type"] == "tool_done"], ["Wrote a.py"])
        self.assertEqual(self.bodies[1]["messages"][-1]["content"], halo_chat.NUDGE["cut_call"])
        self.assertEqual(self.bodies[1]["max_tokens"], 2 * self.bodies[0]["max_tokens"])
        with open(os.path.join(self.box_root, c["id"], "a.py")) as f:
            self.assertEqual(f.read(), "print(1)\nprint(2)\n")
        self.assertNotIn("DSML", self.answer(c)["content"])

    def test_a_nudge_is_in_the_log_by_its_shape_never_its_words(self):
        import contextlib
        import io
        c = self.chat("box")
        self.script += [{"text": "Let me open the plum-tree file:"},
                        {"text": "Opening.\n<tool_call>\n{\"name\": \"read_file\", \"arguments\": {\"path\": \"plum\"\n</tool_call>"},
                        {"text": "There is no such file."}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.turn(c)
        out = buf.getvalue()
        self.assertIn("chat: a reply stopped short (said) at step 1 after 0 call(s)", out)
        self.assertIn("chat: a reply stopped short (garbled, tool_call) at step 2", out)
        self.assertNotIn("plum", out)
        self.assertEqual(self.bodies[2]["messages"][-1]["content"], halo_chat.NUDGE["garbled"])


DSML_LS = ("\n<｜DSML｜ calls>\n<｜DSML｜ invoke name=\"run_command\">\n<｜DSML｜ parameter name=\"command\" "
           "string=\"true\">echo hi</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n</｜DSML｜ calls>\n")


class CallsAsWords(Base):
    """A call the engine hands back as the reply's words:
    DeepSeek's DSML not exactly in the engine's format. It runs, and the page never shows the markup."""

    def test_a_call_written_out_as_words_runs(self):
        c = self.chat("box")
        self.script += [{"texts": ["Now the list.\n<", DSML_LS[2:40], DSML_LS[40:]]}, {"text": "The file says hi."}]
        evs = self.turn(c)
        self.assertEqual(len(self.bodies), 2)
        said = self.bodies[1]["messages"][-2]
        self.assertEqual(said["content"], "Now the list.")
        self.assertEqual(said["tool_calls"][0]["function"], {"name": "run_command", "arguments": json.dumps({"command": "echo hi"})})
        self.assertIn("tool_done", [e["type"] for e in evs])
        shown = "".join(e.get("text", "") for e in evs if e["type"] == "delta")
        self.assertNotIn("｜", shown)
        text = self.answer(c)["content"]
        self.assertTrue(text.startswith("Now the list.") and text.endswith("The file says hi."), text)
        self.assertNotIn("DSML", text)

    def test_a_garbled_call_is_made_again(self):
        c = self.chat("box")
        self.script += [{"text": "Checking.\n\n<｜DSML｜ calls>\n<｜DSML｜ invoke nam"},
                        {"text": "", "calls": [("run_command", {"command": "echo hi"})]}, {"text": "It says hi."}]
        self.turn(c)
        self.assertEqual(len(self.bodies), 3)
        self.assertEqual(self.bodies[1]["messages"][-1]["content"], halo_chat.NUDGE["garbled"])
        self.assertEqual(self.bodies[1]["messages"][-2]["content"], "Checking.")
        text = self.answer(c)["content"]
        self.assertNotIn("DSML", text)
        self.assertTrue(text.endswith("It says hi."), text)

    def test_other_words_stand(self):
        c = self.chat("box")
        self.script += [{"texts": ["If a < b then x", "<", "y holds."]}]
        self.turn(c)
        self.assertEqual(self.answer(c)["content"], "If a < b then x<y holds.")
        words = "Plain words, no markup <｜end｜> here."
        self.assertEqual(T.calls_in_text(words), (words, [], False))
        two = (DSML_LS.replace("echo hi", "a").replace("</｜DSML｜ calls>", "")
               + "<｜DSML｜ invoke name=\"write\">\n<｜DSML｜ parameter name=\"n\" string=\"false\">[1, 2]"
               "</｜DSML｜ parameter>\n<｜DSML｜ parameter name=\"x\" string=\"false\">not json</｜DSML｜ "
               "parameter>\n</｜DSML｜ invoke>\n</｜DSML｜ calls>\nand words after")
        w, calls, garbled = T.calls_in_text("Two calls:" + two)
        self.assertEqual((w, garbled), ("Two calls:", False))
        self.assertEqual([(x["name"], json.loads(x["args"])) for x in calls],
                         [("run_command", {"command": "a"}), ("write", {"n": [1, 2], "x": "not json"})])

    def test_other_forms_of_a_call_run(self):
        """Hypothesis (a): a call in a form neither the engine nor the loop read ended the answer as words that
        look like a call. DSML with plain bars and DeepSeek V3's tool tokens run wherever they start; the page never
        keeps their markup."""
        forms = [
            ("Now the list.\n\n<|DSML| calls>\n<|DSML| invoke name=\"list_dir\">\n<|DSML| parameter name=\"path\" "
             "string=\"true\">.</|DSML| parameter>\n</|DSML| invoke>\n</|DSML| calls>", False),
            ("Now the list.<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>list_dir\n```json\n"
             "{\"path\": \".\"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>", False),
        ]
        for text, shown in forms:
            with self.subTest(text=text[14:40]):
                c = self.chat("box")
                self.bodies.clear()
                self.script += [{"texts": [text[:20], text[20:]]}, {"text": "It is empty."}]
                evs = self.turn(c)
                self.assertEqual([e["line"] for e in evs if e["type"] == "tool_done"], ["Listed ."])
                said = self.bodies[1]["messages"][-2]
                self.assertEqual((said["content"], said["tool_calls"][0]["function"]["name"]), ("Now the list.", "list_dir"))
                self.assertEqual(json.loads(said["tool_calls"][0]["function"]["arguments"]), {"path": "."})
                self.assertEqual(self.answer(c)["content"], "Now the list.\n\nIt is empty.")
                # the markup never reaches the page; a JSON call (not held back as it comes) is taken back
                first = evs[:[e["type"] for e in evs].index("tool")]
                page = "".join(e.get("text", "") for e in first if e["type"] == "delta")
                back = sum(e["chars"] for e in first if e["type"] == "retract")
                self.assertEqual(page[:len(page) - back], "Now the list.")
                self.assertEqual(back > 2, shown)

    OTHER = ["<tool_call>\n{\"name\": \"list_dir\", \"arguments\": {\"path\": \".\"}}\n</tool_call>",
             "<function_calls>\n<invoke name=\"list_dir\">\n<parameter name=\"path\">.</parameter>\n</invoke>\n</function_calls>",
             "<tool_call>\n<function=list_dir>\n<parameter=path>\n.\n</parameter>\n</function>\n</tool_call>",
             "```json\n{\"name\": \"list_dir\", \"arguments\": {\"path\": \".\"}}\n```"]

    def test_another_familys_call_runs_only_as_the_whole_reply(self):
        """Gate review: a call in another family's form at the end of an answer may be an example it quoted. It
        runs only when it is the whole reply; after words of its own it is sent back once (NUDGE "stray"), its words
        stand, and nothing runs until the model makes the call with its tools."""
        for block in self.OTHER:
            with self.subTest(block=block[:24]):
                c = self.chat("box")
                self.bodies.clear()
                self.script += [{"text": block}, {"text": "It is empty."}]
                evs = self.turn(c)
                self.assertEqual([e["line"] for e in evs if e["type"] == "tool_done"], ["Listed ."])
                self.assertEqual(self.answer(c)["content"], "It is empty.")
                self.bodies.clear()
                self.script += [{"text": "Now the list.\n" + block}, {"calls": [("list_dir", {"path": "."})]},
                                {"text": "It is empty."}]
                evs = self.turn(c)
                self.assertEqual(self.bodies[1]["messages"][-1]["content"], halo_chat.NUDGE["stray"])
                self.assertEqual([e["line"] for e in evs if e["type"] == "tool_done"], ["Listed ."])  # the real call
                self.assertEqual(len(self.bodies), 3)

    def test_a_retract_counts_the_pages_units(self):
        """A whole-reply JSON call is shown as it streams, then taken back when it runs. The page counts UTF-16 units (an
        emoji is 2): it must take back exactly what it showed, emoji and all."""
        c = self.chat("box")
        text = json.dumps({"name": "list_dir", "arguments": {"path": "\U0001F4C1"}}, ensure_ascii=False)
        self.script += [{"texts": [text[:12], text[12:]]}, {"text": "It is empty."}]
        evs = self.turn(c)
        first = evs[:[e["type"] for e in evs].index("tool")]
        page = "".join(e.get("text", "") for e in first if e["type"] == "delta").encode("utf-16-le")
        back = sum(e["chars"] for e in first if e["type"] == "retract")
        self.assertEqual(page[:max(0, len(page) - 2 * back)].decode("utf-16-le"), "")  # the page keeps none of it
        self.assertEqual(self.answer(c)["content"], "It is empty.")

    def test_a_tag_mentioned_in_prose_is_no_call(self):
        """Gate review: "models emit `<invoke name=...>` blocks" in a last paragraph was cut there and the model
        told its call came out garbled. A tag counts only when it starts a line."""
        c = self.chat("box")
        for text in ('Claude-style models emit `<invoke name="run_command">` blocks, which the app parses into calls.',
                     "Its reader handles `<function=read_file>` too, then hands the result back.",
                     'They wrap it as <tool_call> with "name": "list_dir" inside, which the app reads.'):
            self.bodies.clear()
            self.script.append({"text": text})
            evs = self.turn(c)
            self.assertEqual(len(self.bodies), 1, text)  # not sent back
            self.assertNotIn("tool", [e["type"] for e in evs])
            self.assertEqual(self.answer(c)["content"], text)

    def test_a_quoted_example_never_runs(self):
        """The gate's reproduction: an example call at the end of an answer created a file and was cut from the user's
        answer. It must stay in the answer and run nothing."""
        c = self.chat("box")
        ex = ("A run_command call in OpenAI's format looks like this:\n```json\n"
              + json.dumps({"name": "run_command", "arguments": {"command": "echo QUOTED > q.txt"}}) + "\n```")
        self.script += [{"text": ex}, {"text": "DONE"}]
        evs = self.turn(c)
        self.assertNotIn("tool", [e["type"] for e in evs])
        self.assertEqual(self.answer(c)["content"], ex)
        self.assertFalse(os.path.exists(os.path.join(self.box_root, "q.txt")))
        self.assertFalse(any(f == "q.txt" for _, _, fs in os.walk(self.tmp) for f in fs))

    def test_words_held_before_a_written_call_are_kept(self):
        """Gate review: the hold began at an earlier "<|" in prose (Elm), a written call ended the reply, and the words
        between were never shown or saved ("In Elm you write `f It is empty.")."""
        c = self.chat("box")
        text = "In Elm you write `f <| x` for that. Now the list." + DSML_LS.replace("｜", "|")
        self.script += [{"texts": [text[:20], text[20:]]}, {"text": "It is empty."}]
        evs = self.turn(c)
        self.assertEqual(sum(e["type"] == "tool_done" for e in evs), 1)  # the written call ran
        self.assertIn("In Elm you write `f <| x` for that. Now the list.", self.answer(c)["content"])

    def test_examples_and_other_json_stay_words(self):
        c = self.chat("box")
        example = ("A call looks like this:\n<invoke name=\"run_command\">\n<parameter name=\"command\">ls</parameter>\n"
                   "</invoke>\n\nThe app runs it and hands back what it printed.")
        config = "The config:\n```json\n{\"name\": \"halo\", \"port\": 8080}\n```"
        for text in (example, config):
            self.script.append({"text": text})
            evs = self.turn(c)
            self.assertNotIn("tool", [e["type"] for e in evs])
            self.assertEqual(self.answer(c)["content"], text)
        self.assertEqual(len(self.bodies), 2)  # neither was sent back
        # a reply that ENDS on a call to a tool it doesn't have is sent back once (stray_call); an example stands
        other = "Here it is:\n```json\n{\"name\": \"get_weather\", \"arguments\": {\"city\": \"Paris\"}}\n```"
        self.script += [{"text": other}, {"text": "DONE"}]
        evs = self.turn(c)
        self.assertEqual(len(self.bodies), 4)
        self.assertEqual(self.bodies[3]["messages"][-1]["content"], halo_chat.NUDGE["stray"])
        self.assertNotIn("tool", [e["type"] for e in evs])
        self.assertEqual(self.answer(c)["content"], other)  # its words stand, no DONE

    def test_direct_access_on_the_vps(self):
        """Direct access on the VPS: one reply that looked like a call, then the answer ended (no
        second request, nothing in the log). Each form now reaches run_command (the demo app refuses to run it), and a
        call to a tool it doesn't have ("bash") is sent back and logged by its shape."""
        import contextlib
        import io
        call = {"command": "uptime", "summary": "Check the VPS"}
        forms = ["Checking.\n\n<|DSML| calls>\n<|DSML| invoke name=\"run_command\">\n<|DSML| parameter name=\"command\" "
                 "string=\"true\">uptime</|DSML| parameter>\n<|DSML| parameter name=\"summary\" string=\"true\">Check the VPS"
                 "</|DSML| parameter>\n</|DSML| invoke>\n</|DSML| calls>",
                 "Checking.\n<tool_call>\n" + json.dumps({"name": "run_command", "arguments": call}) + "\n</tool_call>",
                 "Checking.\n```json\n" + json.dumps({"name": "run_command", "arguments": call}) + "\n```",
                 "Checking.\n<invoke name=\"run_command\">\n<parameter name=\"command\">uptime</parameter>\n<parameter "
                 "name=\"summary\">Check the VPS</parameter>\n</invoke>"]
        for text in forms + ["stray"]:
            with self.subTest(text=text[10:30]):
                c = self.st.create_chat(kind="chat", model="unc-chat")
                self.st.set_perm(c["id"], "direct", {"computer": "vps"})
                self.bodies.clear()
                back = text == "stray" or not text.startswith("Checking.\n\n<|DSML|")  # another family's form after words
                if text == "stray":
                    self.script[:] = [{"text": "Checking.\n<tool_call>\n{\"name\": \"bash\", \"arguments\": {\"command\": "
                                               "\"uptime\"}}\n</tool_call>"}, {"calls": [("run_command", call)]},
                                      {"text": "The VPS is up."}]
                elif back:
                    self.script[:] = [{"text": text}, {"calls": [("run_command", call)]}, {"text": "The VPS is up."}]
                else:
                    self.script[:] = [{"text": text}, {"text": "The VPS is up."}]
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    evs = self.turn(c, profile="unc-chat")
                self.assertEqual([e["name"] for e in evs if e["type"] == "tool"], ["run_command"])
                said = next(m for m in reversed(self.bodies[-1]["messages"]) if m.get("tool_calls"))
                self.assertEqual(json.loads(said["tool_calls"][0]["function"]["arguments"]), call)
                self.assertEqual(len(self.bodies), 3 if back else 2)
                self.assertTrue(self.answer(c)["content"].endswith("The VPS is up."))
                if back:
                    self.assertEqual(self.bodies[1]["messages"][-1]["content"], halo_chat.NUDGE["stray"])
                if text == "stray":
                    self.assertEqual(self.bodies[1]["messages"][-1]["content"], halo_chat.NUDGE["stray"])
                    self.assertIn("chat: a reply stopped short (stray, tool_call) at step 1", buf.getvalue())
                    self.assertNotIn("uptime", buf.getvalue())

    def test_a_json_call_after_a_nudge_runs_and_nothing_is_taken_back(self):
        c = self.chat("box")
        self.script += [{"text": "Let me list it:"},
                        {"text": "{\"name\": \"list_dir\", \"arguments\": {\"path\": \".\"}}"}, {"text": "It is empty."}]
        evs = self.turn(c)
        self.assertEqual([e["line"] for e in evs if e["type"] == "tool_done"], ["Listed ."])
        self.assertNotIn("retract", [e["type"] for e in evs])  # its words waited (NUDGE "said"): nothing was shown
        self.assertEqual(self.answer(c)["content"], "Let me list it:\n\nIt is empty.")


class Robust(Base):
    """What else ended an answer right after a tool call."""

    def test_a_toolset_that_gives_back_nothing_is_a_failed_call(self):
        c = self.chat("box")
        real = chat_box.BoxTools.call
        chat_box.BoxTools.call = lambda self_, name, args, ctx: None if name == "list_dir" else real(self_, name, args, ctx)
        self.addCleanup(setattr, chat_box.BoxTools, "call", real)
        self.script += [{"calls": [("list_dir", {"path": "."})]}, {"text": "The listing failed."}]
        evs = self.turn(c)
        self.assertEqual([(e["status"], e["line"]) for e in evs if e["type"] == "tool_done"], [("failed", "list_dir failed")])
        self.assertNotIn("error", [e["type"] for e in evs])
        self.assertEqual(self.answer(c)["content"], "The listing failed.")

    def test_a_bug_after_a_tool_call_keeps_its_reason_in_the_answer(self):
        """Before, the answer stored the tool call and nothing else (no error): after a reload the page showed a tool call
        and then nothing, and only the event queue (gone with the page) had the error."""
        import contextlib
        import io
        c = self.chat("box")
        self.script += [{"calls": [("list_dir", {"path": "."})]}, {"text": "never asked"}]
        self.agent._take_steers = lambda cid, tctx: {}["the user's words"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            evs = self.turn(c)
        self.assertEqual([e["type"] for e in evs][-2:], ["error", "done"])
        m = self.answer(c)
        self.assertIn("StrixFold UI hit a problem and stopped this answer (KeyError)", m["meta"]["error"])
        self.assertEqual([list(p)[0] for p in m["meta"]["parts"]], ["tool"])
        self.assertRegex(buf.getvalue(), r"chat: a turn died: KeyError at .* < halo_chat\.py:\d+ _tool_loop")
        self.assertIn("chat: a turn ended on an error at step 1 after 1 call(s) (last: list_dir)", buf.getvalue())
        self.assertNotIn("the user's words", buf.getvalue())
        self.assertFalse(self.relay.busy(c["id"]))

    def test_unreadable_arguments_are_said_never_run_empty(self):
        c = self.chat("box")
        self.script += [{"calls": [("write_file", "{\"path\": \"a.txt\", \"content\": \"hi}")]},
                        {"calls": [("write_file", {"path": "a.txt", "content": "hi"})]}, {"text": "Wrote it."}]
        evs = self.turn(c)
        self.assertEqual([e["status"] for e in evs if e["type"] == "tool_done"], ["refused", "ok"])
        self.assertIn("not a valid JSON object", self.bodies[1]["messages"][-1]["content"])

    def test_a_repeat_now_and_then_in_a_long_task_is_not_a_loop(self):
        c = self.chat("box")
        ls = {"calls": [("list_dir", {"path": "."})]}
        self.script += [ls, ls, {"calls": [("run_command", {"command": "echo a"})]}, ls,
                        {"calls": [("run_command", {"command": "echo b"})]}, ls, {"text": "All checked."}]
        evs = self.turn(c)
        self.assertEqual(len(self.bodies), 7)
        self.assertIn("tools", self.bodies[-1])  # three repeats, but never in a row: tools stay on
        self.assertNotIn("note", [e["type"] for e in evs])

    def test_a_prompt_the_engine_counts_longer_is_fitted_again(self):
        """Tool output (code, JSON) takes more tokens a character than the chat's words: the engine refused the step as
        longer than the window, and the answer ended there on an error after its tool calls."""
        import contextlib
        import io
        c = self.chat("box")
        self.script += [{"calls": [("list_dir", {"path": "."})]},
                        {"status": 400, "error": "maximum context length is 63488 tokens, but the rendered prompt has 64000 tokens"},
                        {"text": "The folder is empty."}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            evs = self.turn(c)
        self.assertNotIn("error", [e["type"] for e in evs])
        self.assertEqual(len(self.bodies), 3)
        self.assertIn("fitted again", buf.getvalue())
        self.assertEqual(self.answer(c)["content"], "The folder is empty.")

    def test_the_estimate_counts_a_calls_arguments(self):
        file_call = {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": "a.py", "content": "x = 1\n" * 5000})}}]}
        self.assertGreater(halo_chat.est_tokens([file_call]), 9000)


class TurnLog(Base):
    """the page showed why, the app's log nothing. A turn that dies, ends on an error or is stopped leaves a line there:
    where, never a chat's words."""

    def test_an_error_after_a_tool_call_is_in_the_log(self):
        import contextlib
        import io
        c = self.chat()
        self.script += [{"calls": [("list_dir", {"path": "."})]}] + [{"status": 500, "error": "engine fell over"}] * 3
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            evs = self.turn(c)
        self.assertIn("error", [e["type"] for e in evs])
        line = [x for x in buf.getvalue().splitlines() if "chat: a turn" in x]
        self.assertEqual(len(line), 1, buf.getvalue())
        self.assertIn("chat: a turn ended on an error at step 2 after 1 call(s) (last: list_dir): the model answered:", line[0])

    def test_a_turn_that_dies_says_where_never_its_words(self):
        import contextlib
        import io

        def gen():
            yield {"type": "start"}
            raise KeyError("the user's private words")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            q = halo_chat.run_detached(gen(), {"type": "first"})
            evs = []
            while True:
                ev = q.get(timeout=5)
                if ev is None:
                    break
                evs.append(ev)
        self.assertEqual(evs[-1]["type"], "error")
        out = buf.getvalue()
        self.assertIn("chat: a turn died: KeyError at test_chat_tools.py:", out)
        self.assertIn(" gen", out)
        self.assertNotIn("private words", out)

    def test_private_and_quiet_ends(self):
        import contextlib
        import io
        buf = io.StringIO()
        priv = types.SimpleNamespace(is_private=lambda cid: True)
        with contextlib.redirect_stdout(buf):
            halo_chat.log_end(priv, "c1", "the model answered: secret", False, 3, 2, "run")
            halo_chat.log_end(priv, "c1", None, True)
            halo_chat.log_end(priv, "c1", None, False)  # a turn that ended well: nothing
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("chat: a turn ended on an error at step 3 after 2 call(s) (last: run) (Private)"))
        self.assertTrue(lines[1].endswith("chat: a turn was stopped"))


class OnTheHalos(Base):
    """The conductor sees each of a chat's requests on its pair (a lease), keeps the main boxes from agents between the
    steps of its turn, and lends them again when the turn ends (review)."""

    def setUp(self):
        super().setUp()
        import conductor
        st = {"state": "ready", "profile": {"id": "unc-chat-256k", "model_id": "dsv41-unc",
                                            "settings": {"streams": 1, "context": 262144}}}
        self.halos = st
        app = types.SimpleNamespace(pair_states=lambda fresh=False: [("halos", "box1 + box2", st)],
                                    local_endpoint=lambda s_: self.ep)
        self.cond, old = conductor.Conductor(app), conductor.C
        conductor.C = self.cond
        self.addCleanup(setattr, conductor, "C", old)

    def test_a_turn_keeps_the_halos_between_its_steps_and_lets_go_at_its_end(self):
        c = self.chat("box")
        seen = []
        hook = lambda: seen.append((self.cond.busy("halos", "chat"), c["id"] in self.cond.between))  # noqa: E731
        self.script += [{"calls": [("list_dir", {"path": "."})], "hook": hook}, {"text": "Done.", "hook": hook}]
        self.turn(c)
        self.assertEqual(seen, [(1, False), (1, True)])  # each step counts; after the first, its turn holds them
        self.assertEqual(self.cond.between, {})  # the turn ended: lent again at once
        self.assertEqual(self.cond.choose("dsv41-unc", "agent", "a0")[0], "halos")

    def test_a_plain_answer_and_a_summary_count_on_their_pair(self):
        """A chat with no tools (the default level, Send without tools) and a compaction's summary are requests on the
        main boxes too: they took no lease, so agents were lent the stream while the user's answer streamed there."""
        seen = []
        hook = lambda: seen.append(self.cond.busy("halos", "chat"))  # noqa: E731 - while the engine answers
        c = self.chat("none")
        self.script.append({"text": "Hello.", "hook": hook})
        self.turn(c)
        self.assertEqual(seen, [1])
        self.script.append({"text": "The summary.", "hook": hook})
        self.assertEqual(self.agent._summarise(self.ep, "served", "", ["the user: hi"], 4000, c["id"]), "The summary.")
        self.assertEqual(seen, [1, 1])
        self.assertEqual(self.cond.busy("halos"), 0)


class Speed(Base):
    """An answer's speeds from the engine's own clock (meta.speed, the footer), and every pair's last hour (speed_view)."""

    def test_an_answer_keeps_its_speeds(self):
        c = self.chat("box")
        u1 = {"prompt_tokens": 20000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}
        u2 = {"prompt_tokens": 20300, "completion_tokens": 300, "prompt_tokens_details": {"cached_tokens": 20000}}
        self.script += [{"text": "Checking.", "calls": [("run_command", {"command": "echo hi"})], "usage": u1,
                         "engine": {"tokens_per_second": 20.0, "prefill_seconds": 10.0, "time_to_first_token": 12.0}},
                        {"text": "It says hi.", "usage": u2,
                         "engine": {"tokens_per_second": 30.0, "prefill_seconds": 0.3, "time_to_first_token": 0.3}}]
        self.turn(c)
        meta = self.answer(c)["meta"]
        self.assertEqual([(x["tps"], x["pp"], x["wait"]) for x in meta["speed_steps"]], [(20.0, 2000, 2.0), (30.0, 1000, 0.0)])
        sp = meta["speed"]
        self.assertEqual((sp["tps"], sp["out"], sp["steps"], sp["wait"], sp["in"], sp["cached"]), (26.7, 400, 2, 2.0, 20300, 20000))
        self.assertEqual(sp["pp"], round(20300 / 10.3))

    def test_every_pair_last_hour(self):
        import studio_http
        now = time.time()
        rows = [{"t": now - 60, "profile": "unc-swarm", "prompt": 30000, "cached": 0, "tokens": 400, "decode_tps": 20.0,
                 "prefill_tps": 900.0, "ttft_s": 40.0, "prefill_s": 33.0},
                {"t": now - 30, "profile": "unc-swarm", "prompt": 30500, "cached": 30000, "tokens": 300, "decode_tps": 30.0,
                 "prefill_tps": 1100.0, "ttft_s": 0.6, "prefill_s": 0.5},
                {"t": now - 20, "profile": "unc-swarm", "prompt": 10, "cached": 0, "tokens": 4, "decode_tps": 99.0, "probe": 1},
                {"t": now - 7200, "profile": "unc-swarm", "prompt": 9000, "cached": 0, "tokens": 300, "decode_tps": 5.0},
                {"t": now - 10, "profile": "unc-chat", "prompt": 4000, "cached": 0, "tokens": 50, "decode_tps": 50.0,
                 "prefill_tps": 1500.0, "ttft_s": 2.7, "prefill_s": 2.7}]
        ready = lambda pid: {"state": "ready", "profile": {"id": pid, "name": pid}}  # noqa: E731
        A = types.SimpleNamespace(
            pair_states=lambda: [("halos", "box1 + box2", ready("unc-chat")), ("box-b", "box5 + box4", ready("unc-swarm")),
                                 ("box-c", "box3 + box6", None)],
            pair_get=lambda name, path: {"requests": rows} if name == "box-b" else {"requests": []})
        v = studio_http.speed_view(A)
        by = {(p["pair"], p["profile"]): p for p in v["pairs"]}
        b = by[("box-b", "unc-swarm")]
        self.assertEqual((b["n"], b["tps"], b["tps_best"], b["pp"], b["wait"], b["now"]), (2, 25.0, 30.0, 1000.0, 3.5, True))
        self.assertEqual(by[("box-b", "unc-chat")]["now"], False)  # an earlier profile on the same pair stays apart
        self.assertEqual((by[("halos", "unc-chat")]["n"], by[("box-c", None)]["state"]), (0, "unreachable"))


class Steer(Base):
    """A message sent while the chat answers waits in the outbox (meta.steer); a turn with tools takes it in at its next
    step, and a new answer goes on from there with everything the first one did."""

    def steer(self, c, text, **meta):
        return lambda: self.st.enqueue(c["id"], text, dict({"attachments": [], "effort": None, "steer": True}, **meta))

    def test_a_message_sent_mid_turn_goes_in_after_the_running_step(self):
        c = self.chat("box")
        self.script += [{"text": "Checking.", "calls": [("run_command", {"command": "echo one"})], "hook": self.steer(c, "also say two")},
                        {"text": "One, and two."}]
        evs = self.turn(c)
        kinds = [e["type"] for e in evs]
        self.assertNotIn("error", kinds, evs)
        self.assertEqual([k for k in kinds if k in ("user", "start", "tool_done", "done")], ["user", "start", "tool_done", "user", "start", "done"])
        cut = next(e for e in evs[1:] if e["type"] == "user")
        self.assertEqual((cut["content"], cut["meta"]["steer"]), ("also say two", True))
        self.assertTrue(cut["from_outbox"])
        msgs = self.st.messages(c["id"])
        self.assertEqual([(m["role"], m["content"]) for m in msgs],
                         [("user", "do it"), ("assistant", "Checking."), ("user", "also say two"), ("assistant", "One, and two.")])
        first, second = msgs[1]["meta"], msgs[3]["meta"]
        self.assertNotIn("partial", first)
        self.assertEqual([p["tool"]["status"] for p in first["parts"] if p.get("tool")], ["ok"])
        self.assertEqual((first["calls"], first["stopped"]), (1, False))
        self.assertTrue(second["steered"])
        self.assertEqual((second["calls"], second["level"]), (0, "box"))
        self.assertNotIn("partial", second)
        # the model read the user's words inside the command's result, its turn going on (Claude Code's way: a user turn
        # there made the engine's template drop the answer's reasoning so far), with its tools still offered
        last = self.bodies[1]["messages"][-1]
        self.assertEqual(last["role"], "tool")
        self.assertTrue(last["content"].endswith(halo_chat.STEER_IN + "also say two" + halo_chat.STEER_OUT), last["content"])
        self.assertNotIn("user", [m["role"] for m in self.bodies[1]["messages"][2:]])
        self.assertIn("tools", self.bodies[1])
        self.assertEqual(self.st.waiting(c["id"]), [])
        done = [e for e in evs if e["type"] == "done"][-1]
        self.assertEqual(done["id"], msgs[3]["id"])

    def test_a_message_sent_while_it_writes_its_answer_keeps_it_going(self):
        c = self.chat("box")
        self.script += [{"text": "Done.", "hook": self.steer(c, "now in French")}, {"text": "Fini."}]
        self.turn(c)
        self.assertEqual([(m["role"], m["content"]) for m in self.st.messages(c["id"])],
                         [("user", "do it"), ("assistant", "Done."), ("user", "now in French"), ("assistant", "Fini.")])
        self.assertEqual(self.bodies[1]["messages"][-2:], [{"role": "assistant", "content": "Done."}, {"role": "user", "content": "now in French"}])
        self.assertEqual(self.st.waiting(c["id"]), [])

    def test_send_now_leaves_their_message_to_go_on_its_own(self):
        """Send now (chats.Relay.send_now): the message it was pressed on is never taken in at a step's end,
        so the stop that follows can't cut off the answer to it; it waits for the outbox, as after any stop."""
        c = self.chat("box")

        def hook():
            oid = self.steer(c, "stop and do this instead")()
            self.relay.now[oid] = next(w["created"] for w in self.st.waiting(c["id"]) if w["id"] == oid)  # pressed just as
            # the step ends: marked before the step looks
        self.script += [{"text": "Checking.", "calls": [("run_command", {"command": "echo one"})], "hook": hook}, {"text": "Done."}]
        self.turn(c)
        self.assertEqual([m["role"] for m in self.st.messages(c["id"])], ["user", "assistant"])
        self.assertEqual([w["content"] for w in self.st.waiting(c["id"])], ["stop and do this instead"])
        self.assertNotIn("stop and do this instead", json.dumps(self.bodies[1]["messages"]))

    def test_send_now_after_a_step_took_it_in_stops_nothing(self):
        relay, stops = chats.Relay(), []
        entry = relay.claim("c1")
        self.assertEqual(relay.send_now(7, lambda oid: None), (None, False))  # taken in already: the answer goes on
        self.assertFalse(entry["stop"].is_set())
        self.assertEqual(relay.send_now(8, lambda oid: {"chat_id": "c1", "created": 5.0}), ("c1", True))  # still waiting:
        self.assertTrue(entry["stop"].is_set())  # marked, then the answer stops
        self.assertEqual(relay.now, {8: 5.0})
        self.assertTrue(relay.is_now({"id": 8, "created": 5.0}))
        self.assertFalse(relay.is_now({"id": 8, "created": 6.0}))  # a later message that got the same id back
        self.assertEqual(relay.send_now(9, lambda oid: {"chat_id": "c2", "created": 7.0}), ("c2", False))  # no answer
        # running: the outbox sends it

    def test_their_message_gives_the_answer_a_fresh_allowance(self):
        c = self.chat("box")
        real = T.MAX_STEPS
        T.MAX_STEPS = 1
        self.addCleanup(setattr, T, "MAX_STEPS", real)
        self.script += [{"calls": [("run_command", {"command": "echo a"})], "hook": self.steer(c, "and b")},
                        {"calls": [("run_command", {"command": "echo b"})]}, {"text": "a and b"}]
        self.turn(c)
        self.assertIn("tools", self.bodies[1])  # a step 2 of the first answer would have had to answer without tools
        self.assertIn(halo_chat.FINAL_SPENT, self.bodies[2]["messages"][-1]["content"])  # the new answer's own allowance, used up
        self.assertEqual(self.answer(c)["content"], "a and b")

    def test_a_message_with_more_than_words_waits_for_the_answer_to_end_in_order(self):
        c = self.chat("box")
        hook = lambda: (self.steer(c, "plainly please", no_tools=True)(), self.steer(c, "and quickly")())  # noqa: E731
        self.script += [{"text": "Done.", "hook": hook}]
        self.turn(c)
        self.assertEqual([m["role"] for m in self.st.messages(c["id"])], ["user", "assistant"])
        self.assertEqual([w["content"] for w in self.st.waiting(c["id"])], ["plainly please", "and quickly"])  # the outbox sends them

    def test_one_cancelled_meanwhile_is_skipped_and_stop_leaves_them_waiting(self):
        c = self.chat("box")
        oid = {}
        self.script += [{"text": "Done.", "hook": lambda: oid.update(a=self.steer(c, "never mind")())}]
        real = self.st.add_message

        def cancel_first(cid, role, content, meta=None, version=None, from_outbox=None):
            if from_outbox == oid.get("a"):
                self.st.unqueue(from_outbox)  # the user pressed Cancel just as the step ended
            return real(cid, role, content, meta, version, from_outbox)
        self.st.add_message = cancel_first
        self.turn(c)
        self.assertEqual([m["role"] for m in self.st.messages(c["id"])], ["user", "assistant"])
        self.assertEqual(len(self.bodies), 1)
        # Stop: whatever waits is not taken in; the outbox sends it once the chat is free
        self.st.add_message = real
        c2 = self.chat("box")
        entry = self.relay.claim(c2["id"])
        entry["stop"].set()
        self.steer(c2, "after the stop")()
        list(self.agent.turn(c2["id"], self.ep, "served", "quick-chat", "go", None, claim=entry))
        self.assertEqual([w["content"] for w in self.st.waiting(c2["id"])], ["after the stop"])


class Before(Base):
    def test_before_runs_ahead_of_the_request_and_its_meta(self):
        """A toolset's before() runs before the turn's request is built (chat_look waits there for a photo's description,
        which must then be in the request): its events reach the page before "start", a note it yields is kept in
        meta.parts, and the answer counts a photo that reached a text-only model as a description ("described")."""
        st = self.st
        u = st.add_media(mediainfo.test_pattern(64, 48, 3), "p.png")
        st.set_upload_text(u["id"], "before the wait", "64×48 · OCR · 3 words")

        class Waits(T.Toolset):
            def specs(self):
                return [{"type": "function", "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}}}]

            def before(self, ctx):
                yield {"type": "looking", "photos": 1}
                st.set_upload_text(u["id"], "made during the wait", "64×48 · OCR · 4 words")
                yield {"type": "note", "text": "Looked at 1 photo"}

        plug, desc = types.ModuleType("chat_look"), types.ModuleType("look")
        plug.toolset = lambda ctx: Waits()
        desc.look_text = lambda store, uid: "a description" if uid == u["id"] else None
        for name, mod in (("chat_look", plug), ("look", desc)):
            self.addCleanup(lambda n=name, old=sys.modules.get(name): sys.modules.__setitem__(n, old) if old else sys.modules.pop(n, None))
            sys.modules[name] = mod
        c = self.chat("none")
        self.script.append({"text": "Done."})
        evs = self.turn(c, "what is it?", [{"id": u["id"]}])
        self.assertEqual([e["type"] for e in evs][:4], ["user", "looking", "note", "start"])
        self.assertIn("made during the wait", self.bodies[0]["messages"][-1]["content"])
        self.assertNotIn("before the wait", self.bodies[0]["messages"][-1]["content"])
        a = self.answer(c)
        self.assertEqual(a["meta"]["parts"][0], {"note": "Looked at 1 photo"})
        self.assertEqual((a["meta"]["described"], a["meta"]["photo_text"]), (1, 0))
        self.assertEqual(a["content"], "Done.")


class Asks(Base):
    """A tap (Full's toolset uses it; a stand-in toolset here): the loop shows it, waits, and Stop or the timeout denies."""

    def setUp(self):
        super().setUp()
        mod = types.ModuleType("chat_full")

        class Tap(T.Toolset):
            level = "full"

            def specs(self):
                return [{"type": "function", "function": {"name": "risky", "description": "x", "parameters": {"type": "object", "properties": {}}}}]

            def call(self, name, args, ctx):
                d = ctx.ask({"command": "rm -rf build", "folder": "~/HaloChats/x", "why": "deletes files"})
                return T.result(f"decision {d}", f"risky · {d}", "ok" if d == "allow" else "denied", "tap_allowed" if d == "allow" else "tap_denied")
        mod.toolset = lambda ctx: Tap() if ctx.level == "full" else None
        mod.offered = lambda model_id: (model_id != "qwen38-kyojin", "Not offered for Qwen 3.8 (Kyojin)")
        mod.check_turn_on = lambda body, chat, model_id: {"scope": "folder"} if body.get("confirm") is True else (_ for _ in ()).throw(ValueError("confirm"))
        sys.modules["chat_full"] = mod
        self.addCleanup(sys.modules.pop, "chat_full", None)

    def run_turn(self, c, react):
        evs = []
        for e in self.agent.turn(c["id"], self.ep, "served", "quick-chat", "go"):
            evs.append(e)
            if e["type"] == "ask":
                react(e)
        return evs

    def test_allow_and_deny(self):
        for decision in ("allow", "deny"):
            c = self.chat("full")
            self.script += [{"calls": [("risky", {})]}, {"text": "ok"}]
            open_seen = []

            def react(e, decision=decision):
                open_seen.append(self.st.chat(c["id"]) and T.ASKS.open(c["id"]))
                threading.Timer(0.2, T.ASKS.answer, args=(c["id"], e["id"], decision)).start()
            evs = self.run_turn(c, react)
            ask = next(e for e in evs if e["type"] == "ask")
            self.assertEqual((ask["command"], ask["why"]), ("rm -rf build", "deletes files"))
            self.assertEqual(open_seen[0]["id"], ask["id"])
            self.assertEqual(next(e for e in evs if e["type"] == "ask_done")["decision"], decision)
            self.assertEqual(next(e for e in evs if e["type"] == "tool_done")["decision"], "tap_allowed" if decision == "allow" else "tap_denied")
            p = [x for x in self.answer(c)["meta"]["parts"] if "ask" in x][0]["ask"]
            self.assertEqual((p["open"], p["decision"], p["how"]), (False, decision, "owner"))
            self.assertFalse(T.ASKS.answer(c["id"], ask["id"], "allow"))  # answered once only

    def test_allow_all_persists_and_is_scoped_to_chat(self):
        c = self.chat("full")
        self.script += [{"calls": [("risky", {})]}, {"calls": [("risky", {})]}, {"text": "ok"}]
        evs = self.run_turn(c, lambda e: T.ASKS.answer(c["id"], e["id"], "allow_all"))
        self.assertEqual(sum(e["type"] == "ask" for e in evs), 1)
        self.assertEqual([e["how"] for e in evs if e["type"] == "ask_done"], ["chat_all", "chat_auto"])
        self.assertEqual(self.st.chat(c["id"])["auto_approve"], 1)
        self.script += [{"calls": [("risky", {})]}, {"text": "next turn"}]
        evs = self.run_turn(c, lambda e: self.fail("Should auto approve the next turn"))
        self.assertTrue(any(e.get("how") == "chat_auto" for e in evs))
        other = self.chat("full")
        self.script += [{"calls": [("risky", {})]}, {"text": "other chat"}]
        evs = self.run_turn(other, lambda e: T.ASKS.answer(other["id"], e["id"], "deny"))
        self.assertTrue(any(e["type"] == "ask" for e in evs))
        self.assertFalse(T.ASKS.answer(other["id"], "stale", "allow_all"))
        self.assertEqual(self.st.chat(other["id"])["auto_approve"], 0)
        self.st.set_auto_approve(c["id"], False)
        self.script += [{"calls": [("risky", {})]}, {"text": "revoked"}]
        evs = self.run_turn(c, lambda e: T.ASKS.answer(c["id"], e["id"], "deny"))
        self.assertTrue(any(e["type"] == "ask" for e in evs))

    def test_permission_change_clears_allow_all(self):
        c = self.chat("full")
        self.st.set_auto_approve(c["id"], True)
        self.st.set_perm(c["id"], "none")
        self.assertEqual(self.st.chat(c["id"])["auto_approve"], 0)

    def test_stop_precedes_auto_approval(self):
        stop = threading.Event()
        stop.set()
        decision, _, how = T.Asks().wait("stopped", {}, stop, auto_allow=lambda: True)
        self.assertEqual((decision, how), ("deny", "stop"))

    def test_stop_denies_an_open_tap(self):
        c = self.chat("full")
        self.script += [{"calls": [("risky", {})]}, {"text": "never"}]
        evs = self.run_turn(c, lambda e: threading.Timer(0.2, self.relay.stop, args=(c["id"],)).start())
        self.assertEqual(next(e for e in evs if e["type"] == "ask_done")["how"], "stop")
        self.assertEqual(next(e for e in evs if e["type"] == "tool_done")["decision"], "tap_denied")
        self.assertIsNone(T.ASKS.open(c["id"]))

    def test_a_message_sent_while_a_tap_waits_goes_in_after_the_tap(self):
        """The uncensored model in Full asks before every command: the user's message waits for the user's tap, never
        answers it."""
        c = self.chat("full")
        self.script += [{"calls": [("risky", {})]}, {"text": "Read it."}]
        state = {}

        def react(e):
            self.st.enqueue(c["id"], "use the other folder", {"attachments": [], "effort": None, "steer": True})
            state["open"] = T.ASKS.open(c["id"])  # still waiting for the tap after the message came
            threading.Timer(0.2, T.ASKS.answer, args=(c["id"], e["id"], "deny")).start()
        evs = self.run_turn(c, react)
        self.assertEqual(state["open"]["id"], next(e for e in evs if e["type"] == "ask")["id"])
        self.assertEqual(next(e for e in evs if e["type"] == "ask_done")["how"], "owner")
        kinds = [e["type"] for e in evs if e["type"] in ("ask_done", "tool_done", "user", "start")]
        self.assertEqual(kinds, ["user", "start", "ask_done", "tool_done", "user", "start"])
        self.assertEqual([(m["role"], m["content"]) for m in self.st.messages(c["id"])][-2:],
                         [("user", "use the other folder"), ("assistant", "Read it.")])
        last = self.bodies[1]["messages"][-1]  # in the result of the call the user answered, as a step's end takes it
        self.assertEqual(last["role"], "tool")
        self.assertTrue(last["content"].endswith(halo_chat.STEER_IN + "use the other folder" + halo_chat.STEER_OUT), last["content"])

    def test_the_perm_checks(self):
        c = self.st.create_chat(kind="chat", model="quick-chat")
        with self.assertRaises(ValueError):
            T.check_perm(c, {"perm": "full"}, "dsv41")          # no confirm
        self.assertEqual(T.check_perm(c, {"perm": "full", "confirm": True}, "dsv41"), ("full", {"scope": "folder"}))
        with self.assertRaisesRegex(ValueError, "Kyojin"):
            T.check_perm(c, {"perm": "full", "confirm": True}, "qwen38-kyojin")
        with self.assertRaises(ValueError):
            T.check_perm(c, {"perm": "root"}, "dsv41")
        self.assertEqual(T.check_perm(c, {"perm": "box_web"}, "dsv41"), ("box_web", {}))
        try:  # Direct is offered with a computer to run on (a setup's own; the fixture's here)
            import site_fixture
        except ImportError:
            from tests import site_fixture
        site_fixture.use_targets(self)
        v = T.view(c, "qwen38-kyojin")
        self.assertEqual([(r["id"], r["offered"]) for r in v["levels"]], [("none", True), ("box", True), ("box_web", True), ("full", False), ("direct", True)])

    def test_a_private_chats_audit_is_metadata_only(self):
        T.audit(self.st, "c1", {"tool": "run_command", "args": {"command": "cat secret"}, "status": "ok", "out_bytes": 9,
                                "refused": ["x"], "exit": 0}, private=True)
        line = json.loads(open(self.st.path("chat-tools", "c1.jsonl")).read())
        self.assertEqual(set(line), {"tool", "status", "exit", "t"})


CANARY = "kestrel-PRIVATE-7f3a91"


def canary_forms(s=CANARY):
    import base64
    b = s.encode()
    out = {b, b.hex().encode(), b.hex().upper().encode()}
    for pad in range(3):
        out.add(base64.b64encode(b"\0" * pad + b)[4:-4])
    return out


def canary_hits(*roots):
    fs, hits = canary_forms(), []
    for root in roots:
        for dp, dn, fn in os.walk(root):
            for f in fn:
                try:
                    data = open(os.path.join(dp, f), "rb").read()
                except OSError:
                    continue
                hits += [os.path.join(dp, f) for x in fs if x in data]
    return hits


class PrivateBox(Base):
    """A Private chat's container: its folder on Toolbox is one sealed file, opened only
    inside a call; nothing of it is in plain on Toolbox's disk or this Mac's, and an older folder is refused."""

    @classmethod
    def setUpClass(cls):  # Private's photo and PDF reader, as private_code.install sets it up at app start
        import private_reader as R
        cls._reader = R.READER
        R.READER = R.Reader(open(os.path.join(ROOT, "mac", "halo_extract.swift"), "rb").read(), True)

    @classmethod
    def tearDownClass(cls):
        import private_reader as R
        R.READER.close()
        R.READER = cls._reader

    def setUp(self):
        super().setUp()
        import private_store as P
        self.P = P
        self.st.vault.unlock("correct horse battery staple")
        tok = P.GRANT.set(True)  # this test's thread holds the grant, as the unlocked page's request would
        self.addCleanup(P.GRANT.reset, tok)

    def pchat(self, perm="box"):
        c = self.st.create_chat(kind="chat", model="quick-chat", private=True)
        self.st.set_perm(c["id"], perm, {})
        return c

    def test_a_private_turn_works_in_a_sealed_folder(self):
        c = self.pchat()
        self.script += [{"calls": [("run_command", {"command": f"echo {CANARY} > a.txt; mkdir -p d; cat a.txt"})]},
                        {"text": "Wrote it."}]
        evs = self.turn(c, f"write {CANARY}")
        done = next(e for e in evs if e["type"] == "tool_done")
        self.assertEqual((done["status"], done["exit"]), ("ok", 0), done)
        self.assertIn(CANARY, done["out"])
        self.assertEqual(sorted(os.listdir(self.box_root)), [".locks", ".meta", ".tmp", f"{c['id']}.sealed"])
        self.assertEqual(os.listdir(os.path.join(self.box_root, ".tmp")), [])
        self.assertEqual(self.st.private_workspace(c["id"])[1], 1)
        # the next turn finds the file: the folder lasts, sealed, between calls
        self.script += [{"calls": [("read_file", {"path": "a.txt"}), ("list_dir", {"path": "."})]}, {"text": "Read it."}]
        evs = self.turn(c, "read it back")
        dones = [e for e in evs if e["type"] == "tool_done"]
        self.assertIn(CANARY, dones[0]["out"])
        self.assertIn("d/", dones[1]["out"])
        self.assertEqual(self.st.private_workspace(c["id"])[1], 1)  # reading changes nothing
        self.assertEqual(canary_hits(self.box_root, self.st.root), [])  # Toolbox's disk and the store: sealed only
        audit = [json.loads(x) for x in open(self.st.path("chat-tools", f"{c['id']}.jsonl"))]
        self.assertTrue(all(set(a) <= set(T.AUDIT_META) for a in audit), audit)

    def test_attachments_go_in_and_a_shown_picture_comes_back_sealed(self):
        c = self.pchat()
        txt = self.st.add_upload(f"name,code\nSam,{CANARY}\n".encode(), "codes.csv", c["id"], text_ok=True)
        pic = self.st.add_upload(mediainfo.test_pattern(64, 48, 3), "board.png", c["id"], text_ok=True)
        self.script += [{"calls": [("run_command", {"command": "ls attachments; cp attachments/board.png out.png"}),
                                   ("show_file", {"path": "out.png"})]}, {"text": "Shown."}]
        evs = self.turn(c, "use these", [{"id": txt["id"]}, {"id": pic["id"]}])
        notes = [e["text"] for e in evs if e["type"] == "note"]
        self.assertEqual(notes, ["Copied into the container: attachments/codes.csv, attachments/board.png"])
        dones = [e for e in evs if e["type"] == "tool_done"]
        self.assertIn("board.png", dones[0]["out"])
        u = self.st.upload(dones[1]["shown"])
        self.assertEqual((u["kind"], u.get("private")), ("image", True))
        self.assertTrue(os.path.exists(self.st.path("refs", f"{u['id']}.hpv")))
        self.assertFalse(os.path.exists(self.st.path("refs", f"{u['id']}.png")))
        self.assertEqual(canary_hits(self.box_root, self.st.root), [])

    def test_an_older_folder_is_refused(self):
        c = self.pchat()
        self.script += [{"calls": [("write_file", {"path": "a.txt", "content": "one"})]}, {"text": "1"}]
        self.turn(c)
        blob = os.path.join(self.box_root, f"{c['id']}.sealed")
        old = open(blob, "rb").read()
        self.script += [{"calls": [("write_file", {"path": "a.txt", "content": "two"})]}, {"text": "2"}]
        self.turn(c)
        self.assertEqual(self.st.private_workspace(c["id"])[1], 2)
        open(blob, "wb").write(old)  # Toolbox hands back the first version
        self.script += [{"calls": [("read_file", {"path": "a.txt"})]}, {"text": "3"}]
        evs = self.turn(c)
        done = next(e for e in evs if e["type"] == "tool_done")
        self.assertEqual(done["status"], "refused" if "refused" in done["line"] else done["status"])
        self.assertIn("didn't work", done["line"])
        res = self.bodies[-1]["messages"][-1]["content"]
        self.assertIn("rolled back", res)

    def test_full_access_is_never_offered_and_a_plain_call_never_reaches_it(self):
        c = self.pchat("box_web")
        with self.assertRaisesRegex(ValueError, "Full access isn't offered"):
            T.check_perm(self.st.chat(c["id"]), {"perm": "full", "confirm": True}, "dsv41")
        v = T.view(self.st.chat(c["id"]), "dsv41")
        self.assertEqual([(r["id"], r["offered"]) for r in v["levels"]], [("none", True), ("box", True), ("box_web", True), ("full", False), ("direct", False)])
        self.assertTrue(v["private"])
        self.assertIsNone(v["full"])
        # a call without the seal (a bug on this side) is refused by chat-tools once the chat has a sealed folder
        self.script += [{"calls": [("write_file", {"path": "a.txt", "content": "x"})]}, {"text": "ok"}]
        self.turn(c)
        r = chat_box.call(True, c["id"], "box", {"op": "list", "path": "."}, threading.Event(), 30)
        self.assertEqual(r.get("refused"), "this chat's folder on Toolbox is sealed: it opens only with its key")
        self.assertFalse(os.path.isdir(os.path.join(self.box_root, c["id"])))

    def test_a_folder_gone_from_toolbox_starts_empty_and_says_so(self):
        c = self.pchat()
        self.script += [{"calls": [("write_file", {"path": "a.txt", "content": "x"})]}, {"text": "1"}]
        self.turn(c)
        os.unlink(os.path.join(self.box_root, f"{c['id']}.sealed"))
        self.script += [{"calls": [("list_dir", {"path": "."})]}, {"text": "2"}]
        self.turn(c)
        self.assertIn("sealed folder on Toolbox was gone", self.bodies[-1]["messages"][-1]["content"])

    def test_a_message_sent_mid_turn_goes_in_after_the_running_step(self):
        """Mid-answer messages work in a Private chat too: written sealed, with the turn's grant."""
        c = self.pchat()

        def hook():  # the user's page sends it (its own request, with the grant)
            with self.P.granted():
                self.st.enqueue(c["id"], f"also {CANARY}", {"attachments": [], "effort": None, "steer": True})
        self.script += [{"text": "Checking.", "calls": [("run_command", {"command": "echo one"})], "hook": hook},
                        {"text": "One, and two."}]
        evs = self.turn(c)
        self.assertNotIn("error", [e["type"] for e in evs], evs)
        self.assertEqual([(m["role"], m["content"]) for m in self.st.messages(c["id"])],
                         [("user", "do it"), ("assistant", "Checking."), ("user", f"also {CANARY}"), ("assistant", "One, and two.")])
        self.assertEqual(self.st.waiting(c["id"]), [])
        self.assertEqual(canary_hits(self.box_root, self.st.root), [])

    def test_no_private_call_when_toolbox_cant_say_it_seals(self):
        c = self.pchat()
        os.environ["HALO_CHAT_TOOLS_TRANSPORT"] = json.dumps(["/usr/bin/false"])  # Toolbox not answering
        self.script += [{"calls": [("write_file", {"path": "a.txt", "content": CANARY})]}, {"text": "1"}]
        self.turn(c)
        self.assertIn("Toolbox isn't answering", self.bodies[-1]["messages"][-1]["content"])
        self.assertFalse(os.path.exists(self.box_root) and os.listdir(self.box_root))

    def test_an_older_toolbox_gets_no_private_call(self):
        """An older chat-tools would ignore the seal and write in plain: without "sealed" in its health, nothing goes."""
        c = self.pchat()
        chat_box._HEALTH[True] = (time.time(), True, {"bwrap": True, "chats_fs": True})
        self.script += [{"calls": [("write_file", {"path": "a.txt", "content": CANARY})]}, {"text": "1"}]
        evs = self.turn(c)
        done = next(e for e in evs if e["type"] == "tool_done")
        self.assertIn("didn't work", done["line"])
        self.assertIn("older than Private chats need", self.bodies[-1]["messages"][-1]["content"])
        self.assertFalse(os.path.exists(self.box_root) and os.listdir(self.box_root))

    def test_the_wipe_removes_the_sealed_folder(self):
        c = self.pchat()
        self.script += [{"calls": [("write_file", {"path": "a.txt", "content": "x"})]}, {"text": "1"}]
        self.turn(c)
        self.assertTrue(os.path.exists(os.path.join(self.box_root, f"{c['id']}.sealed")))
        self.assertTrue(chat_box.call(True, c["id"], "box", {"op": "wipe"}, threading.Event(), 30)["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.box_root, f"{c['id']}.sealed")))


class Sandbox(unittest.TestCase):
    """chat-tools and chat-run as they run on Toolbox: the bwrap arguments, the disk watcher, the gate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-sbx-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["CHAT_TOOLS_ROOT"] = self.tmp
        self.addCleanup(os.environ.pop, "CHAT_TOOLS_ROOT", None)
        self.ct = load_script("chat-tools")

    def test_the_sandbox_holds_only_the_chats_folder(self):
        d = os.path.join(self.tmp, "0123456789abcdef")
        a = self.ct.bwrap_argv(d, exists=lambda p: True)
        s = " ".join(a)
        for want in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv", "--ro-bind /usr /usr",
                     f"--bind {d} /work", "--chdir /work", f"--size {1 << 30} --tmpfs /tmp"):
            self.assertIn(want, s)
        self.assertNotIn("/home/agent", s.replace(d, ""))
        self.assertNotIn("--share-net", s)
        self.assertNotIn("/run/proxy.sock", s)
        self.assertNotIn("/etc/halo-tools", s)
        self.assertIn("--bind /x.sock /run/proxy.sock", " ".join(self.ct.bwrap_argv(d, "/x.sock", exists=lambda p: True)))
        binds = [a[i + 2] for i, x in enumerate(a) if x in ("--bind", "--ro-bind")]
        self.assertTrue(all(b.startswith(("/usr", "/etc/", "/opt/node", "/run/chat-run", "/work")) for b in binds), binds)

    def test_a_sealed_calls_sandbox_has_its_folder_in_memory_only(self):
        a = " ".join(self.ct.bwrap_argv(None, exists=lambda p: True, sealed=("/c/x.sealed", "/c/.tmp/o", "/c/.tmp/s")))
        for want in (f"--size {2 << 30} --tmpfs /work", "--ro-bind /c/x.sealed /run/sealed.in", "--bind /c/.tmp/o /run/sealed.out",
                     "--bind /c/.tmp/s /run/seal.json", "--chdir /work", "--unshare-all"):
            self.assertIn(want, a)
        self.assertNotIn("--bind None", a)
        first = " ".join(self.ct.bwrap_argv(None, exists=lambda p: True, sealed=(None, "/o", "/s")))
        self.assertNotIn("/run/sealed.in", first)  # the first call: no sealed file yet

    def test_the_sealed_file_is_what_the_comment_says(self):
        """The format, read here by an independent reader on the Mac's own cipher (private_crypto.aes_ctr): header,
        64 KiB chunks, each tagged with its index and whether it is the last; a changed byte, a dropped last chunk or a
        file cut at a chunk boundary is refused by chat-run."""
        import base64
        import hashlib
        import hmac
        import io
        import struct
        import tarfile
        import private_crypto as C
        cid, key = "0123456789abcdef", os.urandom(32)
        env = dict(os.environ, CHAT_TOOLS_SANDBOX="none")

        def call(req):
            p = subprocess.Popen([sys.executable, "-I", os.path.join(TOOLS_DIR, "chat-tools"), cid, "box"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            p.stdin.write((json.dumps(dict(req, seal={"wk": base64.b64encode(key).decode(), "ver": req.pop("ver", 0)})) + "\n").encode())
            p.stdin.flush()
            res = None
            for line in p.stdout:
                f = json.loads(line)
                if f.get("t") == "result":
                    res = f
                    break
            p.stdin.close()
            p.wait(10)
            return res
        big = "".join(f"line {i} {'x' * 90}\n" for i in range(2000))  # ~200 KB: several chunks
        self.assertTrue(call({"op": "write", "path": "big.txt", "content": big})["ok"])
        blob = open(os.path.join(self.tmp, f"{cid}.sealed"), "rb").read()
        self.assertEqual(blob[:4], b"hpw1")
        self.assertEqual(blob[4:20], cid.encode())
        self.assertEqual(struct.unpack(">Q", blob[20:28])[0], 1)
        head, nonce = blob[:36], blob[28:36]
        kenc, kmac = hmac.new(key, b"hpw1 enc", hashlib.sha256).digest(), hmac.new(key, b"hpw1 mac", hashlib.sha256).digest()
        body, plain, i = blob[36:], b"", 0
        while body:
            piece, body = body[:65536 + 32], body[65536 + 32:]
            ct, tag = piece[:-32], piece[-32:]
            want = hmac.new(kmac, head + struct.pack(">QB", i, 0 if body else 1) + ct, hashlib.sha256).digest()
            self.assertEqual(tag, want, i)
            plain += C.aes_ctr(kenc, nonce + struct.pack(">II", i, 0), ct) if ct else b""
            i += 1
        self.assertGreater(i, 2)
        with tarfile.open(fileobj=io.BytesIO(plain)) as t:
            self.assertEqual(t.extractfile("big.txt").read().decode(), big)
        path = os.path.join(self.tmp, f"{cid}.sealed")
        for bad in (blob[:100] + bytes([blob[100] ^ 1]) + blob[101:],  # a changed byte
                    blob[:36 + 2 * (65536 + 32)],                       # cut at a chunk boundary
                    blob[:-1]):                                         # cut short
            open(path, "wb").write(bad)
            r = call({"op": "read", "path": "big.txt", "ver": 1})
            self.assertFalse(r["ok"], r)
            self.assertIn("sealed folder couldn't be used", r["error"])

    def test_stop_and_the_time_limit_still_seal_the_folder(self):
        import base64
        cid, wk = "0123456789abcdef", base64.b64encode(os.urandom(32)).decode()
        env = dict(os.environ, CHAT_TOOLS_SANDBOX="none")

        def call(req, close_after=None):
            p = subprocess.Popen([sys.executable, "-I", os.path.join(TOOLS_DIR, "chat-tools"), cid, "box"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            p.stdin.write((json.dumps(req) + "\n").encode())
            p.stdin.flush()
            if close_after:
                threading.Timer(close_after, p.stdin.close).start()
            res = None
            for line in p.stdout:
                f = json.loads(line)
                if f.get("t") == "result":
                    res = f
                    break
            try:
                p.stdin.close()
            except OSError:
                pass
            p.wait(10)
            return res
        r = call({"op": "run", "command": "echo kept > a.txt; sleep 30; echo never > b.txt", "seal": {"wk": wk, "ver": 0}}, close_after=1.5)
        self.assertEqual((r["killed"], r["ver"]), ("stop", 1), r)
        r = call({"op": "run", "command": "echo t > t.txt; sleep 30", "timeout_s": 1, "seal": {"wk": wk, "ver": 1}})
        self.assertEqual((r["killed"], r["ver"]), ("time", 2), r)
        r = call({"op": "list", "path": ".", "seal": {"wk": wk, "ver": 2}})
        self.assertEqual(r["text"].split("\n"), ["a.txt\tfile\t5", "t.txt\tfile\t2"])
        # a chat with a plain folder never gets a sealed one, and the other way round
        os.makedirs(os.path.join(self.tmp, "fedcba9876543210"))
        p = subprocess.run([sys.executable, "-I", os.path.join(TOOLS_DIR, "chat-tools"), "fedcba9876543210", "box"],
                           input=(json.dumps({"op": "list", "seal": {"wk": wk, "ver": 0}}) + "\n").encode(), capture_output=True, env=env, timeout=30)
        self.assertIn("unsealed folder on Toolbox", p.stdout.decode())

    def test_the_disk_watcher_stops_a_command_past_the_cap(self):
        self.ct.CHAT_CAP, self.ct.WATCH_S = 3 << 20, 0.2
        call = self.ct.Call("0123456789abcdef", "box", {"op": "run", "command": "while true; do head -c 1048576 /dev/zero >> big; sleep 0.05; done", "timeout_s": 30})
        os.makedirs(call.dir, exist_ok=True)
        got = []
        self.ct.emit = got.append
        t0 = time.time()
        old = self.ct.SANDBOX
        self.ct.SANDBOX = "none"
        self.addCleanup(setattr, self.ct, "SANDBOX", old)
        call.run()
        res = got[-1]
        self.assertEqual((res["t"], res["killed"], res["exit"]), ("result", "disk", None))
        self.assertLess(time.time() - t0, 10)

    def test_usage_counts_real_blocks(self):
        d = os.path.join(self.tmp, "u")
        os.makedirs(d)
        with open(os.path.join(d, "sparse"), "wb") as f:
            f.seek(1 << 30)
            f.write(b"x")
        open(os.path.join(d, "real"), "wb").write(b"y" * (1 << 20))
        used, n = self.ct.usage(d)
        self.assertEqual(n, 2)
        self.assertLess(used, 4 << 20)  # the 1 GB sparse file counts as its blocks

    def test_the_total_cap_and_the_idle_sweep(self):
        a, b = "a" * 16, "b" * 16
        for cid in (a, b):
            os.makedirs(os.path.join(self.tmp, cid))
        self.ct.set_meta(a, bytes=self.ct.TOTAL_CAP + 1, last=time.time())
        self.ct.set_meta(b, last=time.time() - self.ct.IDLE_S - 10)
        self.ct.sweep()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, b)))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, a)))
        r = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "chat-tools"), "c" * 16, "box"], input=b'{"op":"write","path":"x","content":"1"}\n',
                           capture_output=True, env=dict(os.environ, CHAT_TOOLS_SANDBOX="none"), timeout=30)
        self.assertIn("100 GB together", r.stdout.decode())

    def test_the_gate_lets_through_only_its_own_commands(self):
        gate = open(os.path.join(TOOLS_DIR, "dsh-gate")).read().replace("/opt/halo-tools/bin/", os.path.join(self.tmp, "bin") + "/")
        os.makedirs(os.path.join(self.tmp, "bin"))
        for n in ("dsh-acp", "dsh-health", "chat-tools", "chat-health"):
            p = os.path.join(self.tmp, "bin", n)
            open(p, "w").write(f'#!/bin/bash\necho "{n} $*"\n')
            os.chmod(p, 0o755)
        g = os.path.join(self.tmp, "gate")
        open(g, "w").write(gate)
        os.chmod(g, 0o755)
        cases = {"chat-tools 0123456789abcdef box": "chat-tools 0123456789abcdef box", "chat-tools 0123456789abcdef web": "chat-tools 0123456789abcdef web",
                 "chat-health": "chat-health ", "dsh-health": None, "dsh-acp agents": None,
                 "chat-tools 0123456789ABCDEF box": None, "chat-tools 0123456789abcdef full": None, "chat-tools ../../etc box": None,
                 "chat-tools 0123456789abcdef box; bash": None, "chat-tools 0123456789abcdef box\nbash": None, "bash": None,
                 "chat-health x": None, "": None}
        for cmd, want in cases.items():
            r = subprocess.run([g], env={"SSH_ORIGINAL_COMMAND": cmd, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=10)
            if want is None:
                self.assertEqual((r.returncode, r.stdout), (64, ""), cmd)
            else:
                self.assertEqual((r.returncode, r.stdout.strip()), (0, want.strip()), cmd)

    def test_mounted_needs_all_three_signs(self):
        """A mount point, another device than /home/agent, and the install's root-owned marker; each one
        missing means the chats disk isn't there."""
        import stat as st_
        self.ct.SANDBOX = "bwrap"
        root = os.path.join(self.tmp, "chats")
        os.makedirs(root)
        dev = {root: 2, self.tmp: 1}
        S = lambda path, **_: types.SimpleNamespace(st_dev=dev[os.path.abspath(path)])  # noqa: E731
        mark = lambda uid=0, mode=st_.S_IFREG | 0o644: (lambda path: types.SimpleNamespace(st_uid=uid, st_mode=mode))  # noqa: E731
        self.assertTrue(self.ct.mounted(root, S, mark(), lambda p: True))
        self.assertFalse(self.ct.mounted(root, S, mark(), lambda p: False))             # not a mount point
        self.assertFalse(self.ct.mounted(root, S, mark(uid=1000), lambda p: True))      # marker not root's
        self.assertFalse(self.ct.mounted(root, S, mark(mode=st_.S_IFLNK | 0o777), lambda p: True))  # a link, not a file
        dev[root] = 1
        self.assertFalse(self.ct.mounted(root, S, mark(), lambda p: True))             # same device as /home/agent
        self.assertFalse(self.ct.mounted(root))                                          # a plain folder: no marker
        r = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "chat-tools"), "--health"], capture_output=True,
                           env=dict(os.environ, CHAT_TOOLS_SANDBOX="bwrap", PATH="/usr/bin:/bin"), timeout=30)
        h = json.loads(r.stdout.decode().strip().splitlines()[-1])
        self.assertEqual((h["chats_fs"], h["words"]), (False, "chats disk not mounted"))

    def test_a_full_disk_in_plain_words(self):
        import errno as errno_
        import io
        got = []
        self.ct.emit = got.append
        self.ct.SANDBOX = "none"
        def full(self_):
            raise OSError(errno_.ENOSPC, "No space left on device")
        self.ct.Call.op = full
        old = sys.stdin
        sys.stdin = io.TextIOWrapper(io.BytesIO(b'{"op": "list"}\n'))
        try:
            self.ct.main(["9" * 16, "box"])
        finally:
            sys.stdin = old
        self.assertEqual([g for g in got if g.get("t") == "result"][-1]["error"], "Toolbox's chats disk is full")

    def test_lost_and_found_is_never_a_chat(self):
        lf = os.path.join(self.tmp, "lost+found")
        os.makedirs(lf)
        os.utime(lf, (0, 0))  # as old as can be
        self.ct.sweep()
        self.assertTrue(os.path.exists(lf))
        self.assertNotIn("lost+found", self.ct.chats())
        r = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "chat-tools"), "lost+found", "box"], input=b'{"op":"wipe"}\n',
                           capture_output=True, env=dict(os.environ, CHAT_TOOLS_SANDBOX="none"), timeout=30)
        self.assertEqual(r.returncode, 64)
        self.assertTrue(os.path.exists(lf))

    def test_no_wipe_without_the_chats_filesystem(self):
        """An unmounted filesystem can't be wiped, so chat-tools says so instead of "wiped" (the files would
        still sit in the image), and the folder underneath is left as it is."""
        d = os.path.join(self.tmp, "f" * 16)
        os.makedirs(d)
        open(os.path.join(d, "keep"), "w").write("x")
        r = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "chat-tools"), "f" * 16, "box"], input=b'{"op":"wipe"}\n',
                           capture_output=True, env=dict(os.environ, CHAT_TOOLS_SANDBOX="bwrap"), timeout=30)
        res = json.loads(r.stdout.decode().strip().splitlines()[-1])
        self.assertEqual((res["ok"], res["refused"]), (False, "Toolbox's chats disk isn't mounted, so the container is off"))
        self.assertTrue(os.path.exists(os.path.join(d, "keep")))

    def test_no_call_without_the_chats_filesystem_and_the_floor(self):
        """In production a call runs only on the mounted 100 GB filesystem, and a writing call only
        with 6 GB free on it."""
        r = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "chat-tools"), "d" * 16, "box"], input=b'{"op":"list"}\n',
                           capture_output=True, env=dict(os.environ, CHAT_TOOLS_SANDBOX="bwrap"), timeout=30)
        self.assertIn("chats disk isn't mounted, so the container is off", r.stdout.decode())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "d" * 16)))
        r = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "chat-tools"), "--health"], capture_output=True,
                           env=dict(os.environ, CHAT_TOOLS_SANDBOX="bwrap", PATH="/usr/bin:/bin"), timeout=30)
        h = json.loads(r.stdout.decode().strip().splitlines()[-1])
        self.assertIs(h["chats_fs"], False)
        import io
        got = []
        self.ct.emit = got.append
        self.ct.SANDBOX, self.ct.FLOOR = "none", 1 << 62
        old = sys.stdin
        for req, want in (({"op": "write", "path": "x", "content": "1"}, "under 6 GB free"), ({"op": "list"}, None)):
            sys.stdin = io.TextIOWrapper(io.BytesIO((json.dumps(req) + "\n").encode()))
            try:
                self.ct.main(["e" * 16, "box"])
            finally:
                sys.stdin = old
            res = [g for g in got if g.get("t") == "result"][-1]
            if want:
                self.assertIn(want, res.get("refused", ""))
            else:
                self.assertTrue(res["ok"], res)  # reading needs no free space


class WebRelay(Base):
    """Container + web: a command's HTTP goes from the sandbox's proxy port through chat-tools' socket, over the
    call's own stdio, into this Mac's filter (lead_proxy with a --fake guard: one test route to a stand-in site);
    anything else, a private address included, is refused and said."""

    def site(self):
        class S(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                out = b"HELLO-FROM-THE-SITE"
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), S)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_a_command_reaches_a_site_only_through_the_filter(self):
        if not shutil.which("curl"):
            self.skipTest("curl is not installed")
        import lead_proxy
        from agents_web.guard import EgressGuard
        g = EgressGuard(fake=True)
        g.add_test_route("site.halo-test.invalid", self.site())
        c = self.chat("box_web")
        ctx = T.TurnCtx(c["id"], self.st.chat(c["id"]), self.st, "dsv41", "box_web", fake=True)
        tools = chat_box.BoxTools(ctx, web=True)
        tools.proxy = lead_proxy.LeadProxy(g, own=set())
        self.addCleanup(tools.close)
        self.assertEqual({x["function"]["name"] for x in tools.specs()} >= {"web_search", "web_read", "run_command"}, True)
        r = tools.call("run_command", {"command": "curl -s -m 10 http://site.halo-test.invalid/hello; echo; "
                                                  "curl -s -m 10 -o /dev/null -w '%{http_code}' http://192.168.1.1/; echo"}, ctx)
        self.assertEqual((r["status"], r["exit"]), ("ok", 0), r)
        self.assertIn("HELLO-FROM-THE-SITE", r["out"])
        self.assertIn("403", r["out"])  # the filter's answer to a private address
        self.assertTrue(any("192.168.1.1" in x for x in r["refused"]), r["refused"])
        self.assertIn("The network filter refused", r["text"])

    def test_web_search_and_read_on_the_fake_apps_stand_in(self):
        """agents_tools' Stub under --fake (fixed results, no network): the search and the read come back as untrusted
        data with their lines; a search that would carry what the turn read out is refused."""
        c = self.chat("box_web")
        ctx = T.TurnCtx(c["id"], self.st.chat(c["id"]), self.st, "dsv41", "box_web", app=types.SimpleNamespace(fake=True), fake=True)
        tools = chat_box.BoxTools(ctx, web=True)
        self.addCleanup(tools.close)
        r = tools.call("web_search", {"query": "rust ownership rules"}, ctx)
        self.assertEqual(r["status"], "ok", r)
        self.assertTrue(r["line"].startswith("Searched the web for"))
        self.assertIn("<untrusted", r["text"])
        url = next((ln.strip() for ln in r["out"].splitlines() if ln.strip().startswith("http")), None)
        if url:
            r2 = tools.call("web_read", {"url": url}, ctx)
            self.assertIn(r2["status"], ("ok", "failed"), r2)
            self.assertTrue(r2["line"].startswith(("Read ", "Read failed", "Read refused")), r2["line"])

    def test_container_without_web_has_no_proxy(self):
        c = self.chat("box")
        ctx = T.TurnCtx(c["id"], self.st.chat(c["id"]), self.st, "dsv41", "box", fake=True)
        tools = chat_box.BoxTools(ctx, web=False)
        self.assertNotIn("web_search", {x["function"]["name"] for x in tools.specs()})
        r = tools.call("run_command", {"command": "echo ${HTTPS_PROXY:-none}"}, ctx)
        self.assertIn("none", r["out"])
        self.assertIsNone(tools.proxy)


class WebGuard(Base):
    """The leak guard on Container + web's search and read (a job search refused over and over): a public page's words
    are not the user's text, the user's own messages anywhere in the chat may be searched for, an address from a search
    result skips the copy check; words copied from the user's files still don't go."""
    JOB = "Harbor Valley Credit Union is hiring a Chief Operations Officer in Springfield, Oregon."
    CV = "Logistics lead at Northwind Bakery, 2019 to now; BSc in Supply Chain."

    def tools(self, *said):
        c = self.chat("box_web")
        for words in said:
            self.st.add_message(c["id"], "user", words)
        ctx = T.TurnCtx(c["id"], self.st.chat(c["id"]), self.st, "dsv41", "box_web", app=types.SimpleNamespace(fake=True), fake=True)
        tools = chat_box.BoxTools(ctx, web=True)
        self.addCleanup(tools.close)
        import agents_tools
        tools.webtools = agents_tools.Stub(os.path.join(self.tmp, "web"), web={
            "pages": {"https://jobs.example.com/harbor": {"title": "Harbor jobs", "text": self.JOB}},
            "searches": {"logistics jobs": [{"title": "Job", "url": "https://jobs.example.com/logistics-lead-at-northwind-bakery",
                                                "snippet": "A job"}]},
            "search_default": [{"title": "A result", "url": "https://example.com/a", "snippet": "words"}]})
        return ctx, tools

    def test_words_from_a_public_page_may_be_searched(self):
        ctx, tools = self.tools("Find me some good jobs to apply for")
        self.assertEqual(tools.call("web_read", {"url": "https://jobs.example.com/harbor"}, ctx)["status"], "ok")
        for q in ("Harbor Valley Credit Union chief operations officer salary", "chief operations officer jobs Springfield"):
            r = tools.call("web_search", {"query": q}, ctx)
            self.assertEqual(r["status"], "ok", (q, r["line"], r["text"]))

    def test_words_from_their_files_still_dont_go_and_the_model_is_told_why(self):
        ctx, tools = self.tools("Find me some good jobs to apply for")
        tools.seen.append(self.CV)  # what read_file of the user's CV adds (file_op)
        r = tools.call("web_search", {"query": "Logistics lead at Northwind Bakery"}, ctx)
        self.assertEqual((r["status"], r["decision"]), ("refused", "refused"), r)
        self.assertTrue(r["line"].startswith("Search refused: “Logistics lead"), r["line"])
        self.assertIn("repeats words from the user's files", r["text"])
        self.assertIn("Use your own general words", r["text"])
        self.assertEqual(tools.call("web_search", {"query": "logistics jobs"}, ctx)["status"], "ok")  # general words go

    def test_an_address_from_a_search_result_skips_the_copy_check(self):
        ctx, tools = self.tools("Find me some good jobs to apply for")
        tools.seen.append(self.CV)
        made_up = "https://jobs.example.com/logistics-lead-at-northwind-bakery-2"
        self.assertEqual(tools.call("web_read", {"url": made_up}, ctx)["status"], "refused")  # the model's own address
        tools.call("web_search", {"query": "logistics jobs"}, ctx)
        r = tools.call("web_read", {"url": "https://jobs.example.com/logistics-lead-at-northwind-bakery"}, ctx)
        self.assertNotEqual(r["status"], "refused", r)  # found in a result: a third party wrote it (the stub has no page: failed)

    def test_their_earlier_messages_count_not_only_the_last(self):
        ctx, tools = self.tools("I'm a Logistics lead at Northwind Bakery, find me jobs", "keep going")
        tools.seen.append(self.CV)
        r = tools.call("web_search", {"query": "Logistics lead at Northwind Bakery jobs"}, ctx)
        self.assertEqual(r["status"], "ok", r["text"])


class Routes(unittest.TestCase):
    """The PUT's level check and divider, the delete's wipe, Toolbox offline at send."""

    def test_health_says_off_without_the_chats_filesystem(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        fake = os.path.join(d, "h")
        open(fake, "w").write('#!/bin/sh\necho \'{"bwrap": true, "chats_fs": false, "free_gb": 900}\'\n')
        os.chmod(fake, 0o755)
        os.environ["HALO_CHAT_TOOLS_TRANSPORT"] = json.dumps([fake])
        self.addCleanup(os.environ.pop, "HALO_CHAT_TOOLS_TRANSPORT", None)
        chat_box._HEALTH.clear()
        self.addCleanup(chat_box._HEALTH.clear)
        ok, info = chat_box.health(fake=True)
        self.assertEqual((ok, info["chats_fs"]), (False, False))

    def test_a_wipe_that_didnt_happen_stays_pending_and_is_retried(self):
        """Delete keeps the Toolbox wipe pending until Toolbox confirms it, and the next health that shows the
        chats filesystem retries it."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        st = studio.Store(os.path.join(d, "S"))
        chat_box._HEALTH.clear()
        self.addCleanup(chat_box._HEALTH.clear)
        os.environ["HALO_CHAT_TOOLS_TRANSPORT"] = json.dumps(["/usr/bin/false"])  # Toolbox not answering
        self.addCleanup(os.environ.pop, "HALO_CHAT_TOOLS_TRANSPORT", None)
        chat_box.wipe("a" * 16, fake=True, store=st)
        self.assertEqual(chat_box.pending(st), ["a" * 16])  # pending at once, before the wipe is even tried
        for t in [t for t in threading.enumerate() if t.name == "halo-wipe-" + "a" * 16]:
            t.join(30)  # the failed attempt (one retry after 5 s) has ended
        self.assertEqual(chat_box.pending(st), ["a" * 16])  # it failed: still pending, never dropped
        box = os.path.join(d, "box")
        open(box, "w").write('#!/bin/sh\ncase "$1" in --health) echo \'{"bwrap": true, "chats_fs": true}\' ;; '
                             '*) read l; echo \'{"t": "result", "ok": true, "text": "wiped"}\' ;; esac\n')
        os.chmod(box, 0o755)
        os.environ["HALO_CHAT_TOOLS_TRANSPORT"] = json.dumps([box])
        chat_box._HEALTH.clear()
        self.assertEqual(chat_box.health(fake=True, store=st)[0], True)
        for t in [t for t in threading.enumerate() if t.name == "halo-wipe-retry"]:
            t.join(30)
        self.assertEqual(chat_box.pending(st), [])
        self.assertEqual(os.stat(st.path("chat-tools", "pending-wipes.json")).st_mode & 0o777, 0o600)

    def test_outside_the_fake_app_the_filter_reaches_the_internet_guard(self):
        import lead_proxy
        seen = []
        real_guard, real_proxy = lead_proxy.guard_for, lead_proxy.LeadProxy
        lead_proxy.guard_for = lambda fake: seen.append(fake) or "guard"
        lead_proxy.LeadProxy = lambda guard, **kw: types.SimpleNamespace(guard=guard, close=lambda: None)
        self.addCleanup(setattr, lead_proxy, "guard_for", real_guard)
        self.addCleanup(setattr, lead_proxy, "LeadProxy", real_proxy)
        ctx = T.TurnCtx("0" * 16, {"messages": []}, None, "dsv41", "box_web", fake=False)
        self.assertEqual(chat_box.BoxTools(ctx, web=True).net().guard, "guard")
        self.assertEqual(seen, [False])

    def test_a_command_never_runs_past_the_turns_time(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.environ["CHAT_TOOLS_ROOT"] = d
        self.addCleanup(os.environ.pop, "CHAT_TOOLS_ROOT", None)
        st = studio.Store(os.path.join(d, "S"))
        c = st.create_chat(kind="chat")
        ctx = T.TurnCtx(c["id"], st.chat(c["id"]), st, "dsv41", "box", fake=True)
        ctx.deadline = time.monotonic() + 2
        t0 = time.time()
        r = chat_box.BoxTools(ctx).call("run_command", {"command": "sleep 30", "timeout_s": 600}, ctx)
        self.assertEqual(r["audit"]["killed"], "time")
        self.assertLess(time.time() - t0, 10)
        self.assertRegex(r["line"], r"stopped at its [12] s limit")  # the time the turn had left, rounded down

    def test_the_audit_keeps_at_most_2_kb_of_arguments(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        st = studio.Store(os.path.join(d, "S"))
        T.audit(st, "c2", {"tool": "write_file", "args": {"path": "a", "content": "x" * 100_000}, "status": "ok"})
        line = json.loads(open(st.path("chat-tools", "c2.jsonl")).read())
        self.assertLessEqual(len(line["args"]["_cut"]), T.AUDIT_ARGS)


    def test_offline_toolbox_words_and_health_cache(self):
        os.environ["HALO_CHAT_TOOLS_TRANSPORT"] = json.dumps(["/usr/bin/false"])
        self.addCleanup(os.environ.pop, "HALO_CHAT_TOOLS_TRANSPORT", None)
        chat_box._HEALTH.clear()
        self.assertEqual(chat_box.health(fake=True), (False, {}))
        self.assertEqual(chat_box.OFFLINE, "Toolbox isn't answering, so this chat's container can't run.")
        r = chat_box.call(True, "0" * 16, "box", {"op": "list"}, threading.Event(), 5)
        self.assertTrue(r["lost"])
        self.assertIn("Toolbox isn't answering", r["error"])

    def test_never_ssh_without_the_fake_flag(self):
        self.assertEqual(chat_box.argv(False, "0" * 16, "box")[-4:], ["halo-dsh", "chat-tools", "0" * 16, "box"])
        self.assertEqual(chat_box.argv(False, "--health")[-2:], ["halo-dsh", "chat-health"])
        os.environ["HALO_CHAT_TOOLS_TRANSPORT"] = json.dumps(["/bin/echo"])
        self.addCleanup(os.environ.pop, "HALO_CHAT_TOOLS_TRANSPORT", None)
        self.assertEqual(chat_box.argv(False, "x")[0], "ssh")  # the override only for the fake app


class LightChat(unittest.TestCase):
    """On a slower computer the app was slow: the longest chat was 8.8 MB,
    mostly its steps' output and the model's thinking, fetched whole on every open and redraw. The chat page asks for it
    light (studio_http.light): those stay on the Mac until the user opens one (chats/<id>/messages/<mid>), the answer
    being written keeps them, and the JSON is gzipped for a browser. test_studio's Http harness."""

    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("hc_test_studio_l", os.path.join(ROOT, "tests", "test_studio.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.ts = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ts)
        self.ts.Http.setUp(self)
        self.ts.Http.unlock(self)
        self.st = self.app.studio["store"]
        self.c = self.st.create_chat(kind="chat")
        self.st.add_message(self.c["id"], "user", "find the file")
        self.meta = {"reasoning": "r" * 5000, "ttft_s": 2.0, "speed": {"tps": 50},
                     "parts": [{"text": "Looking."}, {"tool": {"id": "call_a", "line": "Ran: ls", "status": "ok", "out": "o" * 9000}},
                               {"tool": {"id": "call_b", "line": "Ran: pwd", "status": "ok", "out": ""}}, {"note": "a note"},
                               {"text": "Found it."}]}
        self.st.add_message(self.c["id"], "assistant", "Looking. Found it.", self.meta)
        self.mid = self.st.messages(self.c["id"])[-1]["id"]

    def req(self, *a, **k):
        return self.ts.Http.req(self, *a, **k)

    def get(self, path, **h):
        code, body, hd = self.req("GET", path, **h)
        self.assertEqual(code, 200, body)
        return json.loads(body)

    def test_light_leaves_out_the_output_and_the_thinking_and_says_how_much(self):
        whole = self.get(f"/studio/api/chats/{self.c['id']}")["messages"][-1]["meta"]
        self.assertEqual(whole, self.meta)  # without ?light=1 (Studio's page, the Command Centre): as before
        m = self.get(f"/studio/api/chats/{self.c['id']}?light=1")["messages"][-1]
        self.assertEqual(m["content"], "Looking. Found it.")
        meta = m["meta"]
        self.assertTrue(meta["light"])
        self.assertNotIn("reasoning", meta)
        self.assertEqual((meta["reasoning_n"], meta["ttft_s"], meta["speed"]), (5000, 2.0, {"tps": 50}))
        self.assertEqual(meta["parts"], [{"text": "Looking."}, {"tool": {"id": "call_a", "line": "Ran: ls", "status": "ok", "out_n": 9000}},
                                         {"tool": {"id": "call_b", "line": "Ran: pwd", "status": "ok", "out_n": 0}}, {"note": "a note"},
                                         {"text": "Found it."}])
        self.assertEqual(self.st.messages(self.c["id"])[-1]["meta"], self.meta)  # the stored message is untouched
        # what was left out, when the user opens a step or the thinking
        got = self.get(f"/studio/api/chats/{self.c['id']}/messages/{self.mid}")
        self.assertEqual((got["id"], got["meta"]), (self.mid, self.meta))

    def test_the_answer_being_written_keeps_them(self):
        relay = self.app.studio["relay"]
        busy = relay.busy
        relay.busy = lambda cid: cid == self.c["id"]
        self.addCleanup(setattr, relay, "busy", busy)
        self.st.add_message(self.c["id"], "user", "and again")
        live = {"partial": True, "reasoning": "now", "parts": [{"tool": {"id": "c", "line": "Ran: make", "status": "running", "live": "so far"}}]}
        self.st.add_message(self.c["id"], "assistant", "", live)
        msgs = self.get(f"/studio/api/chats/{self.c['id']}?light=1")["messages"]
        self.assertTrue(msgs[1]["meta"]["light"])
        self.assertEqual(msgs[-1]["meta"], live)

    def test_the_answer_being_written_keeps_them_with_a_row_after_it(self):
        """A permission change while it answers adds a note row after the answer: the answer is still the live one (the
        review: by position, a reload then drew it without its thinking and running step)."""
        relay = self.app.studio["relay"]
        busy = relay.busy
        relay.busy = lambda cid: cid == self.c["id"]
        self.addCleanup(setattr, relay, "busy", busy)
        self.st.add_message(self.c["id"], "user", "and again")
        live = {"partial": True, "reasoning": "now", "parts": [{"tool": {"id": "c", "line": "Ran: make", "status": "running", "live": "so far"}}]}
        self.st.add_message(self.c["id"], "assistant", "", live)
        self.st.add_message(self.c["id"], "note", "", {"perm_from": "box", "perm_to": "full"})
        msgs = self.get(f"/studio/api/chats/{self.c['id']}?light=1")["messages"]
        self.assertEqual(msgs[-2]["meta"], live)

    def test_a_message_only_from_its_own_chat(self):
        other = self.st.create_chat(kind="chat")
        for path in (f"/studio/api/chats/{other['id']}/messages/{self.mid}", f"/studio/api/chats/{self.c['id']}/messages/x1",
                     f"/studio/api/chats/{self.c['id']}/messages/{self.mid + 99}"):
            code, body, _ = self.req("GET", path)
            self.assertEqual((code, json.loads(body)), (404, {"error": "no such message"}), path)
        self.assertEqual(self.req("GET", f"/studio/api/chats/{self.c['id']}/messages/{self.mid}", cookie=False)[0], 401)

    def test_json_is_gzipped_for_a_browser(self):
        import gzip
        path = f"/studio/api/chats/{self.c['id']}"
        code, plain, hd = self.req("GET", path)
        self.assertEqual((code, hd.get("Content-Encoding"), hd.get("Vary")), (200, None, "Accept-Encoding"))
        code, body, hd = self.req("GET", path, Accept_Encoding="gzip, deflate, br")
        self.assertEqual((code, hd.get("Content-Encoding")), (200, "gzip"))
        self.assertEqual(int(hd["Content-Length"]), len(body))
        self.assertEqual(gzip.decompress(body), plain)
        self.assertLess(len(body), len(plain) / 5)
        code, body, hd = self.req("GET", "/studio/api/private", Accept_Encoding="gzip")  # a small answer: as it is
        self.assertEqual((code, hd.get("Content-Encoding")), (200, None))
        json.loads(body)


class DeleteRoute(unittest.TestCase):
    """Deleting a chat removes its tool files on this Mac (the audit, the web tools' pages) and wipes its
    container folder on Toolbox (the fake transport), through the real route. test_studio's Http harness, borrowed."""

    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("hc_test_studio", os.path.join(ROOT, "tests", "test_studio.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.ts = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ts)
        self.ts.Http.setUp(self)

    def req(self, *a, **k):
        return self.ts.Http.req(self, *a, **k)

    def test_the_sheet_says_which_toolbox_problem(self):
        self.ts.Http.unlock(self)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        box = os.path.join(d, "box")
        open(box, "w").write('#!/bin/sh\necho \'{"bwrap": true, "chats_fs": false, "words": "chats disk not mounted"}\'\n')
        os.chmod(box, 0o755)
        os.environ["HALO_CHAT_TOOLS_TRANSPORT"] = json.dumps([box])
        self.addCleanup(os.environ.pop, "HALO_CHAT_TOOLS_TRANSPORT", None)
        chat_box._HEALTH.clear()
        self.addCleanup(chat_box._HEALTH.clear)
        self.app.fake = True
        code, body, _ = self.req("GET", "/studio/api/chat/perms?fresh=1")
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["toolbox"], {"ok": False, "words": chat_box.NO_DISK})
        os.environ["HALO_CHAT_TOOLS_TRANSPORT"] = json.dumps(["/usr/bin/false"])
        code, body, _ = self.req("GET", "/studio/api/chat/perms?fresh=1")
        self.assertEqual(json.loads(body)["toolbox"], {"ok": False, "words": chat_box.OFFLINE})

    def test_delete_removes_the_audit_the_web_pages_and_the_container(self):
        self.ts.Http.unlock(self)
        st = self.app.studio["store"]
        c = st.create_chat(kind="chat")
        st.set_perm(c["id"], "box_web", {})
        T.audit(st, c["id"], {"tool": "web_read", "status": "ok"})
        web = st.path("chat-tools", "web", c["id"])
        os.makedirs(web)
        open(os.path.join(web, "page.html"), "w").write("<p>fetched</p>")
        wiped = []
        real = chat_box.wipe
        chat_box.wipe = lambda cid, fake=False, store=None: wiped.append(cid)
        self.addCleanup(setattr, chat_box, "wipe", real)
        code, body, _ = self.req("DELETE", f"/studio/api/chats/{c['id']}")
        self.assertEqual(code, 200, body)
        self.assertFalse(os.path.exists(web))
        self.assertFalse(os.path.exists(st.path("chat-tools", f"{c['id']}.jsonl")))
        self.assertEqual(wiped, [c["id"]])


if __name__ == "__main__":
    unittest.main()
