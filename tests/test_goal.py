"""/goal (chat_goal): what a message asks, what a turn's end does to it, what the judge's words do; and a chat that works on
by itself until it is met, against a stub engine."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mac"))
try:
    from test_experiments import Base
except ImportError:
    from tests.test_experiments import Base
import chat_goal as G  # noqa: E402
import outbox  # noqa: E402

READY = {"state": "ready", "served_model": "dsv41-unc", "profile": {"id": "unc-chat", "model_id": "dsv41-unc", "settings": {"context": 16384}}}
NOT = {"summary": '{"verdict": "not_met", "reason": "the folder was not listed"}'}
MET = {"summary": '{"verdict": "met", "reason": "the list is shown"}'}
WORK = [{"calls": [("list_dir", {"path": "."})]}, {"text": "Listed it."}]


def facts(i, calls=1, **k):
    return dict({"id": i, "calls": calls, "stopped": False, "error": None, "circles": False, "own": True, "tokens": 10}, **k)


class Command(unittest.TestCase):
    def test_what_a_message_asks(self):
        self.assertEqual(G.command("/goal all tests pass\nand lint is clean "), ("set", "all tests pass\nand lint is clean"))
        self.assertEqual(G.command(" /goal "), ("show", None))
        for w in ("clear", "Stop", "off", "reset", "none", "cancel"):
            self.assertEqual(G.command(f"/goal {w}"), ("clear", None))
        for t in ("/goals now", "my /goal is", "goal x", "", None, "/goalx"):
            self.assertIsNone(G.command(t))
        self.assertEqual(G.command("/goal stop the server"), ("set", "stop the server"))
        self.assertRaises(ValueError, G.command, "/goal " + "x" * 4001)


class Turns(unittest.TestCase):
    def test_a_turn_is_judged_once(self):
        g = G.new("x", 100)
        self.assertEqual(G.after_turn(g, facts(5)), "judge")
        self.assertIsNone(G.after_turn(g, facts(5)))
        self.assertEqual((g["turns"], g["tokens"]), (1, 10))

    def test_stop_loops_and_errors_pause_and_their_next_message_goes_on(self):
        for k, why in (({"stopped": True}, "stop"), ({"circles": True}, "loop")):
            g = G.new("x")
            self.assertEqual(G.after_turn(g, facts(1, **k)), "pause")
            self.assertEqual((g["state"], g["pause"]), ("paused", why))
            self.assertEqual(G.after_turn(g, facts(2)), "judge")
            self.assertEqual(g["state"], "active")
        g = G.new("x")
        self.assertEqual([G.after_turn(g, facts(i, error="the model endpoint did not answer: refused"), now=0) for i in (1, 2, 3, 4)], ["retry"] * 3 + ["pause"])
        g = G.new("x")
        self.assertEqual(G.after_turn(g, facts(1, error="too long"), retryable=lambda e: False), "pause")
        g = G.new("x")
        self.assertEqual(G.after_turn(g, facts(1, error="This chat is too long for its model.")), "pause")  # a retry only repeats it
        self.assertTrue(G.retryable("the model endpoint did not answer: [Errno 61] Connection refused"))
        g = G.new("x")
        G.after_turn(g, facts(1, error="the model endpoint did not answer"), now=0)
        self.assertEqual((G.after_turn(g, facts(2)), g["retries"]), ("judge", 0))

    def test_three_turns_without_a_tool_pause_unless_it_is_met(self):
        g, not_met = G.new("x"), '{"verdict": "not_met", "reason": "no test was run"}'
        for i, want in ((1, "next"), (2, "next"), (3, "pause")):
            self.assertEqual(G.after_turn(g, facts(i, calls=0)), "judge")
            self.assertEqual(G.after_judge(g, not_met), want)
        g = G.new("x")
        for i in (1, 2, 3):
            G.after_turn(g, facts(i, calls=0))
        self.assertEqual(G.after_judge(g, '{"verdict": "met", "reason": "ok"}'), "met")
        g = G.new("x")
        for i, c in enumerate((0, 0, 1, 0, 0), 1):  # a call starts it afresh
            G.after_turn(g, facts(i, calls=c))
            self.assertEqual(G.after_judge(g, not_met), "next")

    def test_only_owners_own_message_takes_a_paused_goal_up(self):
        g = G.new("x")
        G.after_turn(g, facts(1, stopped=True))
        self.assertIsNone(G.after_turn(g, facts(2, own=False)))  # an agent's report woke the chat
        self.assertEqual((g["state"], g["pause"], g["seen"]), ("paused", "stop", 2))
        g["asks"] = G.ASKS
        self.assertEqual((G.after_turn(g, facts(3)), g["asks"]), ("judge", 0))
        ms = [{"id": 1, "role": "user", "content": "Team update", "meta": {"team": {"agents": ["a"]}}}, {"id": 2, "role": "assistant", "content": "ok", "meta": {}}]
        self.assertFalse(G.turn_facts(ms)["own"])
        self.assertTrue(G.turn_facts([{"id": 3, "role": "user", "content": "hi", "meta": {}}] + [dict(ms[1], id=4)])["own"])

    def test_a_loop_it_got_out_of_by_itself_does_not_pause_it(self):
        ms = [{"id": 1, "role": "user", "content": "hi", "meta": {}}, {"id": 2, "role": "assistant", "content": "ok", "meta": {"loops": 2, "parts": [{"tool": {"line": "x"}}]}}]
        self.assertFalse(G.turn_facts(ms)["circles"])
        ms[1]["meta"]["circles"] = True
        self.assertTrue(G.turn_facts(ms)["circles"])

    def test_a_finished_goal_is_left_alone(self):
        for s in ("met", "impossible", "cleared"):
            self.assertIsNone(G.after_turn(dict(G.new("x"), state=s), facts(9)))


class Judge(unittest.TestCase):
    def test_its_words(self):
        self.assertEqual(G.reply_of('{"verdict": "met", "reason": "12 tests passed"}'), ("met", "12 tests passed"))
        self.assertEqual(G.reply_of('Sure:\n```json\n{"verdict":"Not yet","reason":"lint fails"}\n```'), ("not_met", "lint fails"))
        self.assertEqual(G.reply_of('{"verdict": "impossible", "reason": "no such file"} ok')[0], "impossible")
        self.assertEqual(G.reply_of('{"verdict": "not_met", "reason": "main() {} is still empty"}'), ("not_met", "main() {} is still empty"))
        self.assertEqual(G.reply_of('{"verdict": "met", "reason": "returns {\\"a\\": {}}"}')[0], "met")
        # a quoted verdict is a quote; a stray quote, an odd escape, a line break or a cut-off end is still read
        self.assertEqual(G.reply_of('{"verdict": "not_met", "reason": "the file only says {"verdict": "met"} in it"}')[0], "not_met")
        self.assertIsNone(G.reply_of('{"verdict": "met", "reason": "it says {"verdict": "not_met"} too"}'))
        self.assertEqual(G.reply_of('Set {x}: {"verdict": "met", "reason": "a"}')[0], "met")
        self.assertEqual(G.reply_of('{"reason": "regex \\\\d+ matches C:\\\\tmp", "verdict": "met"}'), ("met", "regex \\d+ matches C:\\tmp"))
        for odd in ('{"verdict": "not_met", "reason": "a\\d pattern"}', '{"verdict": "not_met", "reason": "line\nbreak"}', '{"verdict": "not_met", "reason": "cut off here bec'):
            self.assertEqual(G.reply_of(odd)[0], "not_met", odd)
        self.assertIsNone(G.reply_of('{"verdict": "not_met", "reason": "a"} {"verdict": "met", "reason": "b"}'))  # two that differ
        self.assertEqual(G.reply_of('{"verdict": "met", "reason": "a"}\n{"verdict": "met", "reason": "b"}'), ("met", "a"))
        for bad in ("yes it is met", "{}", '{"verdict": "maybe"}', "", None, '{"verdict": met}'):
            self.assertIsNone(G.reply_of(bad))
        g = G.new("x")
        self.assertEqual([G.after_judge(g, "hm"), G.after_judge(g, "hm"), g["pause"]], ["again", "pause", "judge"])

    def test_it_reads_the_newest_turns_and_what_the_tools_returned(self):
        ms = []
        for i in range(6):
            ms += [{"role": "user", "content": f"ask {i}", "meta": {}},
                   {"role": "assistant", "content": f"did {i}", "meta": {"parts": [{"text": f"did {i}"}, {"tool": {"line": f"Ran {i}", "out": "x" * 5000 + f"exit {i}"}}]}}]
        s = G.shown(ms + [{"role": "assistant", "content": "gone", "hidden": True, "meta": {}}])
        self.assertIn("ask 5", s)
        self.assertIn("exit 5", s)
        self.assertNotIn("ask 2", s)
        self.assertNotIn("gone", s)
        self.assertLess(len(s), G.JUDGE_CHARS + 100)
        m = G.judge_messages(dict(G.new("ship it", 0), turns=4), ms, now=600)
        self.assertIn("4 turns, 10 minutes", m[1]["content"])
        self.assertIn("ship it", m[1]["content"])

    def test_status_lines(self):
        self.assertIn("No goal set", G.status(None))
        g = dict(G.new("ship it", 0), turns=2, reason="one test fails")
        self.assertIn("Goal active (2 turns, 5 min", G.status(g, 300))
        self.assertIn("Last check: one test fails", G.status(g, 300))
        G.pause(g, "stop")
        self.assertIn("you pressed Stop", G.status(g))


class Runs(Base):
    def setUp(self):
        super().setUp()
        self.state = dict(READY)
        self.box = outbox.Outbox(self.st, None, None, lambda fresh=True: self.state, lambda st: self.ep, lambda pid: None,
                                 chat_agent=self.agent, relay=self.agent.relay)
        self.box.goals = self.goals = G.Goals(self.st)
        self.c = self.chat("box")
        self.cid = self.c["id"]

    def until(self, done, secs=12):
        end = time.time() + secs
        while time.time() < end:
            self.box.tick()
            time.sleep(0.03)
            if done() and not self.box.running and not self.goals.judging:
                return
        self.fail(f"not in {secs} s: {self.goals.get(self.cid)} waiting {len(self.st.waiting(self.cid))} left {self.script}")

    def state_is(self, *states):
        return lambda: (self.goals.get(self.cid) or {}).get("state") in states

    def said(self):
        return [(m["content"], (m["meta"] or {}).get("goal")) for m in self.st.messages(self.cid) if m["role"] == "user"]

    def test_it_works_on_until_the_judge_says_met(self):
        self.script += WORK + [NOT] + WORK + [MET]
        r = self.goals.ask(self.cid, "/goal the folder is listed", "low")
        self.assertIn("Goal active", r["status"])
        self.until(self.state_is("met"))
        g = self.goals.get(self.cid)
        self.assertEqual((g["turns"], g["reason"]), (2, "the list is shown"))
        self.assertEqual([x[1] for x in self.said()], ["start", "next"])
        self.assertIn("the folder was not listed", self.said()[1][0])
        judged = [b for b in self.bodies if not b.get("stream")]
        self.assertEqual(len(judged), 2)
        self.assertNotIn("tools", judged[0])
        self.assertEqual(judged[0]["messages"][0]["content"], G.JUDGE)
        self.assertIn("the folder is listed", judged[0]["messages"][1]["content"])
        self.assertIn("Listed it.", judged[0]["messages"][1]["content"])
        for _ in range(5):  # over: nothing more is started
            self.box.tick()
        self.assertEqual((len(self.said()), self.st.waiting(self.cid)), (2, []))

    def test_their_message_goes_before_its_next_turn_and_is_judged_after(self):
        self.script += WORK
        self.goals.ask(self.cid, "/goal the folder is listed")
        self.box.tick()  # its first turn starts; only a tick after it ends would judge it
        end = time.time() + 12
        while (len(self.said()) != 1 or self.agent.relay.active or self.box.running) and time.time() < end:
            time.sleep(0.03)
        self.st.enqueue(self.cid, "also say hello", {"attachments": []})
        self.script[:] = [{"text": "Hello."}, MET]
        self.until(self.state_is("met"))
        self.assertEqual([x[1] for x in self.said()], ["start", None])

    def test_clear_takes_its_waiting_turn_away(self):
        self.state = {"state": "starting"}  # nothing serves: its first turn waits
        self.goals.ask(self.cid, "/goal x is done")
        self.assertEqual(len(self.st.waiting(self.cid)), 1)
        self.assertIn("cleared", self.goals.ask(self.cid, "/goal clear")["status"])
        self.assertEqual(self.st.waiting(self.cid), [])
        self.assertEqual(self.goals.ask(self.cid, "/goal clear")["status"], "No goal set")
        self.assertIsNone(self.goals.ask(self.cid, "hello"))

    def test_a_new_goal_replaces_the_old_one(self):
        self.state = {"state": "starting"}
        self.goals.ask(self.cid, "/goal one")
        self.goals.ask(self.cid, "/goal two")
        self.assertEqual([w["content"].split("\n")[0] for w in self.st.waiting(self.cid)], ["two"])

    def test_stop_pauses_it_and_their_next_message_takes_it_up(self):
        self.state = {"state": "starting"}
        self.goals.ask(self.cid, "/goal x is done")
        self.st.unqueue(self.st.waiting(self.cid)[0]["id"])
        self.st.add_message(self.cid, "user", "x is done", {"goal": "start"})
        self.st.add_message(self.cid, "assistant", "part", {"stopped": True})
        self.box.tick()
        self.assertEqual((self.goals.get(self.cid)["state"], self.goals.get(self.cid)["pause"]), ("paused", "stop"))
        self.assertEqual(self.st.waiting(self.cid), [])
        self.state = dict(READY)
        self.script += [{"text": "Right."}, MET]
        self.st.enqueue(self.cid, "go on", {"attachments": []})
        self.until(self.state_is("met"))

    def test_three_turns_of_words_pause_it(self):
        self.script += [{"text": "I listed it."}, NOT, {"text": "It is listed."}, NOT, {"text": "Listed."}, NOT]
        self.goals.ask(self.cid, "/goal the folder is listed")
        self.until(self.state_is("paused"))
        self.assertEqual((self.goals.get(self.cid)["pause"], self.goals.get(self.cid)["turns"]), ("idle", 3))
        self.assertEqual(self.st.waiting(self.cid), [])

    def test_a_judge_that_cannot_be_read_twice_pauses_it_and_impossible_ends_it(self):
        self.script += WORK + [{"summary": "hm"}, {"summary": "well"}]
        self.goals.ask(self.cid, "/goal x")
        self.until(self.state_is("paused"))
        self.assertEqual(self.goals.get(self.cid)["pause"], "judge")
        self.script[:] = WORK + [{"summary": '{"verdict": "impossible", "reason": "there is no such folder"}'}]
        self.goals.ask(self.cid, "/goal y")
        self.until(self.state_is("impossible"))

    def test_a_goal_that_breaks_never_holds_the_waiting_messages_up(self):
        def broken(box):
            raise RuntimeError("broken")
        self.goals.tick = broken
        self.script += [{"text": "Hello."}]
        self.st.enqueue(self.cid, "hi", {"attachments": []})
        self.until(lambda: any(m["role"] == "assistant" and m["content"] == "Hello." for m in self.st.messages(self.cid)))

    def test_a_check_that_gets_no_answer_is_asked_a_few_times_then_pauses(self):
        self.goals.WAIT_S, asked = 0, []
        def no(*a, **k):
            asked.append(1)
            raise RuntimeError("the model endpoint did not answer")
        self.script += WORK
        self.goals.ask(self.cid, "/goal x")
        self.agent._summarise = no
        self.until(self.state_is("paused"))
        self.assertEqual((len(asked), self.goals.get(self.cid)["pause"]), (G.ASKS, "judge"))
        for _ in range(5):
            self.box.tick()
        self.assertEqual(len(asked), G.ASKS)

    def test_after_stop_an_agents_report_leaves_it_paused(self):
        self.state = {"state": "starting"}
        self.goals.ask(self.cid, "/goal x is done")
        self.st.unqueue(self.st.waiting(self.cid)[0]["id"])
        self.st.add_message(self.cid, "user", "x is done", {"goal": "start"})
        self.st.add_message(self.cid, "assistant", "part", {"stopped": True})
        self.box.tick()
        self.st.add_message(self.cid, "user", "Team update", {"team": {"agents": ["a"]}})
        self.st.add_message(self.cid, "assistant", "Read it.", {})
        self.box.tick()
        self.assertEqual(self.goals.get(self.cid)["state"], "paused")
        self.assertEqual(self.st.waiting(self.cid), [])

    def test_it_outlives_a_restart(self):
        self.script += WORK
        self.goals.ask(self.cid, "/goal x")
        self.state_ready, self.goals.judging = None, {self.cid}  # the app goes away before the judge is asked
        end = time.time() + 8
        while time.time() < end and not [m for m in self.st.messages(self.cid) if m["role"] == "assistant" and m["content"].endswith("Listed it.")]:
            self.box.tick()
            time.sleep(0.03)
        while self.box.running:
            time.sleep(0.02)
        self.script[:] = [MET]
        self.box.goals = self.goals = G.Goals(self.st)
        self.until(self.state_is("met"))

    def test_not_in_private_chats_and_a_deleted_chat_is_forgotten(self):
        keep, self.st.is_private = self.st.is_private, lambda cid: True
        self.assertRaises(ValueError, self.goals.ask, self.cid, "/goal x")
        self.st.is_private = keep
        self.state = {"state": "starting"}
        self.goals.ask(self.cid, "/goal x")
        g = self.goals.get(self.cid)
        self.goals._put(self.cid, dict(g, due=True))
        self.st.delete_chat(self.cid)
        self.box.tick()
        self.assertIsNone(self.goals.get(self.cid))


if __name__ == "__main__":
    unittest.main()
