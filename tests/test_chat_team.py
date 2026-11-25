"""Team mode for a chat (mac/chat_team.py): the lead's tools, agents over the pairs with a slot kept for chats,
the chat's own tools or the sandbox, reports back as one growing team update, the cap, Stop, never in Private."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import chat_team as CT  # noqa: E402
import chat_tools as T  # noqa: E402
import studio  # noqa: E402


def ready(streams=4, model="dsv41-unc", pid="unc-swarm"):
    return {"state": "ready", "run_id": "r", "served_model": "dsv41",
            "profile": {"id": pid, "name": pid, "model_id": model, "settings": {"streams": streams}}}


class FakeApp:
    def __init__(self):
        self.states = {"halos": ready(1, pid="unc-chat"), "box-b": ready(), "box-c": ready()}
        self.posts = []
        self.owner = types.SimpleNamespace(typed_at=0.0)
        self.studio = {"agents": types.SimpleNamespace(owner=self.owner)}
        self.fake = True

    def pair_states(self, fresh=False):
        return [(n, n, self.states.get(n)) for n in ("halos", "box-b", "box-c")]

    def pair_state(self, name, fresh=False):
        return dict(self.states[name], pair=name) if self.states.get(name) else None

    def halod_state(self, fresh=False):
        return self.states.get("halos")

    def local_endpoint(self, st):
        return f"http://{(st or {}).get('pair') or 'halos'}/v1"

    def pair_post(self, name, path, body):
        self.posts.append((name, path, body))
        if path == "/api/start":
            self.states[name] = ready()
        return True, {"ok": True}


class FakeTools(T.Toolset):
    level = "box"

    def __init__(self, ctx, calls):
        self.ctx, self.calls = ctx, calls

    def specs(self):
        return [{"type": "function", "function": {"name": "run_command", "parameters": {}}}]

    def call(self, name, args, ctx):
        self.calls.append((ctx.team_agent, ctx.level, args.get("command")))
        return T.result(f"ran {args.get('command')}", "ran")


class Model:
    """An agent's DeepSeek: one command, then its report (slowed by `hold` while a test wants agents running)."""

    def __init__(self):
        self.endpoints, self.hold, self.mu = [], threading.Event(), threading.Lock()
        self.hold.set()

    def __call__(self, endpoint, body):
        self.hold.wait(10)
        with self.mu:
            self.endpoints.append(endpoint)
        brief = body["messages"][1]["content"]
        if body["messages"][-1]["role"] == "user" and "tools" in body:
            return {"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "echo hi"})}}]}}],
                "usage": {"completion_tokens": 10}}
        return {"choices": [{"message": {"content": f"REPORT for {brief[-12:]}"}}], "usage": {"completion_tokens": 20}}


class Outbox:
    def __init__(self):
        self.running = set()
        self.engine = types.SimpleNamespace(kick=threading.Event())


class Team(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-team-")
        self.st = studio.Store(os.path.join(self.tmp, "S"))
        self.app, self.calls, self.model = FakeApp(), [], Model()
        self.team = CT.Team(self.app, self.st, outbox=Outbox(), fake=True, post=self.model,
                            toolsets=lambda ctx: [FakeTools(ctx, self.calls)])
        self.addCleanup(setattr, self.team, "closed", True)
        self.chat = self.st.create_chat(kind="chat", model="unc-chat")
        self.st.set_perm(self.chat["id"], "direct", {"computer": "vps"})
        self.cid = self.chat["id"]

    def wait(self, cond, t=15):
        end = time.time() + t
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail(f"timed out: {self.team.view(self.cid)}")

    def test_spawn_run_report(self):
        self.team.set_cap(self.cid, 4)
        for i in range(3):
            self.team.spawn(self.cid, "direct", {"computer": "vps"}, f"agent {i}", f"do part {i} of the job", sandbox=(i == 2))
        self.wait(lambda: all(a["status"] == "done" for a in self.team.view(self.cid)["agents"]))
        v = self.team.view(self.cid)
        self.assertEqual([a["id"] for a in v["agents"]], ["a1", "a2", "a3"])
        pairs = {a["pair"] for a in v["agents"]}
        self.assertIn("halos", pairs)  # the main boxes first while no chat is there (their one stream lent to one agent)
        self.assertTrue(pairs <= {"halos", "box-b", "box-c"})
        levels = {c[0]: c[1] for c in self.calls}
        self.assertEqual(levels, {"a1": "direct", "a2": "direct", "a3": "box_web"})  # the chat's tools; sandboxed: the Container + web
        self.assertEqual(self.team.detail(self.cid, "a3")["opts"], {})  # a sandboxed agent gets none of the chat's computer
        self.assertIn("REPORT", self.team.detail(self.cid, "a1")["report"])
        rows = self.st.waiting(self.cid)
        self.assertTrue(rows and all(r["meta"].get("team") for r in rows))
        text = "\n".join(r["content"] for r in rows)
        for aid in ("a1", "a2", "a3"):
            self.assertIn(f"({aid}) finished", text)
        self.assertEqual(sum(len(r["meta"]["team"]["agents"]) for r in rows), 3)
        self.assertTrue(text.startswith("Team update (from your agents, not from the user)"))
        self.assertTrue(rows[-1]["content"].endswith("(No agents are working now.)"))

    def test_cap_reserve_and_queue(self):
        self.team.set_cap(self.cid, 8)
        self.app.states["box-c"] = None  # out of reach
        self.model.hold.clear()  # agents stay busy
        for i in range(5):
            self.team.spawn(self.cid, "box", {}, f"a{i}", "work")
        # an agent is "running" a moment before the conductor places it: wait for the four placed (review: the
        # full suite read one with no pair yet)
        self.wait(lambda: self.team.view(self.cid)["running"] == 5 and
                  sum(a["status"] == "running" and bool(a["pair"]) for a in self.team.view(self.cid)["agents"]) == 4)
        v = self.team.view(self.cid)
        self.assertEqual(sorted(a["status"] for a in v["agents"]), ["queued", "running", "running", "running", "running"])
        pairs = [a["pair"] for a in v["agents"] if a["status"] == "running"]
        self.assertEqual(sorted(pairs), ["box-b"] * 3 + ["halos"])  # box5 + box4: 4 slots, one kept; the main boxes' one lent
        self.assertFalse(any(p[0] == "halos" for p in self.app.posts))  # the main boxes are never switched away
        with self.assertRaises(ValueError):
            for i in range(4):
                self.team.spawn(self.cid, "box", {}, "more", "work")
        self.team.stop(self.cid, "a5")  # a queued agent stops at once
        self.assertEqual(self.team.detail(self.cid, "a5")["status"], "stopped")
        self.model.hold.set()
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)

    def test_agents_spread_over_the_pairs(self):
        """Never all on one pair while another idles: the main boxes first while no chat is there, then the pair with the most
        free slots, a tie to the later pair (chats go to the first)."""
        self.team.set_cap(self.cid, 8)
        self.model.hold.clear()
        for i in range(4):
            self.team.spawn(self.cid, "box", {}, f"a{i}", "work")
        self.wait(lambda: all(a["pair"] for a in self.team.view(self.cid)["agents"]))
        pairs = [a["pair"] for a in self.team.view(self.cid)["agents"]]
        self.assertEqual(sorted(pairs), ["box-b", "box-c", "box-c", "halos"])
        self.assertEqual(pairs[:2], ["halos", "box-c"])
        self.model.hold.set()
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)

    def test_saving_keeps_the_conversation_and_trimming_is_linear(self):
        """Review: the save after each step went through _fit at HISTORY_TOKENS, which trims in place, so every
        agent was cut to ~24K of its share after each step, and its counting (the whole conversation again for each
        message) held Python's lock for most of a second. Saving keeps it as it stands; _fit drops what it dropped
        before, counted once."""
        def convo(n):
            m = [{"role": "system", "content": "s" * 8000}, {"role": "user", "content": "brief"}]
            for i in range(n):
                m += [{"role": "assistant", "content": "", "reasoning_content": "thinking " * 60,
                       "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "run_command", "arguments": "{}"}}]},
                      {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 1500}]
            return m

        def old_fit(messages, budget):  # the counting before, for the same drops
            if CT.est_tokens(messages) > budget:
                for m in messages[2:]:
                    if CT.est_tokens(messages) <= budget * CT.FIT_TO:
                        break
                    if m.get("role") == "tool" and not m.get("_dropped"):
                        m["content"], m["_dropped"] = "(older output dropped to save room)", True
                    elif m.get("role") == "assistant" and m.get("reasoning_content"):
                        m.pop("reasoning_content")
            return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]

        live = convo(300)
        before = CT.est_tokens(live)
        saved = self.team._kept(live)
        self.assertEqual((CT.est_tokens(live), len(saved)), (before, len(live) - 1))  # nothing trimmed by a save
        self.assertNotIn("(older output dropped to save room)", json.dumps(saved))
        for budget in (40_000, 120_000, 250_000):
            a, b = convo(300), convo(300)
            self.assertEqual(self.team._fit(a, budget), old_fit(b, budget), budget)
        big = convo(1200)
        t0 = time.time()
        self.team._fit(big, 40_000)
        self.assertLess(time.time() - t0, 1.0)  # was tens of seconds at this length

    def test_trimming_keeps_the_start_for_many_steps(self):
        """Past HISTORY_TOKENS, old output goes down to FIT_TO of it in one go; the next steps send the same start (the
        engine's prompt cache resumes it) until it is past the cap again."""
        big = "x" * (CT.CHARS_PER_TOKEN * 4000)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "brief"}]
        for i in range(14):
            msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]}, {"role": "tool", "tool_call_id": f"c{i}", "content": big}]
        first = self.team._fit(msgs)
        self.assertLessEqual(CT.est_tokens(first), CT.HISTORY_TOKENS * CT.FIT_TO)
        starts = []
        for i in range(3):  # three more steps of 4K tokens each: no new drops, the same start
            msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"d{i}"}]}, {"role": "tool", "tool_call_id": f"d{i}", "content": big}]
            sent = self.team._fit(msgs)
            starts.append(json.dumps(sent[:len(first)]))
        self.assertEqual(len(set(starts)), 1)
        self.assertEqual(starts[0], json.dumps(first))

    def test_the_budget_follows_the_engines_own_count(self):
        """est_tokens ran ~1.35 of the engine's count, so agents trimmed at ~34K real tokens of a 46K budget and re-read
        ~24K cold every ~10 steps, the pair's other streams near 3 tok/s meanwhile (an agent at 3.2
        tok/s). Each reply's prompt_tokens now scales the budget; a reply too small to tell leaves it."""
        st = {"profile": {"settings": {"context": 262144, "streams": 4}}}
        a = {}
        before = self.team._history_budget(a, st, 1000)
        self.assertEqual(before, self.team._budget(st) - 1000)
        self.team._measured(a, 40_000, {"prompt_tokens": 29_600})
        self.assertEqual(a["_tok"], 0.74)
        after = self.team._history_budget(a, st, 1000)
        self.assertEqual(after, int(self.team._budget(st) / 0.74) - 1000)  # trims at the real budget, not ~34K of it
        self.team._measured(a, 40_000, {"prompt_tokens": 500})
        self.assertEqual(a["_tok"], 0.74)
        a["_fit_cap"] = 20_000  # the engine said the window was past: its count still wins
        self.assertEqual(self.team._history_budget(a, st, 1000), 19_000)

    def test_trimming_drops_old_thinking_too(self):
        """With tools offered the engine renders every earlier reply's thinking (TensorFold deepseek_v41 template.zig:
        drop = drop_thinking and not tools), so a thinking agent's history grows by its reasoning each step; trimming
        dropped only tool output and could never get under the budget."""
        think = "t" * (CT.CHARS_PER_TOKEN * 3000)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "brief"}]
        for i in range(20):
            msgs += [{"role": "assistant", "content": "", "reasoning_content": think, "tool_calls": [{"id": f"c{i}"}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": "short"}]
        sent = self.team._fit(msgs)
        self.assertLessEqual(CT.est_tokens(sent), CT.HISTORY_TOKENS * CT.FIT_TO)
        self.assertEqual(sent[-2]["reasoning_content"], think)  # the newest thinking stays

    def test_idle_pair_is_started(self):
        self.team.set_cap(self.cid, 8)
        self.app.states["box-c"] = {"state": "idle"}
        self.model.hold.clear()
        for i in range(5):  # one more than the main boxes' lent stream and box5 + box4's three
            self.team.spawn(self.cid, "box", {}, f"a{i}", "work")
        self.wait(lambda: ("box-c", "/api/start", {"profile": "unc-swarm", "replace": False}) in self.app.posts)
        self.wait(lambda: ("box-c", "/api/hold", {"on": True}) in self.app.posts)
        self.wait(lambda: sum(a["status"] == "running" for a in self.team.view(self.cid)["agents"]) == 5)
        self.model.hold.set()
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)

    def test_lead_tools_and_limits(self):
        ctx = T.TurnCtx(self.cid, self.st.chat(self.cid), self.st, "dsv41-unc", level="direct", fake=True)
        CT.TEAM, old = self.team, CT.TEAM
        self.addCleanup(setattr, CT, "TEAM", old)
        self.assertIsNone(CT.toolset(ctx))  # the chip is off
        self.team.set_cap(self.cid, 2)
        lead = CT.toolset(ctx)
        self.assertIsInstance(lead, CT.LeadTools)
        self.assertIn("up to 2 helper agents", lead.system_note(ctx))
        self.model.hold.clear()
        r = lead.call("spawn_agent", {"done_when": "it reports", "name": "x", "brief": "do x"}, ctx)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("Started a1", r["text"])
        lead.call("spawn_agent", {"done_when": "it reports", "name": "y", "brief": "do y"}, ctx)
        r = lead.call("spawn_agent", {"done_when": "it reports", "name": "z", "brief": "do z"}, ctx)
        self.assertEqual(r["status"], "refused")
        self.assertIn("cap is 2", r["text"])
        self.assertIn("a1 “x”", lead.call("list_agents", {}, ctx)["text"])
        self.assertEqual(lead.call("message_agent", {"agent": "a1", "text": "hurry"}, ctx)["status"], "ok")
        self.assertEqual(lead.call("stop_agent", {"agent": "a9"}, ctx)["status"], "refused")
        agent_ctx = T.TurnCtx(self.cid, self.st.chat(self.cid), self.st, "dsv41-unc", level="direct", fake=True)
        agent_ctx.team_agent = "a1"
        self.assertIsNone(CT.toolset(agent_ctx))  # agents can't start agents
        self.model.hold.set()
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)

    def test_private_and_bounds(self):
        priv = self.st.create_chat(kind="chat", model="unc-chat")
        real = self.st.chat
        self.st.chat = lambda cid: dict(real(cid), private=True) if cid == priv["id"] else real(cid)  # a Private chat
        with self.assertRaises(ValueError):
            self.team.set_cap(priv["id"], 4)
        ctx = T.TurnCtx(priv["id"], self.st.chat(priv["id"]), self.st, "dsv41-unc", private=True, fake=True)
        CT.TEAM, old = self.team, CT.TEAM
        self.addCleanup(setattr, CT, "TEAM", old)
        self.assertIsNone(CT.toolset(ctx))
        with self.assertRaises(ValueError):
            self.team.set_cap(self.cid, 5)  # not one of the chip's choices
        with self.assertRaises(ValueError):
            self.team.spawn(self.cid, "box", {}, "x", "brief")  # the chip is off
        self.team.set_cap(self.cid, 2)
        with self.assertRaises(ValueError):
            self.team.spawn(self.cid, "box", {}, "x", "  ")

    def test_restart_resumes_recent_agents(self):
        """The app's restart cuts off a working agent: it carries on from its last finished step (an old one doesn't)."""
        now = time.time()
        base = {"brief": "count the files", "sandbox": False, "level": "box", "opts": {}, "created": now - 120, "pair": "box-b",
                "pair_label": "box5 + box4", "calls": 1, "steps": 1, "out_tokens": 10, "busy_s": 1.0, "tps": 9.0, "report": None,
                "error": None, "work_s": 30.0}
        agents = [dict(base, id="a1", name="recent", status="running", started=now - 60, last_step=now - 30),
                  dict(base, id="a2", name="old", status="running", started=now - CT.RESUME_WITHIN_S - 600,
                       last_step=now - CT.RESUME_WITHIN_S - 60)]
        root = self.st.path("halo-teams")
        with open(os.path.join(root, f"{self.cid}.json"), "w") as f:
            json.dump(agents, f)
        os.makedirs(os.path.join(root, self.cid), exist_ok=True)
        convo = [{"role": "user", "content": "Your brief from the lead:\n\ncount the files"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                     "name": "run_command", "arguments": json.dumps({"command": "ls | wc -l"})}}]},
                 {"role": "tool", "tool_call_id": "c1", "content": "EARLIER RESULT 42"}]
        with open(os.path.join(root, self.cid, "a1.json"), "w") as f:
            json.dump({"log": [{"k": "start", "n": 1, "t": now - 60}], "messages": convo}, f)
        self.team.set_cap(self.cid, 4)
        seen = []
        def model(endpoint, body):
            seen.append(body["messages"])
            return {"choices": [{"message": {"content": "REPORT: 42 files"}}], "usage": {"completion_tokens": 5}}
        again = CT.Team(self.app, self.st, outbox=Outbox(), fake=True, post=model, toolsets=lambda ctx: [FakeTools(ctx, self.calls)])
        self.addCleanup(setattr, again, "closed", True)
        self.assertEqual(again.detail(self.cid, "a2")["status"], "interrupted")
        self.wait(lambda: again.detail(self.cid, "a1")["status"] == "done")
        d = again.detail(self.cid, "a1")
        self.assertEqual(d["report"], "REPORT: 42 files")
        text = json.dumps(seen[0])
        self.assertIn("EARLIER RESULT 42", text)  # its conversation so far
        self.assertIn("app restarted while you worked", text)
        self.assertEqual(d["steps"], 2)  # its budget carries on too
        self.assertEqual([e["k"] for e in d["log"]], ["start", "restart", "start", "place", "model", "end"])

    def test_restart_carries_over_an_older_save(self):
        """A running agent saved by the code before the timeline (its conversation in the chat's file) still carries on."""
        now = time.time()
        old = [{"id": "a1", "name": "old style", "brief": "the brief", "sandbox": False, "level": "box", "opts": {}, "status": "running",
                "max_steps": 200, "wall_s": 5400,  # the budgets every agent got earlier: dropped on the restart
                "created": now - 90, "started": now - 60, "pair": "box-b", "pair_label": "box-b", "calls": 1, "steps": 1, "out_tokens": 5,
                "busy_s": 1.0, "tps": 5.0, "doing": "x", "report": None, "error": None,
                "messages": [{"role": "user", "content": "Your brief from the lead:\n\nthe brief"},
                             {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                                 "name": "run_command", "arguments": "{}"}}]},
                             {"role": "tool", "tool_call_id": "c1", "content": "OLD STYLE RESULT"}]}]
        with open(os.path.join(self.st.path("halo-teams"), f"{self.cid}.json"), "w") as f:
            json.dump(old, f)
        self.team.set_cap(self.cid, 2)
        seen = []
        def model(endpoint, body):
            seen.append(json.dumps(body["messages"]))
            return {"choices": [{"message": {"content": "done"}}], "usage": {}}
        again = CT.Team(self.app, self.st, outbox=Outbox(), fake=True, post=model, toolsets=lambda ctx: [FakeTools(ctx, self.calls)])
        self.addCleanup(setattr, again, "closed", True)
        self.wait(lambda: again.detail(self.cid, "a1")["status"] == "done")
        self.assertIn("OLD STYLE RESULT", seen[0])
        self.assertNotIn("messages", json.load(open(os.path.join(self.st.path("halo-teams"), f"{self.cid}.json")))[0])
        self.assertEqual([again.detail(self.cid, "a1")[k] for k in ("max_steps", "wall_s")], [None, None])

    def test_agents_have_no_step_or_time_limit_and_their_whole_window(self):
        """An agent has no step or time cap unless the lead sets one (when the user asks), is told so, is never flagged
        near a limit, and keeps its slot's whole share of the window (it was 48K: 258K on unc-swarm's 1M x 4)."""
        self.team.set_cap(self.cid, 2)
        m = self._agent_model(lambda n, msgs: self._tool_reply(f"step {n}") if n <= 12 else
                              {"choices": [{"message": {"content": "REPORT all done"}}], "usage": {"completion_tokens": 3}})
        self.team.spawn(self.cid, "box", {}, "x", "long one")
        self.wait(lambda: self.team.detail(self.cid, "a1")["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["max_steps"], d["wall_s"]), ("done", None, None))
        self.assertGreaterEqual(d["steps"], 12)
        self.assertNotIn("near its limit", [e.get("state") for e in d["log"]])
        self.assertIn("no step or time limit", m["seen"][0][0]["content"])
        self.assertFalse(any("you have about" in x["content"] for msgs in m["seen"] for x in msgs if x["role"] == "user"))
        st = {"profile": {"settings": {"context": 1048576, "streams": 4}}}
        self.assertEqual(self.team._budget(st), 262144 - CT.REPLY_TOKENS - 2048)

    def test_timeline_follows_each_step(self):
        self.team.set_cap(self.cid, 2)
        self.team.spawn(self.cid, "box", {}, "x", "do x")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] == "done")
        d = self.team.detail(self.cid, "a1")
        self.assertEqual([e["k"] for e in d["log"]], ["queued", "start", "place", "model", "tool", "model", "end"])
        self.assertEqual(d["log"][2]["text"], "On halos")  # where the conductor put its first step (the main boxes first)
        tool = d["log"][4]
        self.assertEqual((tool["name"], tool["status"], tool["result"]), ("run_command", "ok", "ran echo hi"))
        self.assertIn('"command": "echo hi"', tool["args"])
        self.assertGreater(tool["u"], tool["n"])  # updated in place when it ended
        later = self.team.detail(self.cid, "a1", after=d["log"][4]["u"])["log"]
        self.assertEqual([e["k"] for e in later], ["model", "end"])  # only what came after
        self.assertEqual(self.team.detail(self.cid, "a1", after=d["seq"])["log"], [])
        v = self.team.view(self.cid)["agents"][0]
        self.assertNotIn("log", v)
        self.assertEqual(v["health"]["state"], "ok")
        w = self.team.view(self.cid)["watch"]  # the flags' explanations use the watcher's own numbers
        self.assertEqual((w["slow_min"], w["stall_min"], w["loop"], w["errs"], w["near_pct"], w["steps"], w["wall_min"]),
                         (CT.SLOW_S // 60, CT.STALL_S // 60, [CT.LOOP_WARN, CT.LOOP_STOP], [CT.ERRS_WARN, CT.ERRS_STOP], 80,
                          None, None))  # no step or time limit

    def _agent_model(self, step):
        """A model whose reply with tools is step(n, messages) (n from 1); without tools, or asked for its report (the
        tools still offered), it reports."""
        state = {"n": 0, "seen": [], "bodies": []}
        def model(endpoint, body):
            state["seen"].append(body["messages"])
            state["bodies"].append(body)
            if "tools" not in body or CT.FINAL_ASK in str(body["messages"][-1].get("content")):
                return {"choices": [{"message": {"content": "FINAL REPORT"}}], "usage": {"completion_tokens": 3}}
            state["n"] += 1
            return step(state["n"], body["messages"])
        self.team.post = model
        return state

    @staticmethod
    def _tool_reply(command):
        return {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}}]}}],
            "usage": {"completion_tokens": 4}}

    def test_watcher_wraps_up_a_loop(self):
        self.team.set_cap(self.cid, 2)
        m = self._agent_model(lambda n, msgs: self._tool_reply("same thing"))
        self.team.spawn(self.cid, "box", {}, "x", "loop")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual(d["status"], "stopped")
        self.assertIn("watcher wrapped it up", d["error"])
        self.assertEqual(d["report"], "FINAL REPORT")  # it still reports
        self.assertEqual(d["calls"], CT.LOOP_STOP)
        notes = [x["content"] for x in m["seen"][-1] if x["role"] == "user"]
        self.assertTrue(any("team's watcher" in t and "same result" in t for t in notes))
        self.assertEqual(d["health"]["state"], "looping")
        self.assertTrue(any(e["k"] == "watch" and e["state"] == "looping" for e in d["log"]))

    def test_watcher_wraps_up_failures(self):
        class Failing(FakeTools):
            def call(self, name, args, ctx):
                raise RuntimeError(f"broken {args.get('command')}")
        self.team.toolsets = lambda ctx: [Failing(ctx, self.calls)]
        self.team.set_cap(self.cid, 2)
        self._agent_model(lambda n, msgs: self._tool_reply(f"try {n}"))
        self.team.spawn(self.cid, "box", {}, "x", "fail")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["calls"], d["health"]["state"]), ("stopped", CT.ERRS_STOP, "erroring"))
        self.assertTrue(all(e["status"] == "failed" for e in d["log"] if e["k"] == "tool"))

    def test_watcher_near_the_end_and_slow_and_stalled(self):
        for k, v in (("MAX_STEPS", 5), ("SLOW_S", 0.3), ("STALL_S", 4.5)):
            self.addCleanup(setattr, CT, k, getattr(CT, k))
            setattr(CT, k, v)
        self.team.set_cap(self.cid, 3)
        m = self._agent_model(lambda n, msgs: self._tool_reply(f"step {n}"))
        self.team.spawn(self.cid, "box", {}, "x", "long")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["steps"], d["report"]), ("done", 6, "FINAL REPORT"))  # 5 steps, then the report
        last, before = m["bodies"][-1], m["bodies"][-2]  # the report's request keeps the prompt's start: tools and thinking
        self.assertEqual((last["tools"], last["chat_template_kwargs"]), (before["tools"], before["chat_template_kwargs"]))
        self.assertEqual(last["max_tokens"], CT.REPLY_TOKENS * 2)
        self.assertTrue(any("you have about" in x["content"] for x in m["seen"][4] if x["role"] == "user"))  # after 4 of 5
        gate = threading.Event()
        self.addCleanup(gate.set)
        def hang(endpoint, body):
            gate.wait(10)
            return {"choices": [{"message": {"content": "late"}}], "usage": {"completion_tokens": 1}}
        self.team.post = hang
        self.team.spawn(self.cid, "box", {}, "y", "hang")
        self.wait(lambda: self.team.detail(self.cid, "a2")["health"]["state"] == "slow")
        self.assertIn("The model has been answering", self.team.detail(self.cid, "a2")["health"]["line"])
        self.wait(lambda: self.team.detail(self.cid, "a2")["health"]["state"] == "stalled")
        gate.set()
        self.wait(lambda: self.team.detail(self.cid, "a2")["status"] == "stopped")
        self.assertIn("watcher stopped it", self.team.detail(self.cid, "a2")["error"])

    def test_an_agent_that_stops_short_is_sent_back(self):
        """Announcing a step and ending: back to it (it does it); a false alarm (DONE) keeps its earlier words as the report."""
        self.team.set_cap(self.cid, 2)
        def step(n, msgs):
            if n == 1:
                return {"choices": [{"message": {"content": "I have the plan. Let me run the tests:"}}], "usage": {}}
            if n == 2:
                return self._tool_reply("python3 -m unittest")
            if n == 3:
                return {"choices": [{"message": {"content": "All tests pass. Now I will write up: it works."}}], "usage": {}}
            return {"choices": [{"message": {"content": "DONE"}}], "usage": {}}
        m = self._agent_model(step)
        self.team.spawn(self.cid, "box", {}, "x", "test it")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["calls"]), ("done", 1))  # it ran the tests it announced
        self.assertEqual(d["report"], "All tests pass. Now I will write up: it works.")  # not DONE
        self.assertEqual(m["seen"][1][-1]["content"], CT.NUDGE["said"])
        self.assertEqual(sum(1 for e in d["log"] if e["k"] == "watch" and e["state"] == "nudged"), 2)

    def test_a_long_agent_is_sent_back_each_time_it_stops_short(self):
        """NUDGES counted over the agent's whole life: after two stop-shorts anywhere in a 120-step run, the third ended it
        with 'Let me run the tests:' as its report. Each stretch of work gets its own."""
        self.team.set_cap(self.cid, 2)
        def step(n, msgs):
            if n in (1, 3, 5):
                return {"choices": [{"message": {"content": f"Part {n} next. Let me run it:"}}], "usage": {}}
            if n in (2, 4, 6):
                return self._tool_reply(f"run part {n}")
            return {"choices": [{"message": {"content": "REPORT: all three parts ran"}}], "usage": {}}
        self._agent_model(step)
        self.team.spawn(self.cid, "box", {}, "x", "three parts")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["calls"], d["report"]), ("done", 3, "REPORT: all three parts ran"))

    def test_a_call_that_needs_owners_tap_is_explained_to_the_agent(self):
        """An agent's ask is a Deny nobody gave: the tool's 'the user did not allow this ... say what you wanted to do
        instead' read as the user's no and ended agents early; it is told it can't ask and goes on without it."""
        class Asks(FakeTools):
            def call(self, name, args, ctx):
                if ctx.ask({"command": args.get("command")}) != "allow":
                    return T.result("the user did not allow this. It did not run. Don't try it another way; say what you wanted "
                                    "to do instead.", "Not run", "denied", "tap_denied")
                return T.result("ran", "ran")
        self.team.toolsets = lambda ctx: [Asks(ctx, self.calls)]
        self.team.set_cap(self.cid, 2)
        m = self._agent_model(lambda n, msgs: self._tool_reply("sudo rm -rf build") if n == 1 else
                              {"choices": [{"message": {"content": "REPORT"}}], "usage": {}})
        self.team.spawn(self.cid, "box", {}, "x", "clean")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        self.assertEqual(m["seen"][1][-1]["content"], CT.DENIED)

    def test_every_call_in_a_reply_gets_an_answer(self):
        """Six calls in one reply (2% of DeepSeek's replies with tools, rev-model): the first STEP_CALLS run and the rest are
        answered 'not run', so the history never holds a call without its result."""
        self.team.set_cap(self.cid, 2)
        def step(n, msgs):
            if n == 1:
                return {"choices": [{"message": {"content": "", "tool_calls": [
                    {"id": f"c{i}", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": f"echo {i}"})}}
                    for i in range(6)]}}], "usage": {}}
            return {"choices": [{"message": {"content": "REPORT"}}], "usage": {}}
        m = self._agent_model(step)
        self.team.spawn(self.cid, "box", {}, "x", "many")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        sent = m["seen"][1]
        ids = [c["id"] for x in sent if x["role"] == "assistant" for c in x.get("tool_calls") or []]
        answered = [x["tool_call_id"] for x in sent if x["role"] == "tool"]
        self.assertEqual(ids, answered)
        self.assertEqual(len(self.calls), T.STEP_CALLS)
        self.assertIn("Not run", sent[-1]["content"])

    def test_an_agents_call_written_after_its_words_is_sent_back(self):
        """Gate review: "Now let me run it." and a <tool_call> block ended the agent "done" with 0 calls and the
        block as its report. It is sent back (NUDGE "stray"); it makes the call; or, if it was an example, DONE ends it
        with its words as the report."""
        self.team.set_cap(self.cid, 2)
        block = "Now let me run it.\n<tool_call>\n" + json.dumps({"name": "run_command", "arguments": {"command": "echo hi"}}) + "\n</tool_call>"
        def step(n, msgs):
            if n == 1:
                return {"choices": [{"message": {"content": block}}], "usage": {}}
            if n == 2:
                return self._tool_reply("echo hi")
            return {"choices": [{"message": {"content": "REPORT: it printed hi"}}], "usage": {}}
        m = self._agent_model(step)
        self.team.spawn(self.cid, "box", {}, "x", "say hi")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["report"]), ("done", "REPORT: it printed hi"))
        self.assertEqual([c[2] for c in self.calls], ["echo hi"])  # once, from the real call
        self.assertEqual(m["seen"][1][-1]["content"], CT.NUDGE["stray"])

    def test_an_agents_example_call_then_done_is_its_report(self):
        self.team.set_cap(self.cid, 2)
        ex = "A call looks like this:\n<tool_call>\n" + json.dumps({"name": "run_command", "arguments": {"command": "ls"}}) + "\n</tool_call>"
        def step(n, msgs):
            return {"choices": [{"message": {"content": ex if n == 1 else "DONE"}}], "usage": {}}
        self._agent_model(step)
        self.team.spawn(self.cid, "box", {}, "x", "explain calls")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["report"]), ("done", ex))
        self.assertEqual(self.calls, [])  # the example never ran

    def test_an_agents_call_written_out_as_words_runs(self):
        """The engine hands back a call it couldn't read as the reply's words (DeepSeek's DSML): it runs; one too garbled
        to read is sent back to be made again."""
        self.team.set_cap(self.cid, 2)
        dsml = ("Checking.\n<｜DSML｜ calls>\n<｜DSML｜ invoke name=\"run_command\">\n<｜DSML｜ parameter "
                "name=\"command\" string=\"true\">ls -la</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n</｜DSML｜ calls>\n\n")
        def step(n, msgs):
            if n == 1:
                return {"choices": [{"message": {"content": dsml}}], "usage": {}}
            if n == 2:
                return {"choices": [{"message": {"content": "Next.\n\n<｜DSML｜ calls>\n<｜DSML｜ invoke"}}], "usage": {}}
            if n == 3:
                return self._tool_reply("pwd")
            return {"choices": [{"message": {"content": "REPORT: listed it"}}], "usage": {}}
        m = self._agent_model(step)
        self.team.spawn(self.cid, "box", {}, "x", "list it")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["report"]), ("done", "REPORT: listed it"))
        self.assertEqual([c[2] for c in self.calls], ["ls -la", "pwd"])
        said = m["seen"][1][-2]  # the call it wrote out, as a call, and only its words before
        self.assertEqual((said["content"], said["tool_calls"][0]["function"]["name"]), ("Checking.", "run_command"))
        self.assertEqual(m["seen"][2][-1]["content"], CT.NUDGE["garbled"])
        self.assertNotIn("DSML", json.dumps(m["seen"][3]))

    def test_a_cut_off_agent_reply_carries_on(self):
        self.team.set_cap(self.cid, 2)
        def step(n, msgs):
            if n == 1:
                return {"choices": [{"message": {"content": "The report starts here and"}, "finish_reason": "length"}], "usage": {}}
            return {"choices": [{"message": {"content": " ends here."}, "finish_reason": "stop"}], "usage": {}}
        self._agent_model(step)
        self.team.spawn(self.cid, "box", {}, "x", "report")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        self.assertEqual(self.team.detail(self.cid, "a1")["report"], "The report starts here and ends here.")

    def _lead(self, cap=4):
        self.team.set_cap(self.cid, cap)
        ctx = T.TurnCtx(self.cid, self.st.chat(self.cid), self.st, "dsv41-unc", level="direct", fake=True)
        CT.TEAM, old = self.team, CT.TEAM
        self.addCleanup(setattr, CT, "TEAM", old)
        return CT.toolset(ctx), ctx

    def test_the_lead_has_claudes_agent_tools(self):
        """Roles, fork, workdir, thinking; wait in its answer for reports (no team update then); read a report or its
        steps; address agents by name; a follow-up to a finished agent picks up its conversation; stop all."""
        class Writes(FakeTools):
            def specs(self):
                return super().specs() + [{"type": "function", "function": {"name": "write_file", "parameters": {}}}]
        self.team.toolsets = lambda ctx: [Writes(ctx, self.calls)]
        self.st.add_message(self.cid, "user", "We are building the inv package; tests live in tests/.", {})
        lead, ctx = self._lead()
        self.assertEqual({x["function"]["name"] for x in lead.specs()},
                         {"spawn_agent", "wait_agents", "get_agent", "list_agents", "message_agent", "stop_agent"})
        seen = []
        def model(endpoint, body):
            seen.append(body)
            if "tools" in body and body["messages"][-1]["role"] == "user":
                return {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
                    "name": "run_command", "arguments": json.dumps({"command": "ls"})}}]}}], "usage": {"completion_tokens": 3}}
            return {"choices": [{"message": {"content": f"REPORT {len(seen)}"}}], "usage": {"completion_tokens": 3}}
        self.team.post = model
        r = lead.call("spawn_agent", {"done_when": "it reports", "name": "scout", "brief": "find the tests", "role": "explore", "fork": True,
                                      "workdir": "agents/scout", "thinking": False, "max_minutes": 10, "max_steps": 20}, ctx)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("explore, forked, in agents/scout/", r["text"])
        r = lead.call("wait_agents", {"agents": ["scout"], "timeout_minutes": 1}, ctx)
        self.assertIn("“scout” (a1) finished", r["text"])
        self.assertIn("REPORT", r["text"])
        self.assertEqual(self.st.waiting(self.cid), [])  # it came back in the answer, not as a team update
        first = seen[0]
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["run_command"])  # explore: nothing that writes
        self.assertIn("You are an explorer", first["messages"][0]["content"])
        self.assertIn("agents/scout/", first["messages"][0]["content"])
        self.assertIn("We are building the inv package", first["messages"][1]["content"])  # forked: the chat's words
        self.assertFalse(first["chat_template_kwargs"]["enable_thinking"])
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["max_steps"], d["wall_s"]), (20, 600))
        self.assertIn("REPORT", lead.call("get_agent", {"agent": "a1"}, ctx)["text"])
        steps = lead.call("get_agent", {"agent": "scout", "what": "steps"}, ctx)["text"]  # by name
        self.assertIn("- ran [ok]: ran ls", steps)
        self.assertIn("place: On halos", steps)
        n = len(seen)
        r = lead.call("message_agent", {"agent": "scout", "text": "now count them"}, ctx)  # a follow-up: it picks up again
        self.assertIn("picks up again", r["text"])
        self.wait(lambda: self.team.detail(self.cid, "a1")["status"] == "done" and len(seen) > n)
        after = json.dumps(seen[n]["messages"])
        self.assertIn("REPORT 2", after)  # its own earlier report is in its conversation
        self.assertIn("now count them", after)
        self.model.hold.clear()
        self.team.post = self.model
        lead.call("spawn_agent", {"done_when": "it reports", "name": "x", "brief": "x"}, ctx)
        lead.call("spawn_agent", {"done_when": "it reports", "name": "y", "brief": "y"}, ctx)
        self.wait(lambda: sum(a["status"] == "running" for a in self.team.view(self.cid)["agents"]) == 2)
        self.assertIn("Stopping 2 agents", lead.call("stop_agent", {"agent": "all"}, ctx)["text"])
        self.model.hold.set()
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)
        self.assertEqual(lead.call("get_agent", {"agent": "nobody"}, ctx)["status"], "refused")

    def test_owner_writes_to_an_agent_and_runs_it_again(self):
        self.team.set_cap(self.cid, 2)
        gate = threading.Event()
        self.addCleanup(gate.set)
        def step(n, msgs):
            if n == 1:
                gate.wait(10)
            return self._tool_reply("ls") if n == 1 else {"choices": [{"message": {"content": "OK, done"}}], "usage": {}}
        m = self._agent_model(step)
        self.team.spawn(self.cid, "box", {}, "x", "the brief", sandbox=True)
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] == "running")
        self.team.message(self.cid, "a1", "use the docs folder", who="owner")
        with self.assertRaises(ValueError):
            self.team.message(self.cid, "a1", "  ", who="owner")
        with self.assertRaises(ValueError):
            self.team.retry(self.cid, "a1")  # still working
        gate.set()
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] == "done")
        self.assertTrue(any("A note from the user" in x["content"] and "docs folder" in x["content"] for x in m["seen"][-1] if x["role"] == "user"))
        note = [e for e in self.team.detail(self.cid, "a1")["log"] if e["k"] == "note"][0]
        self.assertEqual((note["who"], note["read"]), ("owner", True))
        b = self.team.retry(self.cid, "a1")
        self.assertEqual((b["id"], b["brief"], b["sandbox"], b["retry_of"], b["level"]), ("a2", "the brief", True, "a1", "box"))
        self.wait(lambda: self.team.detail(self.cid, "a2")["status"] == "done")


    # -- review: how agents end and how the lead hears back
    def test_a_follow_up_after_wait_reaches_the_lead(self):
        """wait_agents, then message_agent (a follow-up) and wait_agents again: the follow-up's report comes back (it
        was marked heard by the first wait and the second wait swallowed it: never in the answer, never a team update)."""
        lead, ctx = self._lead()
        n = {"i": 0}
        def model(endpoint, body):
            n["i"] += 1
            return {"choices": [{"message": {"content": f"REPORT {n['i']}"}}], "usage": {"completion_tokens": 3}}
        self.team.post = model
        lead.call("spawn_agent", {"done_when": "it reports", "name": "scout", "brief": "find it"}, ctx)
        self.assertIn("REPORT 1", lead.call("wait_agents", {"agents": ["scout"], "timeout_minutes": 1}, ctx)["text"])
        lead.call("message_agent", {"agent": "scout", "text": "now count them"}, ctx)
        r = lead.call("wait_agents", {"agents": ["scout"], "timeout_minutes": 1}, ctx)
        self.assertIn("REPORT 2", r["text"])

    def test_the_halos_are_lent_again_when_their_agent_ends_or_the_lead_waits(self):
        """Review: an ended agent stayed the main boxes' tenant for TENANT_S (they idled while others waited), and a
        lead waiting for its agents kept them between its steps (conductor.between)."""
        lead, ctx = self._lead()
        self.team.post = lambda endpoint, body: {"choices": [{"message": {"content": "REPORT"}}], "usage": {}}
        lead.call("spawn_agent", {"done_when": "it reports", "name": "scout", "brief": "find it"}, ctx)
        self.assertIn("REPORT", lead.call("wait_agents", {"agents": ["scout"], "timeout_minutes": 1}, ctx)["text"])
        self.assertEqual(self.team.view(self.cid)["agents"][0]["pair"], "halos")
        self.assertIsNone(self.team.cond.tenant)  # it ended: lent to the next at once
        self.team.cond.between[self.cid] = time.time()  # the lead's last step on the main boxes just ended
        lead.call("wait_agents", {}, ctx)
        self.assertNotIn(self.cid, self.team.cond.between)  # waiting, it lets them go

    def test_wait_agents_fits_every_report(self):
        """Three long reports in one wait_agents: each agent's report is in what the lead reads (the chat cuts a tool's
        result at OUT_MODEL characters, so the third was lost)."""
        lead, ctx = self._lead()
        def model(endpoint, body):
            name = body["messages"][1]["content"].split("part ")[1][:2]  # (its brief ends with its finish line now)
            return {"choices": [{"message": {"content": f"RESULT-{name} " + "x" * 9000 + f" END-{name}"}}], "usage": {}}
        self.team.post = model
        for i in range(3):
            lead.call("spawn_agent", {"done_when": "it reports", "name": f"r{i}", "brief": f"part p{i}"}, ctx)
        r = lead.call("wait_agents", {"timeout_minutes": 1}, ctx)
        self.assertLessEqual(len(r["text"]), T.OUT_MODEL)
        for i in range(3):
            self.assertIn(f"RESULT-p{i}", r["text"])
            self.assertIn(f"(a{i + 1}) finished", r["text"])
        self.assertIn("get_agent", r["text"])  # how to read the rest
        full = lead.call("get_agent", {"agent": "a3"}, ctx)["text"]
        self.assertIn("END-p2", full)

    def test_a_stopped_agent_reports_what_it_did(self):
        """stop_agent: it stops what it is doing and writes its report (seven agents stopped after 13 h came
        back as their last 12 calls); its running call is cut, not waited out."""
        lead, ctx = self._lead()
        gate, cut = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        class Long(FakeTools):
            def call(self, name, args, ctx):
                if args.get("command") == "make long":  # a long command: Stop cuts it (ctx.stop), as a real tool's
                    cut.set() if ctx.stop.wait(10) else None
                    return T.result("stopped", "Stopped", "stopped")
                return super().call(name, args, ctx)
        self.team.toolsets = lambda ctx: [Long(ctx, self.calls)]
        def step(n, msgs):
            return self._tool_reply("make long" if n == 3 else f"grep -n TODO file{n}.py")
        m = self._agent_model(step)
        lead.call("spawn_agent", {"done_when": "it reports", "name": "x", "brief": "find the TODOs"}, ctx)
        self.wait(lambda: self.team.detail(self.cid, "a1")["doing"] == "Make long" or self.team.detail(self.cid, "a1")["calls"] == 3)
        self.assertIn("writes its report", lead.call("stop_agent", {"agent": "a1"}, ctx)["text"])
        self.wait(lambda: self.team.detail(self.cid, "a1")["status"] not in CT.LIVE)
        self.assertTrue(cut.is_set())  # its running command was stopped at once
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["error"], d["report"], d["stopping"]), ("stopped", "stopped by the lead", "FINAL REPORT", None))
        asked = [x["content"] for x in m["seen"][-1] if x["role"] == "user"][-1]
        self.assertTrue(asked.startswith("Your work is stopped (stopped by the lead).") and CT.FINAL_ASK in asked, asked)
        self.assertTrue(any(e["k"] == "watch" and e["state"] == "stopping" for e in d["log"]))
        self.assertIn("(a1) was stopped", "\n".join(r["content"] for r in self.st.waiting(self.cid)))

    def test_stop_now_and_a_report_that_never_comes(self):
        """Stop now (or no report WRAP_GRACE_S after Stop): at once; what comes back is its latest check-in and last
        calls. Stop all twice: the second is at once."""
        self.addCleanup(setattr, CT, "WRAP_GRACE_S", CT.WRAP_GRACE_S)
        CT.WRAP_GRACE_S = 0.5
        self.team.set_cap(self.cid, 4)
        gate = threading.Event()
        self.addCleanup(gate.set)
        def model(endpoint, body):
            last = str(body["messages"][-1].get("content"))
            if CT.FINAL_ASK in last:
                gate.wait(10)  # its report never comes (the model hangs)
                return {"choices": [{"message": {"content": "late report"}}], "usage": {}}
            return self._tool_reply("step")
        self.team.post = model
        self.team.spawn(self.cid, "box", {}, "x", "work")
        self.wait(lambda: self.team.detail(self.cid, "a1")["calls"] >= 2)
        a = self.team._find(self.cid, "a1")
        self.team._checkin(self.cid, a, "CHECK-IN: did 2 steps; left: the rest " + "y" * 80)
        self.team.stop(self.cid, "a1", "stopped by the user")
        self.wait(lambda: self.team.detail(self.cid, "a1")["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["error"]), ("stopped", "stopped by the user"))
        self.assertIn("Its latest check-in (1,", d["report"])
        self.assertIn("CHECK-IN: did 2 steps", d["report"])
        self.assertIn("ran step", d["report"])  # and its last calls
        self.assertTrue(any(e["k"] == "watch" and "after it was asked to stop" in (e.get("text") or "") for e in d["log"]))
        gate.set()
        # Stop all: asked for reports; again at once
        gate.clear()
        for i in range(2):
            self.team.spawn(self.cid, "box", {}, f"y{i}", "work")
        CT.WRAP_GRACE_S = 60
        self.wait(lambda: all(self.team.detail(self.cid, x)["calls"] >= 1 for x in ("a2", "a3")))
        self.assertEqual(self.team.stop_all(self.cid), 2)
        self.wait(lambda: all(self.team.detail(self.cid, x)["stopping"] == "wrap" for x in ("a2", "a3")))
        self.assertIn("[stopping: writing its report]", self.team.lead_view(self.cid))
        self.assertEqual(self.team.stop_all(self.cid), 2)  # again: at once
        self.wait(lambda: all(self.team.detail(self.cid, x)["status"] == "stopped" for x in ("a2", "a3")))
        self.assertTrue(all(self.team.detail(self.cid, x)["error"] == "stopped by the user" for x in ("a2", "a3")))
        gate.set()

    def test_a_long_agent_checks_in_and_carries_on(self):
        """An hour on (CHECKIN_S): a check-in, kept and sent to the lead as a team update, and its work carries on; the
        calls it made in that reply don't run; it reports when done; its check-in leaves the update once its report is in."""
        self.addCleanup(setattr, CT, "CHECKIN_S", CT.CHECKIN_S)
        CT.CHECKIN_S = 0.6
        self.team.set_cap(self.cid, 2)
        state = {"n": 0, "checkins": 0, "seen": []}
        def model(endpoint, body):
            state["seen"].append(body["messages"])
            last = str(body["messages"][-1].get("content"))
            if CT.FINAL_ASK in last or "tools" not in body:
                return {"choices": [{"message": {"content": "FINAL REPORT"}}], "usage": {}}
            if last.startswith("A note from the team's watcher:") and "Write a check-in" in last:
                state["checkins"] += 1
                return {"choices": [{"message": {"content": f"CHECK-IN {state['checkins']}: done steps 1-{state['n']}; left: more. " + "z" * 80,
                                                 "tool_calls": [{"id": "k", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "NOT RUN"})}}]}}],
                        "usage": {}}
            state["n"] += 1
            if state["n"] >= 8 and state["checkins"] >= 2:
                return {"choices": [{"message": {"content": "All done: the report. " + "r" * 40}}], "usage": {}}
            time.sleep(0.2)
            return self._tool_reply(f"step {state['n']}")
        self.team.post = model
        self.team.spawn(self.cid, "box", {}, "long", "a long job", done_when="the tests pass")
        self.assertTrue(self.team.detail(self.cid, "a1")["brief"].endswith("\n\nDone when: the tests pass"))
        # (_checkin records the check-in on the agent, then _wake writes the team update: wait for the update itself)
        ups = lambda: [r for r in self.st.waiting(self.cid) if (r["meta"].get("team") or {}).get("checkins")]  # noqa: E731
        self.wait(lambda: self.team.detail(self.cid, "a1")["checkin"] is not None and ups())
        rows = ups()
        self.assertTrue(rows, self.st.waiting(self.cid))
        self.assertEqual((rows[-1]["meta"]["team"]["agents"], rows[-1]["meta"]["team"]["checkins"]), ([], ["a1"]))
        self.assertIn("### Check-in 1 from agent “long” (a1), still working", rows[-1]["content"])
        self.assertIn(CT.CHECKIN_LEAD, rows[-1]["content"])
        self.wait(lambda: self.team.detail(self.cid, "a1")["status"] == "done", t=30)
        d = self.team.detail(self.cid, "a1")
        self.assertTrue(d["report"].startswith("All done"))
        self.assertGreaterEqual(len(d["checkins"]), 2)
        self.assertEqual([c["n"] for c in d["checkins"]], list(range(1, len(d["checkins"]) + 1)))
        self.assertTrue(d["checkins"][0]["text"].startswith("CHECK-IN 1"))
        self.assertNotIn("checkins", self.team.detail(self.cid, "a1", after=d["seq"]))  # the panel's poll: only when new
        self.assertEqual([e["ck"] for e in d["log"] if e["k"] == "checkin"], [c["n"] for c in d["checkins"]])  # its number, not the event's
        self.assertNotIn("NOT RUN", [c[2] for c in self.calls])  # the calls in its check-in reply didn't run
        after = [x["content"] for x in state["seen"][-1] if x["role"] == "user"]
        self.assertTrue(any(t.startswith(CT.CHECKIN_THANKS) and CT.CHECKIN_DROPPED in t for t in after))
        waiting = lambda: "\n".join(r["content"] for r in self.st.waiting(self.cid))  # noqa: E731
        self.wait(lambda: "(a1) finished" in waiting())  # (the update is written just after it ends)
        self.assertNotIn("### Check-in", waiting())  # its report is in: its check-in isn't sent as well
        self.assertIn("(Its previous check-in,", self.team.checkin_text(self.cid, self.team._find(self.cid, "a1")))

    def test_the_lead_gives_every_agent_a_finish_line(self):
        lead, ctx = self._lead()
        spec = next(x for x in lead.specs() if x["function"]["name"] == "spawn_agent")["function"]["parameters"]
        self.assertIn("done_when", spec["required"])
        r = lead.call("spawn_agent", {"name": "x", "brief": "do x"}, ctx)
        self.assertEqual(r["status"], "refused")
        self.assertIn("done_when is empty", r["text"])
        self.assertEqual(self.team.view(self.cid)["agents"], [])
        r = lead.call("spawn_agent", {"name": "x", "brief": "do x", "done_when": "x.txt exists"}, ctx)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("Done when: x.txt exists", self.team.detail(self.cid, "a1")["brief"])
        self.assertIn("finish line", lead.system_note(ctx))
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)

    def test_a_notice_after_hours_of_work(self):
        """LONG_NOTICE_S of work: one notice to the user (content-free: ids only), never twice."""
        self.addCleanup(setattr, CT, "LONG_NOTICE_S", CT.LONG_NOTICE_S)
        CT.LONG_NOTICE_S = 0.3
        got = []
        self.app.studio_notify = got.append
        self.team.set_cap(self.cid, 3)
        gate = threading.Event()
        self.addCleanup(gate.set)
        def model(endpoint, body):
            if CT.FINAL_ASK in str(body["messages"][-1].get("content")) or gate.is_set():
                return {"choices": [{"message": {"content": "FINAL REPORT"}}], "usage": {}}
            time.sleep(0.1)
            return self._tool_reply(f"secret step words {len(body['messages'])}")  # (never the same call: no loop)
        self.team.post = model
        for i in range(2):
            self.team.spawn(self.cid, "box", {}, f"x{i}", "secret brief words")
        self.wait(lambda: got)
        time.sleep(3)  # the scheduler ticks every 2 s: one notice, never again
        gate.set()
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)
        self.assertEqual(len(got), 1, got)
        self.assertIn("2 Team agents (a1, a2) have worked", got[0])
        self.assertNotIn("secret", got[0])
        self.assertTrue(all(self.team.detail(self.cid, x)["long_told"] for x in ("a1", "a2")))

    def test_a_reply_cut_at_its_cap_gets_twice_the_room_next(self):
        """At 4096 tokens always, an agent writing a big file in one call was cut, told to make the call again, and cut
        at the same place (18 replies of 4096 tokens, 2-7 min each, one prompt cut twice in a row)."""
        self.team.set_cap(self.cid, 2)
        seen = []
        def model(endpoint, body):
            seen.append(body)
            if len(seen) == 1:  # a call cut off mid-file
                return {"choices": [{"message": {"content": "<｜DSML｜ calls>\n<｜DSML｜ invoke name=\"run_command\">\n<｜DSML｜ "
                                                          "parameter name=\"command\" string=\"true\">cat > big.py <<EOF"},
                                     "finish_reason": "length"}], "usage": {"completion_tokens": 4096}}
            if len(seen) == 2:
                return self._tool_reply("echo done")
            return {"choices": [{"message": {"content": "REPORT: wrote it"}}], "usage": {"completion_tokens": 5}}
        self.team.post = model
        self.team.spawn(self.cid, "box", {}, "x", "write a big file")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        self.assertEqual([b["max_tokens"] for b in seen[:3]], [CT.REPLY_TOKENS, CT.REPLY_TOKENS * 2, CT.REPLY_TOKENS * 2])
        self.assertEqual(self.calls, [("a1", "box", "echo done")])  # the cut call never ran
        self.assertIn("next gets 8,192 tokens", json.dumps(self.team.detail(self.cid, "a1")["log"]))

    def test_after_a_cut_its_working_steps_are_not_called_its_report(self):
        """Review: after one cut its reply's room stays at 8192, and every working step after it showed as
        "Writing its report" in the Agents panel (the label went by the reply's size)."""
        self.team.set_cap(self.cid, 2)
        doing = []
        def model(endpoint, body):
            doing.append(self.team.chats[self.cid][0]["doing"])
            if len(doing) == 1:  # cut off mid-call
                return {"choices": [{"message": {"content": "<｜DSML｜ calls>\n<｜DSML｜ invoke name=\"run_command\">"},
                                     "finish_reason": "length"}], "usage": {"completion_tokens": 4096}}
            if len(doing) <= 3:
                return self._tool_reply(f"echo {len(doing)}")
            return {"choices": [{"message": {"content": "REPORT: done"}}], "usage": {"completion_tokens": 5}}
        self.team.post = model
        self.team.spawn(self.cid, "box", {}, "x", "write a big file")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        self.assertEqual(doing[:3], ["Thinking"] * 3)

    def test_an_agent_with_the_document_tools_is_told_the_chats_pdfs(self):
        """Review: the chat's PDFs (their file_ids) moved from the document tools' system note to the words after
        the user's newest message (chat_docs.turn_note), which an agent never sees: it had read_pages and no file_id.
        They go with its brief now (its prompt's start stays the same for its whole run)."""
        import chat_docs
        import filetext
        exe = filetext.helper()
        if not exe:
            self.skipTest("no swiftc on this Mac")
        d = tempfile.mkdtemp(prefix="hc-team-pdf-")
        subprocess.run([exe, "make-form", os.path.join(d, "form.pdf")], check=True, capture_output=True, timeout=120)
        made = self.st.add_document(self.cid, open(os.path.join(d, "form.pdf"), "rb").read(), "lease.pdf")
        self.team.toolsets = lambda ctx: [t for t in (chat_docs.toolset(ctx),) if t is not None]
        seen = []
        def model(endpoint, body):
            seen.append(body)
            return {"choices": [{"message": {"content": "REPORT: read it"}}], "usage": {"completion_tokens": 5}}
        self.team.post = model
        self.team.set_cap(self.cid, 2)
        self.team.spawn(self.cid, "box", {}, "reader", "Read the lease PDF in this chat and list its fields.")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        self.assertIn("read_pages", [t["function"]["name"] for t in seen[0].get("tools") or []])
        self.assertIn(made["id"], seen[0]["messages"][1]["content"])
        self.assertNotIn(made["id"], seen[0]["messages"][0]["content"])  # never in its system words

    def test_the_last_report_is_never_empty(self):
        """Out of steps, its last reply (asked for its report, the tools still offered so the prompt's start stays) makes
        a call instead: it is asked once more, without tools or thinking, and with nothing then either (a call written as
        words), the lead gets what its timeline shows instead of '(the agent ended without a report)'."""
        for k, v in (("MAX_STEPS", 2),):
            self.addCleanup(setattr, CT, k, getattr(CT, k))
            setattr(CT, k, v)
        self.team.set_cap(self.cid, 2)
        dsml = ("Let me check once more.\n<｜DSML｜ calls>\n<｜DSML｜ invoke name=\"run_command\">\n<｜DSML｜ parameter "
                "name=\"command\" string=\"true\">ls</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n</｜DSML｜ calls>")
        seen = []
        def model(endpoint, body):
            seen.append(body)
            if "tools" in body:
                return self._tool_reply(f"wc -l part{len(seen)}.txt")
            return {"choices": [{"message": {"content": dsml}}], "usage": {}}
        self.team.post = model
        self.team.spawn(self.cid, "box", {}, "x", "count lines")
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertNotIn("ended without a report", d["report"])
        self.assertIn("wc -l part1.txt", d["report"])
        finals = [b for b in seen if any(CT.FINAL_ASK in str(m.get("content")) for m in b["messages"])]
        self.assertEqual(len(finals), 2)  # asked once more
        self.assertEqual(("tools" in finals[0], finals[0]["chat_template_kwargs"]["enable_thinking"]), (True, True))
        self.assertEqual(("tools" in finals[1], finals[1]["chat_template_kwargs"]["enable_thinking"]), (False, False))

    def test_waiting_for_a_slot_is_not_a_stall(self):
        """An agent waiting for a free slot (every pair's agent slots held) is not 'stalled': the watcher stopped it
        with 'No step finished for 20 min' while it had not been given a slot."""
        for k, v in (("STALL_S", 1.0),):
            self.addCleanup(setattr, CT, k, getattr(CT, k))
            setattr(CT, k, v)
        self.team.set_cap(self.cid, 4)
        held = [self.team.cond.acquire(CT.MODEL_ID, "agent", f"other{i}") for i in range(7)]  # every agent slot
        self.assertTrue(all(held))
        self.team.spawn(self.cid, "box", {}, "x", "work")
        self.wait(lambda: self.team.detail(self.cid, "a1")["doing"] == "Waiting for a free slot"
                  and self.team.detail(self.cid, "a1")["status"] == "running")
        time.sleep(3.5)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual(d["status"], "running", d.get("error"))
        for x in held:
            self.team.cond.release(x)
        self.wait(lambda: self.team.detail(self.cid, "a1")["status"] == "done")

    def test_a_note_sent_as_it_finishes_is_read(self):
        """message_agent while its last reply is being written ('reads your note before its next step'): it carries
        on with the note instead of ending without reading it."""
        lead, ctx = self._lead()
        gate, inside = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        seen = []
        def model(endpoint, body):
            seen.append(body)
            if len(seen) == 1:
                inside.set()
                gate.wait(10)
                return {"choices": [{"message": {"content": "REPORT without the note"}}], "usage": {}}
            return {"choices": [{"message": {"content": "REPORT with the note"}}], "usage": {}}
        self.team.post = model
        lead.call("spawn_agent", {"done_when": "it reports", "name": "x", "brief": "do x"}, ctx)
        self.assertTrue(inside.wait(10))
        self.assertIn("reads your note", lead.call("message_agent", {"agent": "a1", "text": "also do y"}, ctx)["text"])
        gate.set()
        self.wait(lambda: self.team.detail(self.cid, "a1")["status"] not in CT.LIVE)
        self.assertEqual(self.team.detail(self.cid, "a1")["report"], "REPORT with the note")
        self.assertIn("also do y", json.dumps(seen[-1]["messages"]))

    def test_an_agent_end_leaves_a_line_in_the_app_log(self):
        """Like a chat's turn (1ccd809): how an agent ended, in the app's log (never its words)."""
        import contextlib
        import io
        self.team.set_cap(self.cid, 2)
        def broken(endpoint, body):
            raise RuntimeError("engine fell over")
        self.team.post = broken
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.team.spawn(self.cid, "box", {}, "x", "secret brief words")
            self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
            time.sleep(0.2)
        line = out.getvalue()
        self.assertIn("team: agent a1", line)
        self.assertIn("failed", line)
        self.assertIn("RuntimeError", line)
        self.assertNotIn("secret brief", line)
        self.assertNotIn("fell over", line)  # the error's words never reach the log (gate review)
        # the error kinds the log keeps: a type or an HTTP status, never the words after them
        self.assertEqual([CT._error_kind(e) for e in (
            "RuntimeError: grep -r PLUMTREE ~/private-notes", "HTTPError: HTTP Error 400: Bad Request {\"prompt\": \"x\"}",
            "the watcher wrapped it up: the same call (\u201cRunning: grep -r PLUMTREE ~/private-notes\u201d)", "Timeout", "")],
            ["RuntimeError", "HTTP 400", "an error", "Timeout", "an error"])

    def test_a_too_long_prompt_is_fitted_and_sent_again(self):
        """The engine refuses a prompt longer than its window (HTTP 400 with its counts): the agent fits its history to
        the engine's count and goes on (it failed with 'HTTPError: HTTP Error 400: Bad Request')."""
        import io
        import urllib.error
        self.team.set_cap(self.cid, 2)
        big = "y" * 30_000
        seen = []
        def model(endpoint, body):
            seen.append(body)
            if len(seen) == 1:
                return self._tool_reply("cat big.txt")
            if len(seen) == 2:
                err = json.dumps({"error": {"message": "maximum context length is 20000 tokens, but the rendered prompt "
                                                       "has 26000 tokens"}}).encode()
                raise urllib.error.HTTPError(endpoint, 400, "Bad Request", {}, io.BytesIO(err))
            return {"choices": [{"message": {"content": "REPORT: fitted"}}], "usage": {}}
        self.team.post = model
        class Big(FakeTools):
            def call(self, name, args, ctx):
                return T.result(big, "ran")
        self.team.toolsets = lambda ctx: [Big(ctx, self.calls)]
        self.team.spawn(self.cid, "box", {}, "x", "read it", thinking=False)
        self.wait(lambda: self.team.view(self.cid)["agents"][0]["status"] not in CT.LIVE)
        d = self.team.detail(self.cid, "a1")
        self.assertEqual((d["status"], d["report"]), ("done", "REPORT: fitted"), d.get("error"))
        self.assertLess(len(json.dumps(seen[2]["messages"])), len(json.dumps(seen[1]["messages"])))


    def _lead_turn(self, script):
        """One answer of the chat's lead through halo_chat's tool loop (the stub engine of test_chat_tools), with this
        test's team as the app's; returns the requests it sent."""
        import chats
        import halo_chat
        from test_chat_tools import engine
        self.team.set_cap(self.cid, 2)
        self.st.set_perm(self.cid, "none", {})
        CT.TEAM, old = self.team, CT.TEAM
        self.addCleanup(setattr, CT, "TEAM", old)
        bodies = []
        srv = engine(list(script), bodies)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        agent = halo_chat.ChatAgent(self.st, chats.Relay())
        agent.app = types.SimpleNamespace(fake=True)
        list(agent.turn(self.cid, f"http://127.0.0.1:{srv.server_address[1]}/v1", "served", "quick-chat", "split the job"))
        return bodies

    def test_a_lead_waiting_for_its_agents_is_not_sent_back(self):
        """'I'll report back when they finish.' after spawning: the turn ends (the reports start the next answer). The
        NUDGE 'said' sent it back with 'do it now with your tools', which led it into wait_agents (the chat held up to
        25 min, the user's messages waiting behind it)."""
        self.model.hold.clear()  # the agent keeps working
        self.addCleanup(self.model.hold.set)
        bodies = self._lead_turn([{"calls": [("spawn_agent", {"name": "scan", "brief": "scan the repo", "done_when": "it reports"})]},
                                  {"text": "Started a1 to scan the repo. I'll report back when it finishes."}])
        self.assertEqual(len(bodies), 2, [b["messages"][-1] for b in bodies])
        self.assertIn("starts your next answer", bodies[1]["messages"][-1]["content"])
        self.model.hold.set()
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)

    def test_a_lead_that_stops_short_is_still_sent_back(self):
        """With agents working, a lead that announces its own next step ('Let me read the config:') still goes back."""
        self.model.hold.clear()
        self.addCleanup(self.model.hold.set)
        bodies = self._lead_turn([{"calls": [("spawn_agent", {"name": "scan", "brief": "scan the repo", "done_when": "it reports"})]},
                                  {"text": "Started a1. Now let me read the config file:"},
                                  {"text": "DONE"}])
        self.assertEqual(len(bodies), 3)
        self.model.hold.set()
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)

    def test_a_message_from_owner_ends_the_wait(self):
        """wait_agents ends when the user sends a message (a steer), so the user's words go in at once instead of after
        the wait."""
        lead, ctx = self._lead()
        self.model.hold.clear()
        self.addCleanup(self.model.hold.set)
        lead.call("spawn_agent", {"done_when": "it reports", "name": "x", "brief": "do x"}, ctx)
        threading.Timer(1.0, lambda: self.st.enqueue(self.cid, "stop, do y first", {"attachments": [], "steer": True})).start()
        t0 = time.time()
        r = lead.call("wait_agents", {"timeout_minutes": 5}, ctx)
        self.assertLess(time.time() - t0, 10)
        self.assertIn("the user wrote while you waited", r["text"])
        self.model.hold.set()
        self.wait(lambda: self.team.view(self.cid)["running"] == 0)

    def test_wait_takes_a_report_out_of_the_waiting_team_update(self):
        """An agent that finished before wait_agents (its report already in a waiting team update): the wait returns it
        and the team update goes, so the lead gets it once, in this answer."""
        lead, ctx = self._lead()
        lead.call("spawn_agent", {"done_when": "it reports", "name": "fast", "brief": "quick one"}, ctx)
        self.wait(lambda: self.team.detail(self.cid, "a1")["status"] == "done" and self.st.waiting(self.cid))
        r = lead.call("wait_agents", {"timeout_minutes": 1}, ctx)
        self.assertIn("(a1) finished", r["text"])
        self.assertEqual(self.st.waiting(self.cid), [])
        again = lead.call("wait_agents", {"agents": ["a1"], "timeout_minutes": 1}, ctx)
        self.assertIn("you have had every report", again["text"])
        self.assertIn("REPORT", lead.call("get_agent", {"agent": "a1"}, ctx)["text"])  # still readable


if __name__ == "__main__":
    unittest.main()
