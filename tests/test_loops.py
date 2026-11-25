"""Replies that go round in circles (chat_loops): stopped as they stream, sent back once, kept out of the history. A
stub engine; no model."""
try:
    import _tmp  # noqa: F401
except ImportError:
    from tests import _tmp  # noqa: F401
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
try:
    from test_experiments import Base
except ImportError:
    from tests.test_experiments import Base
import chat_loops  # noqa: E402
import chat_tools as T  # noqa: E402
import halo_chat  # noqa: E402

ROUND = "Let me check the list.\n\nLet me read the first page again.\n\nLet me check the list and the notes.\n\nLet me read the result.\n\n"
ASIDE = "Right, all of it in one go then.\n\nLet me look at the list, the notes and the folder in one call.\n\nLet me read the result.\n\n"
LOOP = (ROUND * 8 + ASIDE) * 4  # the shape of a real one


def stream(text, seen="", asked="", step=5, steps=True):
    w = chat_loops.Watch(seen, asked, steps)
    for i in range(0, len(text), step):
        keep = w.add(text[i:i + step])
        if keep is not None:
            return len(w.text), keep
    return None


class Detect(unittest.TestCase):
    def test_their_loop_is_caught_in_its_first_rounds_and_one_round_is_kept(self):
        at, keep = stream(LOOP)
        self.assertLess(at, 900)
        self.assertEqual(LOOP[:keep].count("Let me read the first page again."), 1)
        self.assertIsNotNone(stream("I have the plan ready. Now some words first.\n" + LOOP))
        self.assertIsNotNone(stream("I should check the file again. " * 40))  # no new lines
        self.assertIsNotNone(stream("no " * 3500))  # any other text: only after a great many rounds
        self.assertIsNotNone(stream("The boat came in and the crew went home. " * 200))
        self.assertIsNone(stream("The boat came in and the crew went home. " * 30))
        self.assertIsNone(stream(LOOP, steps=False))  # a reply that could make no call announces nothing
        for step in (1, 37, 400, len(LOOP)):  # however it arrives
            self.assertEqual(LOOP[:stream(LOOP, step=step)[1]].count("Let me read the first page again."), 1)

    def test_text_that_may_repeat_is_left_alone(self):
        for text in ("\n".join(f"{i}. Check item {i} and write it down." for i in range(150)),
                     "| a | b |\n|---|---|\n" + "".join(f"| row {i} | {i * 7} |\n" for i in range(200)),
                     "=" * 3000, "+-----+-----+\n" * 200, "Here:\n```python\n" + "x = compute_value(a, b)\n" * 40 + "```\n",
                     ("We walk the line and carry on tonight\n" * 4 + "A new verse comes along\n") * 3):
            self.assertIsNone(stream(text), text[:40])
            self.assertEqual(chat_loops.collapse(text), text)
        for text in ("| A | B | C |\n|---|---|---|\n" + "| TBD | TBD | TBD |\n" * 60, "```html\n" + '<div class="cell"></div>\n' * 800 + "```\n",
                     "~~~\n" + "let me = 1\n" * 900 + "~~~\n", "Breathe in slowly for four counts.\nHold it.\nBreathe out for six counts.\nRest for two counts.\n\n" * 12,
                     "Name: ________  Signature: ________\n" * 40, "la la la, the river runs\nla la la, under the sun\n" * 20):
            for steps in (True, False):
                self.assertIsNone(stream(text, steps=steps), text[:40])
            self.assertEqual(chat_loops.collapse(text), text)
        line = "I will not talk in class again."
        self.assertIsNone(stream((line + "\n") * 100, asked=f'Write "{line}" 100 times.'))
        log = "ERROR retrying the connection to db in 5 s\n"
        self.assertIsNone(stream("The log says:\n" + log * 40, seen=log * 300))  # a tool returned it
        self.assertIsNotNone(stream(LOOP, asked="it says this again:\n" + LOOP))  # the user's own paste of a loop allows none

    def test_a_pledge_an_affirmation_and_a_chorus_come_whole_with_tools_on(self):
        for text in ("I will speak kindly to myself today.\n" * 20, "I will be patient. I will be kind. I will listen first.\n" * 15,
                     "We're going to dance until the morning light\nLet's go, let's go, we own the night\n" * 10,
                     "First, I ______.\nNext, I ______.\n" * 20, "| TBD | TBD | TBD |\n" * 500):
            self.assertIsNone(stream(text), text[:40])
            self.assertEqual(chat_loops.collapse(text), text)
        for text in ("Reading: ______ pages\nWriting: ______ lines\n" * 14, "I need to do ______ by ______.\n" * 18,
                     "Put your name and the date on it.\n" * 16, "Let me see your ticket, please.\n" * 16):
            self.assertIsNone(stream(text), text[:40])
        for ask in ("Say it repeated 12 times.", "Write the oath.", "A lullaby, please.", "I want this 10 times"):
            self.assertIsNone(stream("I'll check the door and I'll read the sign.\n" * 14, asked=ask), ask)
        self.assertIsNotNone(stream("I'll check the door and I'll read the sign.\n" * 14))
        line = "I'll find my way to you and I'll run all the way home tonight.\n" * 12
        self.assertIsNone(stream(line, asked="Write me a song with a chorus."))

    def test_announced_steps_that_show_their_command_and_long_rounds_are_caught(self):
        shown = "Let me check the log:\n```bash\ntail -n 50 app.log\n```\n\n" * 150
        at, keep = stream(shown)
        self.assertLess(at, 1200)
        self.assertEqual(shown[:keep].count("tail -n 50"), 1)
        self.assertLess(len(chat_loops.collapse(shown)), 300)
        words = " ".join(f"the keeper counted lamp {i} and wrote it in the book" for i in range(7)) + ". "
        self.assertGreater(len(words), 300)
        self.assertIsNotNone(stream(words * 60, steps=False))
        self.assertIsNone(stream(words * 8, steps=False))
        self.assertIsNone(stream("```bash\n" + "let me check the log\n" * 900))  # inside a code block

    def test_a_loop_right_after_a_code_block_is_caught(self):
        shown = "Let me check the log:\n```bash\ntail -n 50 app.log\n```\n\n"
        at, keep = stream("Here is what I ran:\n```bash\nls\n```\n\n" + shown * 150)
        self.assertLess(at, 1400)

    def test_collapse_is_quick_and_changes_nothing_it_should_not(self):
        import random
        import time
        for text in ("| TBD | TBD |\n" * 2000, "0,0\n" * 1500, "- item\n" * 800, "```\nx\n" * 1500, "a\n\nb\n" * 1500):
            t = time.perf_counter()
            self.assertEqual(chat_loops.collapse(text), text)
            self.assertLess(time.perf_counter() - t, 0.5)  # (0.01 s; it took 2 to 3 s)
        r = random.Random(4)
        for _ in range(300):
            text = "\n".join(r.choice(["- item", "", "```", "~~~", "x = 1", "| a |", "The boat came in.", "0,0"]) for _ in range(r.randint(40, 160)))
            self.assertEqual(chat_loops.collapse(text), text)

    def test_collapse_says_each_round_once(self):
        c = chat_loops.collapse("Found it.\n\n" + LOOP + "Done.")
        self.assertLess(len(c), len(LOOP) // 3)
        self.assertTrue(c.startswith("Found it.") and c.endswith("Done."))
        self.assertIn("(Said 7 more times.)", c)


class Stops(Base):
    def setUp(self):
        super().setUp()
        T.EXPERIMENTS.add("loop_stop")

    def run_turn(self, c, text="Check the server."):
        evs = list(self.agent.turn(c["id"], self.ep, "dsv41-unc", "unc-chat", text, None, effort="low"))
        page = ""
        for e in evs:  # what the page shows
            if e["type"] == "delta":
                page += e["text"]
            elif e["type"] == "retract":
                page = page[:-e["chars"]] if e["chars"] else page
        last = [m for m in self.st.messages(c["id"]) if m["role"] == "assistant"][-1]
        return evs, page, last

    def test_a_step_that_loops_is_sent_back_once_and_then_makes_its_call(self):
        c = self.chat("box")
        self.script += [{"text": LOOP}, {"calls": [("list_dir", {"path": "."})]}, {"text": "The folder is empty."}]
        evs, page, last = self.run_turn(c)
        self.assertNotIn("error", [e["type"] for e in evs], evs[-3:])
        asked = [b for b in self.bodies if b.get("stream")]
        self.assertEqual(asked[1]["messages"][-1]["content"], halo_chat.NUDGE["loop"])
        self.assertEqual(asked[1]["messages"][-2]["content"].count("Let me check the list.\n"), 1)  # one round of it
        self.assertEqual(page.count("Let me read the first page again."), 1)
        self.assertEqual(last["content"].count("Let me read the first page again."), 1)
        self.assertTrue(last["content"].endswith("The folder is empty."), last["content"][-80:])
        self.assertEqual(last["meta"]["loops"], 1)

    def test_one_that_loops_again_answers_without_tools(self):
        c = self.chat("box")
        self.script += [{"text": LOOP}, {"text": LOOP}, {"text": "I found nothing more."}]
        evs, page, last = self.run_turn(c)
        self.assertIn(halo_chat.NOTE_LOOP, [e.get("text") for e in evs if e["type"] == "note"])
        self.assertIn(halo_chat.FINAL_LOOP, json.dumps([b for b in self.bodies if b.get("stream")][2]["messages"][-1]))
        self.assertTrue(last["content"].endswith("I found nothing more."))
        self.assertLessEqual(last["content"].count("Let me read the first page again."), 2)

    def test_three_loops_still_end(self):
        c = self.chat("box")
        self.script += [{"text": LOOP}] * 6
        evs, page, last = self.run_turn(c)
        self.assertEqual(evs[-1]["type"], "done")
        self.assertLessEqual(len([b for b in self.bodies if b.get("stream")]), 4)
        self.assertLess(len(last["content"]), 800)

    def test_a_plain_reply_is_kept_to_its_first_round(self):
        c = self.chat()
        self.script += [{"text": "Here goes.\n\n" + "The boat came in and the crew went home.\n" * 400}]
        evs, page, last = self.run_turn(c)
        self.assertLess(last["content"].count("The boat came in"), 4)
        self.assertGreater(last["content"].count("The boat came in"), 0)
        self.assertIn(halo_chat.NOTE_LOOPED, last["content"])
        self.assertEqual(page.strip(), last["content"].strip())

    def test_off_nothing_is_stopped(self):
        T.EXPERIMENTS.discard("loop_stop")
        c = self.chat()
        self.script += [{"text": LOOP}]
        self.assertEqual(self.run_turn(c)[2]["content"], LOOP)

    def test_an_answer_meant_to_repeat_comes_whole_with_tools_on(self):
        T.EXPERIMENTS.add("loop_stop")
        c = self.chat("box")
        table = "| A | B | C |\n|---|---|---|\n" + "| TBD | TBD | TBD |\n" * 40
        self.script += [{"text": table}]
        self.assertEqual(self.run_turn(c, "Make a table with 40 empty rows.")[2]["content"], table)
        self.assertEqual(json.dumps(self.turn(c, "thanks")["messages"]).count("TBD | TBD"), 40)

    def test_a_loop_they_stopped_is_not_shown_to_the_model_again(self):
        c = self.chat()
        self.st.add_message(c["id"], "user", "go", {})
        self.st.add_message(c["id"], "assistant", LOOP, {"stopped": True})
        b = self.turn(c, "carry on")
        self.assertLess(json.dumps(b["messages"]).count("Let me read the first page again."), 6)
        self.assertEqual([m for m in self.st.messages(c["id"]) if m["role"] == "assistant"][0]["content"], LOOP)  # the user's keeps it


if __name__ == "__main__":
    unittest.main()
