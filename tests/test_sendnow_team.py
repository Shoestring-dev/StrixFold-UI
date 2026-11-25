"""Send now and the user's mid-answer messages in a Team chat (review): a team update waiting ahead of the user's
message held it out of every step, and after Send now the outbox sent the update first; an outbox id comes back once the
table empties, so a Send now mark kept by id alone landed on a later message. Against the fake engine."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mac"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import outbox  # noqa: E402
from test_chat_tools import Base  # noqa: E402


def team_update(st, cid, text="Team update (from your agents, not from the user):\n\n### Check-in 1 ..."):
    return st.enqueue(cid, text, {"attachments": [], "team": {"agents": [], "checkins": ["a1"]}})


def owner_steer(st, cid, text="stop that and do this instead"):
    return st.enqueue(cid, text, {"attachments": [], "effort": None, "steer": True})


class SteerBehindTeamUpdate(Base):
    def test_owners_message_mid_answer_goes_in_even_with_a_team_update_waiting_ahead(self):
        c = self.chat("box")
        team_update(self.st, c["id"])  # an agent checked in while the lead answers: the update waits for the answer's end
        self.script += [{"text": "Checking.", "calls": [("run_command", {"command": "echo one"})],
                         "hook": lambda: owner_steer(self.st, c["id"])},
                        {"text": "Doing what the user said."}]
        self.turn(c)
        waiting = [w["content"] for w in self.st.waiting(c["id"])]
        # the user's words should reach the lead at its next step (the page says "it reads this after its current step")
        self.assertNotIn("stop that and do this instead", waiting, "the user's message stayed waiting behind the team update")


class OutboxOrder(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        import studio
        import chats
        self.tmp = tempfile.mkdtemp(prefix="hc-review-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.st = studio.Store(os.path.join(self.tmp, "S"))
        self.relay = chats.Relay()

    def test_after_send_now_the_outbox_sends_owners_message_before_a_team_update(self):
        c = self.st.create_chat(kind="chat", model="quick-chat")
        team_update(self.st, c["id"])
        jid = owner_steer(self.st, c["id"])
        self.relay.now[jid] = next(w["created"] for w in self.st.waiting(c["id"]) if w["id"] == jid)  # Send now pressed on
        # it: the answer stopped, the chat is free
        ready = {"state": "ready", "profile": {"model_id": "dsv41", "id": "quick-chat", "settings": {"context": 63488}}}
        ob = outbox.Outbox(self.st, None, None, lambda fresh=False: ready, lambda st_: "", lambda pid: None,
                           chat_agent=object(), relay=self.relay)
        sent = []
        ob.send = lambda cid, w, st, entry=None: sent.append(w["id"])
        ob.tick()
        self.assertEqual(sent, [jid], "the outbox sent the team update first; the user's Send now message kept waiting")


class ReusedIds(Base):
    def test_a_send_now_mark_stays_on_its_own_message_when_its_id_comes_back(self):
        c = self.chat("box")
        old = owner_steer(self.st, c["id"], "an earlier one")
        self.relay.now[old] = next(w["created"] for w in self.st.waiting(c["id"]) if w["id"] == old)
        self.st.unqueue(old)  # it left (sent or cancelled): its id is free again
        self.script += [{"text": "Checking.", "calls": [("run_command", {"command": "echo one"})],
                         "hook": lambda: self.assertEqual(owner_steer(self.st, c["id"], "a new one"), old)},
                        {"text": "Done, and the new one."}]
        self.turn(c)
        self.assertEqual(self.st.waiting(c["id"]), [], "the new message with the old id was never taken in")


class StopLog(unittest.TestCase):
    def test_a_stop_is_logged_by_who_stopped_it_not_as_an_error(self):
        import chat_team as CT
        self.assertEqual([CT._stop_kind(e) for e in ("stopped by the user", "stopped by the lead", "the watcher stopped it: no reply",
                                                     "the watcher wrapped it up: the same call", "stopped", None)],
                         ["by the user", "by the lead", "by the watcher", "by the watcher", "", ""])


if __name__ == "__main__":
    unittest.main()
