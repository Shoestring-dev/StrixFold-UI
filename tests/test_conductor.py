"""The conductor (mac/conductor.py): every model request's slot and pair. Agents never take an extra pair's last slot,
chats never wait; chats (and their leads) go to the main boxes, and agents too while no chat is there (their one stream lent
to one agent at a time, given back to a chat after the step in flight), else to the extra pairs; a conversation keeps its
pair (prompt cache) while it has room and moves, said why, when it hasn't; a pair that fails to connect is set aside;
holds end after agents stop needing the pair."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import conductor as C  # noqa: E402

M = "dsv41-unc"


def ready(streams=4, model=M, pid="unc-swarm", context=262144):
    return {"state": "ready", "profile": {"id": pid, "model_id": model, "settings": {"streams": streams, "context": context}}}


class App:
    def __init__(self):
        self.states = {"halos": ready(1, pid="unc-chat", context=63488), "box-b": ready(), "box-c": ready()}
        self.posts = []

    def pair_states(self, fresh=False):
        labels = {"halos": "box1 + box2", "box-b": "box5 + box4", "box-c": "box3 + box6"}
        return [(n, labels[n], dict(self.states[n], pair=n) if self.states.get(n) else None) for n in ("halos", "box-b", "box-c")]

    def local_endpoint(self, st):
        return f"http://{st['pair']}/v1"

    def pair_post(self, name, path, body):
        self.posts.append((name, path, body))
        return True, {}


class Conductor(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.c = C.Conductor(self.app)

    def test_agents_leave_a_slot_for_chats_and_chats_never_wait(self):
        self.app.states["box-c"] = self.app.states["halos"] = None
        held = [self.c.acquire(M, "agent", f"a{i}") for i in range(3)]
        self.assertEqual({x["pair"] for x in held}, {"box-b"})
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        self.assertIsNone(self.c.acquire(M, "agent", "a3", stop=stop))  # the 4th slot is the user's
        chat = self.c.track("http://box-b/v1", "chat", "chat1")
        self.assertEqual(chat["pair"], "box-b")
        self.assertEqual(self.c.busy("box-b"), 4)
        more = self.c.track("http://box-b/v1", "chat", "chat2")  # a chat is never held back
        self.assertIsNotNone(more)
        self.assertEqual(self.c.agent_slots(M), 3)

    def test_an_agent_waits_for_a_slot_then_takes_it(self):
        self.app.states["box-c"] = self.app.states["halos"] = None
        held = [self.c.acquire(M, "agent", f"a{i}") for i in range(3)]
        got = []
        t = threading.Thread(target=lambda: got.append(self.c.acquire(M, "agent", "late", on_wait=lambda: got.append("waiting"))))
        t.start()
        time.sleep(0.3)
        self.assertEqual(got, ["waiting"])
        self.c.release(held[0])
        t.join(3)
        self.assertEqual(got[1]["pair"], "box-b")

    def test_a_conversation_keeps_its_pair_then_moves_and_says_why(self):
        self.app.states["halos"] = None
        first = self.c.acquire(M, "agent", "x", label="fizz (a1)")
        self.assertEqual(first["pair"], "box-c")  # a tie: the later pair (chats prefer the first)
        self.c.release(first)
        again = self.c.acquire(M, "agent", "x", label="fizz (a1)")
        self.assertEqual(again["pair"], "box-c")  # its prompt cache is there
        self.c.release(again)
        fill = [self.c.track("http://box-c/v1", "chat", f"c{i}") for i in range(3)]  # the user's chats fill box3 + box6
        moved = self.c.acquire(M, "agent", "x", label="fizz (a1)")
        self.assertEqual((moved["pair"], moved["prev"]), ("box-b", "box-c"))
        self.assertIn("fizz (a1) moved to box5 + box4: its pair is full", self.c.moves("x")[0]["text"])
        for f in fill:
            self.c.release(f)

    def test_a_pair_that_fails_is_set_aside(self):
        self.app.states["halos"] = None
        a = self.c.acquire(M, "agent", "x")
        self.assertEqual(a["pair"], "box-c")
        self.c.release(a, ok=False)
        b = self.c.acquire(M, "agent", "x")
        self.assertEqual(b["pair"], "box-b")
        self.assertIn("set aside", self.c.moves("x")[0]["text"])
        self.c.release(b)
        self.c.away["box-c"] = time.time() - 1  # its minute is up
        self.assertEqual(self.c.choose(M, "agent", "new")[0], "box-c")
        self.assertTrue(self.c.view()["pairs"][2]["away"] is False)

    def test_the_halos_one_stream_is_lent_to_one_agent_while_no_chat_is_there(self):
        """the lead waited on its agents and the main boxes' one stream was kept for chats."""
        stop = threading.Event()
        stop.set()
        agents = [self.c.acquire(M, "agent", f"a{i}") for i in range(7)]
        self.assertEqual([x["pair"] for x in agents].count("halos"), 1)  # one agent at a time there
        self.assertEqual(agents[0]["pair"], "halos")  # the first goes there
        self.assertEqual(sorted(x["pair"] for x in agents[1:]), ["box-b"] * 3 + ["box-c"] * 3)
        self.assertEqual(self.c.agent_slots(M), 7)
        h = self.app.states["halos"]
        self.assertTrue(self.c.halos_free(h, "chat1"))  # an agent there never keeps a chat off
        chat = self.c.track("http://halos/v1", "chat", "chat1")
        self.c.release(agents[0])
        self.assertIsNone(self.c.acquire(M, "agent", "a0", stop=stop))  # a chat is there and the extra pairs are full
        self.assertEqual(self.c.agent_slots(M), 6)
        self.c.release(agents[1])
        moved = self.c.acquire(M, "agent", "a0")
        self.assertEqual(moved["pair"], agents[1]["pair"])  # it moves where there is room, said why
        self.assertIn("moved to", self.c.moves("a0")[0]["text"])
        self.c.release(moved)
        self.c.release(chat)
        self.c.turn_over("chat1")  # the chat's turn ended
        self.c.release(agents[2])
        self.assertEqual(self.c.acquire(M, "agent", "a2")["pair"], agents[2]["pair"])  # its own pair, not the main boxes (a0's)
        back = self.c.acquire(M, "agent", "a0")
        self.assertEqual(back["pair"], "halos")  # the chat is done: the main boxes' agent goes back
        self.assertIn("the main boxes come first while they are free", self.c.moves("a0")[0]["text"])  # never "its pair is full"
        self.c.release(back)
        self.c.tenant = ("a0", time.time() - C.TENANT_S - 1)  # a0 hasn't used them for two minutes: another may
        self.assertEqual(self.c.acquire(M, "agent", "new")["pair"], "halos")

    def test_a_chats_turn_keeps_the_halos_between_its_steps(self):
        """Review: a chat's lease covers one step; while its tools ran between steps an agent was lent the stream,
        and the chat's next step (sent to the main boxes: halos_free ignores agents) waited behind a whole agent
        step."""
        h = self.app.states["halos"]
        step1 = self.c.track("http://halos/v1", "chat", "chat1")
        self.c.release(step1)  # its tool runs now
        stop = threading.Event()
        stop.set()
        a = self.c.acquire(M, "agent", "a0")
        self.assertNotEqual(a["pair"], "halos")  # an agent goes to the extra pairs meanwhile
        self.assertTrue(self.c.halos_free(h, "chat1"))
        self.assertEqual(self.c.agent_slots(M), 6)  # nor is one admitted for the main boxes
        self.c.release(a)
        self.c.turn_over("chat1")  # the turn ended (or its lead waits for its agents): lent again at once
        self.assertEqual(self.c.acquire(M, "agent", "a1")["pair"], "halos")
        self.assertEqual(self.c.agent_slots(M), 7)  # the main boxes count again

    def test_a_step_long_ago_keeps_nothing(self):
        step1 = self.c.track("http://halos/v1", "chat", "chat1")
        self.c.release(step1)
        self.c.between["chat1"] -= C.CHAT_HOLD_S  # its tool has run a minute: the main boxes are lent meanwhile
        self.assertEqual(self.c.acquire(M, "agent", "a0")["pair"], "halos")

    def test_a_finished_agent_leaves_the_halos_to_the_next(self):
        """Review: the tenant was never cleared when its agent ended; the main boxes sat idle for TENANT_S while another
        agent, the extra pairs full, waited."""
        a0 = self.c.acquire(M, "agent", "a0")
        self.assertEqual(a0["pair"], "halos")
        bos = [self.c.acquire(M, "agent", f"b{i}") for i in range(6)]
        self.assertEqual({x["pair"] for x in bos}, {"box-b", "box-c"})
        self.c.release(a0)
        self.c.done("a0")  # it wrote its report (chat_team._end)
        stop = threading.Event()
        threading.Timer(1.5, stop.set).start()
        got = self.c.acquire(M, "agent", "a6", stop=stop)
        self.assertIsNotNone(got)
        self.assertEqual(got["pair"], "halos")

    def test_one_agent_at_a_time_on_the_halos_even_for_one_that_was_there(self):
        """Review: an agent that ran there before took the main boxes from their tenant whenever it was between steps
        (its own pair first), and the two traded the one stream, a cold read of a whole prompt each time."""
        a0 = self.c.acquire(M, "agent", "a0")
        self.c.release(a0)
        self.c.tenant = ("a0", time.time() - C.TENANT_S - 1)  # a0 in a long tool call: the main boxes go to another
        a1 = self.c.acquire(M, "agent", "a1")
        self.assertEqual(a1["pair"], "halos")
        self.c.release(a1)  # a1, the tenant now, runs a tool between its steps
        back = self.c.acquire(M, "agent", "a0")
        self.assertNotEqual(back["pair"], "halos")

    def test_the_halos_with_streams_to_spare_take_agents_first(self):
        self.app.states["halos"] = ready(4)
        agents = [self.c.acquire(M, "agent", f"a{i}") for i in range(4)]
        self.assertEqual([x["pair"] for x in agents].count("halos"), 3)  # their 4th is the user's
        self.assertEqual(self.c.choose(M, "chat", "chat1")[0], "halos")  # a chat: the main boxes first
        h = ready(1, pid="unc-chat")
        self.assertTrue(self.c.halos_free(h, "chat1"))
        mine = self.c.track("http://halos/v1", "chat", "chat1")
        self.assertTrue(self.c.halos_free(h, "chat1"))  # its own request doesn't count against it
        self.assertFalse(self.c.halos_free(h, "chat2"))  # another chat's does
        self.c.release(mine)
        self.assertFalse(self.c.halos_free(None, "chat1"))

    def test_a_chat_goes_to_its_own_pair_or_the_least_busy(self):
        b, c = dict(ready(), pair="box-b"), dict(ready(), pair="box-c")
        self.assertEqual(self.c.prefer("chat1", [b, c])["pair"], "box-b")  # nothing yet: the first
        busy = [self.c.acquire(M, "agent", f"a{i}") for i in range(2)]  # one on each
        self.c.release(busy[0])
        self.assertEqual(self.c.prefer("chat1", [b, c])["pair"], "box-c" if busy[1]["pair"] == "box-b" else "box-b")
        self.c.track("http://box-b/v1", "chat", "chat2")
        self.assertEqual(self.c.prefer("chat2", [b, c])["pair"], "box-b")  # its own while it has room

    def test_holds_end_after_agents_stop_needing_the_pair(self):
        self.c.hold("box-c")
        self.c.tick(needed=True)
        self.c.tick(needed=False)
        self.assertEqual(self.app.posts, [])
        self.c.held["box-c"] = time.time() - C.RELEASE_AFTER_S - 1
        self.c.tick(needed=False)
        self.assertEqual(self.app.posts, [("box-c", "/api/hold", {"on": False})])
        self.assertIn("hold is off", self.c.view()["log"][0]["text"])


    def test_a_slow_pair_state_never_holds_the_lock(self):
        """An agent waiting for a slot reads the pairs' states (a hung tunnel: up to 5 s each) outside the lock, so a
        chat's release isn't held behind it."""
        self.app.states["box-c"] = None
        held = [self.c.acquire(M, "agent", f"a{i}") for i in range(3)]
        slow, inside = threading.Event(), threading.Event()
        real = self.app.pair_states
        def pair_states(fresh=False):
            if slow.is_set() and threading.current_thread().name == "waiter":
                inside.set()
                time.sleep(1.5)
            return real(fresh)
        self.app.pair_states = pair_states
        stop = threading.Event()
        self.addCleanup(stop.set)
        slow.set()
        threading.Thread(target=self.c.acquire, args=(M, "agent", "waiter"), kwargs={"stop": stop}, name="waiter",
                         daemon=True).start()
        self.assertTrue(inside.wait(5))  # the waiter is inside a slow read now
        time.sleep(0.1)
        t0 = time.time()
        self.c.release(held[0])
        self.assertLess(time.time() - t0, 0.5)
        slow.clear()


if __name__ == "__main__":
    unittest.main()
