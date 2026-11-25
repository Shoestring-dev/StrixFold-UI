"""The Mac's photo look in Chat (W6 on W1's photos and W2's tool loop): chat_look, the chat_tools plug-in that offers
look_at_image to a model that can't see photos (at every level, "No tools" included), waits for a message's photos
to be described (before), and adds its sentence to the system prompt (system_note); turn_text's description block; the
description counted in too_long; the upload route that starts a look and the chip's poll; the page's words. A fake look
worker (box/fake/fake_look_worker.py) and stub engines only: no model, no box, no port 18080."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chat_look  # noqa: E402
import chat_tools as T  # noqa: E402
import chats  # noqa: E402
import halo_chat  # noqa: E402
import look  # noqa: E402
import mediainfo  # noqa: E402
import studio  # noqa: E402
from test_look import FAKE, lines  # noqa: E402

SAY = "A whiteboard. It says: ship on Friday."


class Base(unittest.TestCase):
    def setUp(self):
        from test_chat_tools import engine
        halo_chat._VISION.clear()
        self.addCleanup(halo_chat._VISION.clear)
        self.tmp = tempfile.mkdtemp(prefix="hc-chat-look-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.st = studio.Store(os.path.join(self.tmp, "S"))
        self.agent = halo_chat.ChatAgent(self.st, chats.Relay())
        self.script, self.bodies = [], []
        self.srv = engine(self.script, self.bodies)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.ep = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        self.log = os.path.join(self.tmp, "worker.log")

    def looker(self, *args):
        lk = look.Looker(self.st, worker_cmd=[sys.executable, FAKE, "--log", self.log, "--say", SAY, *args])
        self.addCleanup(lk.close)
        self.agent.looker = lk
        self.agent.app = types.SimpleNamespace(fake=True, studio={"looker": lk})
        return lk

    def photo(self, seed=3, name="board.png", text="ship on Friday"):
        u = self.st.add_media(mediainfo.test_pattern(320, 180, seed), name)
        return self.st.set_upload_text(u["id"], text, f"320×180 · OCR · {len(text.split())} words") if text else u

    def turn(self, ups, text="what does the board say?", chat=None, profile="quick-chat", ep=None):
        chat = chat or self.st.create_chat(kind="chat", model=profile)
        evs = list(self.agent.turn(chat["id"], ep or self.ep, "served", profile, text, [{"id": u["id"]} for u in ups]))
        return self.st.chat(chat["id"]), evs

    def answer(self, chat):
        return [m for m in self.st.messages(chat["id"]) if m["role"] == "assistant"][-1]

    def ctx(self, chat, sees=False, private=False, app=None):
        return T.TurnCtx(chat["id"], self.st.chat(chat["id"]), self.st, "dsv41", sees=sees, private=private,
                         app=app if app is not None else self.agent.app)


class Turn(Base):
    def test_uncensored_inspects_photo_for_users_question_before_answering(self):
        lk = self.looker()
        u = self.photo()
        lk.describe(u["id"])
        self.script.append({"text": "Friday."})
        chat, evs = self.turn([u], text="What day is written on the board?", profile="unc-chat")
        self.assertNotIn("error", [e["type"] for e in evs])
        system = self.bodies[0]["messages"][0]["content"]
        user = json.dumps(self.bodies[0]["messages"][-1]["content"])  # the findings: after the user's message (the prompt's start stays)
        self.assertIn("Fake look answer to: What day is written on the board?", user)
        self.assertIn(f"vision%20assistant%20photo%20{u['id']}", user)
        self.assertNotIn("Fake look answer", system)
        self.assertEqual([x["kind"] for x in lines(self.log)], ["describe", "ask"])
        self.assertEqual(chat["model"], "unc-chat")
        self.assertTrue(any("Vision assistant inspected" in p.get("note", "") for p in self.answer(chat)["meta"]["parts"]))

    def test_uncensored_photo_inspection_failure_still_answers(self):
        lk = self.looker()
        u = self.photo()
        lk.describe(u["id"])
        def fail(*args):
            raise RuntimeError("worker unavailable")
        lk.ask = fail
        self.script.append({"text": "The description says Friday."})
        _, evs = self.turn([u], profile="unc-chat")
        self.assertNotIn("error", [e["type"] for e in evs])
        self.assertTrue(any("couldn't inspect" in e.get("text", "") for e in evs))
        self.assertIn(SAY, self.bodies[0]["messages"][-1]["content"])

    def test_no_tools_level_offers_look_at_image_and_the_answer_comes_back(self):
        """J5: a chat at "No tools" with a photo, on DeepSeek: the model reads the description (labelled, with its
        image_id), is offered look_at_image alone, calls it, and gets the Mac's answer back as untrusted data."""
        lk = self.looker()
        u = self.photo()
        lk.describe(u["id"])
        self.script += [{"text": "Let me look closer.", "calls": [("look_at_image", {"image_id": u["id"], "question": "What is the date?"})]},
                        {"text": "It says Friday."}]
        chat, evs = self.turn([u])
        kinds = [e["type"] for e in evs]
        self.assertNotIn("error", kinds, evs)
        first, second = self.bodies
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["look_at_image"])
        system, msg = first["messages"][0]["content"], first["messages"][-1]["content"]
        self.assertIn("a small vision model on their Mac describes it for you", system)
        self.assertIn("call look_at_image", system)
        self.assertIn("a description of it made on the user's Mac follows", msg)
        self.assertIn(f"image_id {u['id']}", msg)
        self.assertIn(SAY, msg)
        self.assertIn("[The text recognised in it on the user's Mac:]", msg)  # the OCR still goes, after it
        done = next(e for e in evs if e["type"] == "tool_done")
        self.assertEqual((done["status"], done["line"]), ("ok", "Looked at board.png: What is the date?"))
        self.assertEqual(done["out"], "Fake look answer to: What is the date?")
        res = second["messages"][-1]
        self.assertEqual(res["role"], "tool")
        self.assertTrue(res["content"].startswith(f'<untrusted source="photo%20{u["id"]}%20(look_at_image)">'), res["content"])
        self.assertEqual([(c["kind"], c["side"]) for c in lines(self.log)], [("describe", look.DESCRIBE_SIDE), ("ask", look.ASK_SIDE)])
        a = self.answer(chat)
        self.assertEqual(a["content"], "Let me look closer.\n\nIt says Friday.")
        self.assertEqual([p["tool"]["name"] for p in a["meta"]["parts"] if "tool" in p], ["look_at_image"])

    def test_a_photo_still_being_described_is_waited_for_and_reaches_the_model(self):
        lk = self.looker("--delay", "0.8")
        u = self.photo()
        self.assertTrue(lk.start(u["id"]))  # as the upload does
        self.script.append({"text": "A whiteboard."})
        chat, evs = self.turn([u])
        looking = [e for e in evs if e["type"] == "looking"]
        self.assertEqual(looking, [{"type": "looking", "photos": 1}])
        self.assertIn(SAY, self.bodies[0]["messages"][-1]["content"])
        self.assertEqual(len(lines(self.log)), 1)  # described once
        self.assertEqual((self.answer(chat)["meta"].get("described"), self.answer(chat)["meta"].get("photo_text")), (1, 0))

    def test_without_a_description_w1s_words_stay(self):
        self.looker("--fail")
        u = self.photo()
        self.script.append({"text": "I can read: ship on Friday."})
        chat, evs = self.turn([u])
        msg = self.bodies[0]["messages"][-1]["content"]
        self.assertIn("This model reads text only; the text recognised in it on the user's Mac:", msg)  # W1's fallback
        self.assertNotIn("image_id", msg)

    def test_a_model_that_sees_the_photo_gets_no_look(self):
        """Kyojin with its vision on (W1's sees_images): no wait, no tool, the Mac's model never asked."""
        from test_chat_photos import engine
        bodies, health = [], {"body": {"status": "ok", "source": "kyojin", "vision": True}}
        srv = engine(bodies, health)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.looker("--delay", "3")
        u = self.photo()
        _, evs = self.turn([u], profile="qwen38-kyojin-64k", ep=f"http://127.0.0.1:{srv.server_address[1]}/v1")
        self.assertNotIn("looking", [e["type"] for e in evs])
        self.assertNotIn("tools", bodies[-1])
        self.assertEqual(halo_chat.pictures_in(bodies[-1]["messages"]), 1)
        self.assertEqual(lines(self.log), [])

    def test_too_long_counts_the_description(self):
        lk = self.looker()
        u = self.photo()
        a = {"id": u["id"], "kind": "image", "chars": u["chars"]}
        self.assertEqual(halo_chat.attachment_chars(self.st, a, None), u["chars"])  # no look on this Mac: the OCR alone
        self.assertEqual(halo_chat.attachment_chars(self.st, a, lk), u["chars"] + look.BLOCK_CHARS)  # being made: the most
        lk.describe(u["id"])
        self.assertEqual(halo_chat.attachment_chars(self.st, a, lk), u["chars"] + len(SAY) + 300)
        self.assertEqual(halo_chat.attachment_chars(self.st, {"id": "u0", "kind": "text", "chars": 12}, lk), 12)


class LoopFixes(Base):
    """A toolset is closed exactly once however the turn ends (before() now runs outside the tool
    loop's try), and counting described photos never breaks a turn."""

    def plug(self, raising):
        closes, st = [], self

        class Counting(T.Toolset):
            def specs(self):
                return [{"type": "function", "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}}}]

            def before(self, ctx):
                if raising:
                    raise RuntimeError("before() broke")
                yield {"type": "looking", "photos": 1}

            def close(self):
                closes.append(1)
        mod = types.ModuleType("chat_look")
        mod.toolset = lambda ctx: Counting()
        old = sys.modules.get("chat_look")
        self.addCleanup(lambda: sys.modules.__setitem__("chat_look", old) if old else sys.modules.pop("chat_look", None))
        sys.modules["chat_look"] = mod
        return closes

    def test_a_toolset_is_closed_once_however_the_turn_ends(self):
        closes = self.plug(raising=True)
        chat = self.st.create_chat(kind="chat")
        with self.assertRaisesRegex(RuntimeError, "before\\(\\) broke"):
            list(self.agent.turn(chat["id"], self.ep, "served", "quick-chat", "hi", None))
        self.assertEqual(closes, [1])  # closed although the loop never started
        self.assertFalse(self.agent.relay.busy(chat["id"]))  # and the chat is free again
        closes = self.plug(raising=False)
        self.script.append({"text": "Done."})
        evs = list(self.agent.turn(chat["id"], self.ep, "served", "quick-chat", "again", None))
        self.assertEqual([e["type"] for e in evs][-1], "done")
        self.assertEqual(closes, [1])  # once, not by the loop and again by turn()

    def test_counting_described_photos_never_breaks_a_turn(self):
        u, v = self.photo(seed=1), self.photo(seed=2)
        atts = [{"id": u["id"], "kind": "image"}, {"id": v["id"], "kind": "image"}, {"id": "u0", "kind": "text"}]

        def flaky(store, uid):
            if uid == u["id"]:
                raise ValueError("an unreadable description")
            return "a description"
        real = look.look_text
        self.addCleanup(setattr, look, "look_text", real)
        look.look_text = flaky
        self.assertEqual(halo_chat.photos_described(self.st, atts), 1)  # the unreadable one counts as none
        look.look_text = real

        class Broken:  # an install whose import fails with something other than ImportError
            def find_spec(self, name, path=None, target=None):
                if name == "look":
                    raise RuntimeError("a broken install")
        saved = sys.modules.pop("look")
        self.addCleanup(sys.modules.__setitem__, "look", saved)
        finder = Broken()
        sys.meta_path.insert(0, finder)
        self.addCleanup(sys.meta_path.remove, finder)
        self.assertEqual(halo_chat.photos_described(self.st, atts), 0)


class Toolset(Base):
    def test_when_it_applies(self):
        lk = self.looker()
        u = self.photo()
        with_photo = self.st.create_chat(kind="chat")
        self.st.add_message(with_photo["id"], "user", "look", {"attachments": [{"id": u["id"], "kind": "image", "name": "board.png"}]})
        plain = self.st.create_chat(kind="chat")
        self.st.add_message(plain["id"], "user", "hello", {})
        t = chat_look.toolset(self.ctx(with_photo))
        self.assertIsInstance(t, chat_look.Look)
        self.assertEqual(t.level, "none")  # every level, "No tools" included
        self.assertEqual([s["function"]["name"] for s in t.specs()], ["look_at_image"])
        self.assertIn("look_at_image", t.system_note(self.ctx(with_photo)))
        self.assertIsNone(chat_look.toolset(self.ctx(plain)))  # no photo in the chat
        self.assertIsNone(chat_look.toolset(self.ctx(with_photo, sees=True)))  # the serving engine sees it itself
        self.assertIsNone(chat_look.toolset(self.ctx(with_photo, private=True)))  # Private: the look is off
        self.assertTrue(chat_look.private_chat({"private": 1}))  # the chat row's flag, once W7 adds it
        self.assertIsNone(chat_look.toolset(self.ctx(with_photo, app=types.SimpleNamespace(studio={}))))  # no look on this Mac
        lk.available = lambda: False
        self.assertIsNone(chat_look.toolset(self.ctx(with_photo)))  # set up but its model is missing
        own = types.SimpleNamespace(available=lambda: True)  # the turn's own looker (ctx.looker) wins over Studio's
        c = self.ctx(with_photo)
        c.looker = own
        self.assertIs(chat_look.looker_of(c), own)

    def test_its_calls_see_only_this_chats_photos_at_most_three_times(self):
        self.looker()
        u, other, gone = self.photo(), self.photo(seed=5), self.photo(seed=7)
        c = self.st.create_chat(kind="chat")
        self.st.add_message(c["id"], "user", "a", {"attachments": [{"id": u["id"], "kind": "image", "name": "board.png"}]})
        mid = self.st.add_message(c["id"], "user", "b", {"attachments": [{"id": gone["id"], "kind": "image", "name": "x.png"}]})
        self.st.x("UPDATE messages SET hidden=1 WHERE id=?", (mid,))
        ctx = self.ctx(c)
        t = chat_look.toolset(ctx)
        for bad in (other["id"], gone["id"], "https://example.com/x.png", "/etc/hosts", "", None):
            r = t.call("look_at_image", {"image_id": bad, "question": "what does it say?"}, ctx)
            self.assertEqual((r["status"], r["decision"], r["line"]), ("refused", "refused", "Look refused: not a photo in this chat"), bad)
            self.assertIn(u["id"], r["text"])  # it names the photos it can look at
        self.assertEqual(t.call("look_at_image", {"image_id": u["id"], "question": "  "}, ctx)["line"], "Look refused: no question")
        self.assertEqual(t.call("other_tool", {}, ctx)["status"], "refused")
        self.assertEqual(lines(self.log), [])  # none of those reached the model
        long_q = "What does the small sign say?" + " x" * 400
        oks = [t.call("look_at_image", {"image_id": u["id"], "question": long_q}, ctx) for _ in range(3)]
        self.assertEqual([r["status"] for r in oks], ["ok"] * 3)
        self.assertEqual(oks[0]["line"], "Looked at board.png: What does the small sign say?" + " x" * 25)
        self.assertLess(len(oks[0]["out"]), 600)  # the question is capped (MAX_QUESTION)
        r = t.call("look_at_image", {"image_id": u["id"], "question": "and again?"}, ctx)
        self.assertEqual((r["status"], r["line"]), ("refused", "Look refused: 3 looks an answer"))
        self.assertEqual(len(lines(self.log)), 3)

    def test_before_never_raises(self):
        """g-perm: before() runs before the answer exists and outside the loop's try; a failure is a note."""
        lk = self.looker()
        u = self.photo()
        c = self.st.create_chat(kind="chat")
        self.st.add_message(c["id"], "user", "a", {"attachments": [{"id": u["id"], "kind": "image", "name": "board.png"}]})
        ctx = self.ctx(c)
        lk.wait = lambda *a, **k: (_ for _ in ()).throw(OSError("the store went away"))
        self.assertEqual(list(chat_look.toolset(ctx).before(ctx)),
                         [{"type": "looking", "photos": 1},
                          {"type": "note", "text": "The photo couldn't be looked at on your Mac; the model gets the text read from it."}])
        ctx.stop.set()  # stopped already: no wait at all
        self.assertEqual(list(chat_look.toolset(ctx).before(ctx)), [])

    def test_a_failed_look_is_a_failed_step(self):
        self.looker("--fail")
        u = self.photo()
        c = self.st.create_chat(kind="chat")
        self.st.add_message(c["id"], "user", "a", {"attachments": [{"id": u["id"], "kind": "image", "name": "board.png"}]})
        ctx = self.ctx(c)
        r = chat_look.toolset(ctx).call("look_at_image", {"image_id": u["id"], "question": "what?"}, ctx)
        self.assertEqual((r["status"], r["line"]), ("failed", "Look failed: the photo model could not read this image"))

    def test_before_waits_for_this_messages_photos_and_stop_ends_it(self):
        lk = self.looker("--delay", "5")
        old, new = self.photo(seed=1), self.photo(seed=2)
        c = self.st.create_chat(kind="chat")
        self.st.add_message(c["id"], "user", "a", {"attachments": [{"id": old["id"], "kind": "image", "name": "o.png"}]})
        self.st.add_message(c["id"], "user", "b", {"attachments": [{"id": new["id"], "kind": "image", "name": "n.png"}]})
        ctx = self.ctx(c)
        threading.Timer(0.3, ctx.stop.set).start()
        t0 = time.time()
        self.assertEqual(list(chat_look.toolset(ctx).before(ctx)), [{"type": "looking", "photos": 1}])  # the newest message's
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(lk.state(new["id"]), "looking")  # still being made: it goes on for the next turn
        self.assertIsNone(lk.state(old["id"]))


class Routes(unittest.TestCase):
    """The upload starts a look for a text-only model (never for one that sees photos, nor in a Private chat), and the
    chip polls it."""

    def setUp(self):
        import test_studio
        test_studio.Http.setUp(self)
        self.log = os.path.join(tempfile.mkdtemp(prefix="hc-look-http-"), "worker.log")
        self.lk = look.Looker(self.app.studio["store"], worker_cmd=[sys.executable, FAKE, "--log", self.log, "--delay", "0.4"])
        self.addCleanup(self.lk.close)
        self.app.studio["looker"] = self.lk

    def req(self, *a, **k):
        import test_studio
        return test_studio.Http.req(self, *a, **k)

    def unlock(self):
        import test_studio
        return test_studio.Http.unlock(self)

    def up(self, model, data=None, name="p.png", ctype="image/png", chat=""):
        code, body, _ = self.req("POST", "/studio/api/uploads", raw=data or mediainfo.test_pattern(64, 48, 3), Content_Type=ctype,
                                 X_Upload_For="chat", X_Filename=name, X_Model=model, X_Chat=chat)
        self.assertEqual(code, 200, body)
        return json.loads(body)

    def test_the_upload_starts_a_look_and_the_chip_follows_it(self):
        self.assertEqual(self.req("GET", "/studio/api/uploads/u0/look")[0], 401)  # behind the session
        self.unlock()
        u = self.up("quick-chat")
        self.assertEqual(u["look"], "looking")
        for _ in range(50):
            code, body, _ = self.req("GET", f"/studio/api/uploads/{u['id']}/look")
            self.assertEqual(code, 200)
            if json.loads(body)["look"] == "seen":
                break
            time.sleep(0.1)
        self.assertEqual(json.loads(body), {"id": u["id"], "look": "seen"})
        # a chat on Qwen 3.8 (Kyojin) looks at the photo itself: no look at upload
        self.assertIsNone(self.up("qwen38-kyojin-64k", mediainfo.test_pattern(64, 48, 9))["look"])
        # a Private chat: the look is off; its photo goes up sealed, with the page's private token
        import private_reader
        old = private_reader.READER
        private_reader.READER = private_reader.Reader(open(os.path.join(ROOT, "mac", "halo_extract.swift"), "rb").read(), True)
        self.addCleanup(setattr, private_reader, "READER", old)
        self.addCleanup(private_reader.READER.close)
        self.app.code_check = types.SimpleNamespace(view=lambda: {"clean": True, "problems": [], "line": "Running code: test, clean"})
        tok = json.loads(self.req("POST", "/studio/api/private/unlock", {"passcode": "correct horse"})[1])["token"]
        c = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])
        code, body, _ = self.req("POST", "/studio/api/uploads", raw=mediainfo.test_pattern(64, 48, 10), Content_Type="image/png",
                                 X_Upload_For="chat", X_Filename="p.png", X_Model="quick-chat", X_Chat=c["id"], X_Private="1",
                                 X_Halo_Private=tok)
        self.assertEqual(code, 200, body)
        self.assertIsNone(json.loads(body)["look"])
        # a text file has none; an unknown upload is not found
        self.assertIsNone(self.up("quick-chat", b"notes\n", "n.txt", "text/plain")["look"])
        self.assertEqual(self.req("GET", "/studio/api/uploads/u0000/look")[0], 404)
        self.assertEqual(len(lines(self.log)), 1)
        # no look set up on this Mac: the upload still works, and says nothing about a look
        self.app.studio["looker"] = None
        self.assertIsNone(self.up("quick-chat", mediainfo.test_pattern(64, 48, 11))["look"])


class Page(unittest.TestCase):
    """halo-chat.js's attsend block in node: the composer's, the chip's and the answer's words with the look."""

    def test_the_words(self):
        node = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)
        if not node:
            self.skipTest("node is not installed")
        js = open(os.path.join(ROOT, "mac", "ui", "halo-chat.js")).read()
        block = js[js.index("/* attsend: pure"):js.index("/* attsend: end */")]
        script = block + """
const ky = { name: "Qwen 3.8 (Kyojin)", vision: true }, ds = { name: "DeepSeek V4.1" };
const seen = { id: "u1", kind: "image", look: "seen" }, looking = { id: "u2", kind: "image", look: "looking" }, plain = { id: "u3", kind: "image" };
console.log(JSON.stringify([
  attNote(ds, [seen]), attNote(ds, [seen, looking]), attNote(ky, [seen]), attNote(ds, [seen, plain]),
  photoWords({ described: 1 }), photoWords({ described: 2 }), photoWords({ described: 1, photo_text: 1 }), photoWords({ pictures: 1, described: 1 }),
  lookWords(looking), lookWords(seen), lookWords(plain), lookWords({ look: "failed" })]));
"""
        r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [
            "DeepSeek V4.1 reads a description of this photo made on your Mac.",
            "DeepSeek V4.1 reads a description of these photos made on your Mac.",
            "Qwen 3.8 (Kyojin) looks at the photo itself.",
            "DeepSeek V4.1 can't see photos: it gets the text read from them. Qwen 3.8 (Kyojin) can look at photos.",
            "read a description of the photo", "read descriptions of 2 photos", "read a description of the photo; one photo's text only",
            "saw 1 photo", "Looking…", "Seen on your Mac", None, None])


if __name__ == "__main__":
    unittest.main()
