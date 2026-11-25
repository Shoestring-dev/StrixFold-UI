"""Photos in Chat. The page now sends every attachment; the Mac gives the model what it can take: a picture when the
engine SERVING the turn reads pictures (Kyojin's Qwen 3.8, its own /health saying vision is on), else the text read from
the photo, said plainly. Synthetic pictures and a stub engine only; no box, no tunnel, no port 18080."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import chats  # noqa: E402
import halo_chat  # noqa: E402
import mediainfo  # noqa: E402
import studio  # noqa: E402

KYOJIN = "qwen38-kyojin-64k"


def engine(bodies, health):
    """A stub OpenAI endpoint that keeps each request body, with Kyojin's /health (health["body"], or health["code"])."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            health["asked"] = health.get("asked", 0) + 1
            out = json.dumps(health.get("body") or {}).encode()
            self.send_response(health.get("code", 200))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for d in ({"content": "I see "}, {"content": "it."}):
                self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": d}]}).encode() + b"\n\n")
            last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1500, "completion_tokens": 3}}
            self.wfile.write(b"data: " + json.dumps(last).encode() + b"\n\ndata: [DONE]\n\n")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@unittest.skipUnless(shutil.which("sips"), "macOS's sips is needed to shrink photos")
class Photos(unittest.TestCase):
    def setUp(self):
        halo_chat._VISION.clear()
        self.addCleanup(halo_chat._VISION.clear)
        self.st = studio.Store(os.path.join(tempfile.mkdtemp(prefix="hs-photo-"), "S"))
        self.agent = halo_chat.ChatAgent(self.st, chats.Relay())
        self.bodies, self.health = [], {"body": {"status": "ok", "source": "kyojin", "model": "Qwen3.8-Flash-Yamz", "vision": True}}
        self.srv = engine(self.bodies, self.health)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.ep = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def photo(self, w=320, h=180, text="Whiteboard: ship on Friday", name="board.png"):
        u = self.st.add_media(mediainfo.test_pattern(w, h, 3), name)
        if text:
            u = self.st.set_upload_text(u["id"], text, f"{w}×{h} · OCR · {len(text.split())} words")
        return u

    def send(self, chat, text, ups, profile):
        atts = [{"id": u["id"]} for u in ups]
        return list(self.agent.turn(chat["id"], self.ep, "served", profile, text, atts))

    @staticmethod
    def parts(body):
        return [p for m in body["messages"] if isinstance(m["content"], list) for p in m["content"] if p["type"] == "image_url"]

    def test_kyojin_with_vision_gets_the_photo_as_a_picture(self):
        c = self.st.create_chat(kind="chat", model=KYOJIN)
        evs = self.send(c, "what does the board say?", [self.photo()], KYOJIN)
        self.assertNotIn("error", [e["type"] for e in evs], evs)
        b = self.bodies[-1]
        last = b["messages"][-1]
        self.assertIsInstance(last["content"], list)
        self.assertEqual([p["type"] for p in last["content"]], ["text", "image_url"])
        self.assertIn("it comes with this message as a picture", last["content"][0]["text"])
        self.assertIn("Whiteboard: ship on Friday", last["content"][0]["text"])  # its recognised text rides along
        url = last["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(mediainfo.info(base64.b64decode(url.split(",", 1)[1])).get("kind"), "image")
        self.assertIn("You can see photos", b["messages"][0]["content"])
        self.assertNotIn("You read text only", b["messages"][0]["content"])
        asst = [m for m in self.st.messages(c["id"]) if m["role"] == "assistant"][-1]
        self.assertEqual((asst["content"], asst["meta"]["pictures"], asst["meta"].get("photo_text")), ("I see it.", 1, None))
        uid = self.st.messages(c["id"])[0]["meta"]["attachments"][0]["id"]
        self.assertEqual(os.stat(self.st.path("refs", f"{uid}.see.jpg")).st_mode & 0o777, 0o600)  # kept private, like the rest

    def test_a_photo_alone_is_a_message(self):
        c = self.st.create_chat(kind="chat", model=KYOJIN)
        evs = self.send(c, "", [self.photo(text=None)], KYOJIN)
        self.assertNotIn("error", [e["type"] for e in evs], evs)
        self.assertEqual(len(self.parts(self.bodies[-1])), 1)
        self.assertEqual(self.st.chat(c["id"])["title"], "board.png")

    def test_a_text_only_model_gets_the_text_and_is_told_it_cannot_see(self):
        """DeepSeek serving (and Gufo's Qwen): no picture, no /health probe; the photo's text, said plainly."""
        for profile in ("quick-chat", "qwen38-flash-next"):
            c = self.st.create_chat(kind="chat", model=profile)
            self.send(c, "what does the board say?", [self.photo()], profile)
            b = self.bodies[-1]
            self.assertEqual(self.parts(b), [], profile)
            self.assertIsInstance(b["messages"][-1]["content"], str)
            self.assertIn("This model reads text only; the text recognised in it on the user's Mac:]\n```\nWhiteboard: ship on Friday",
                          b["messages"][-1]["content"])
            self.assertIn("never the picture", b["messages"][0]["content"])
            self.assertIn("Qwen 3.8 in the model menu can look at photos", b["messages"][0]["content"])
            asst = [m for m in self.st.messages(c["id"]) if m["role"] == "assistant"][-1]
            self.assertEqual((asst["meta"].get("pictures"), asst["meta"]["photo_text"]), (None, 1))  # the page says "read the photo's text only"
        self.assertEqual(self.health.get("asked", 0), 0)

    def test_the_served_engine_decides_not_the_chats_choice(self):
        """A chat that chose Kyojin, answered while another engine serves or Kyojin's vision is
        off, gets no picture."""
        c = self.st.create_chat(kind="chat", model=KYOJIN)
        self.send(c, "one", [self.photo()], "quick-chat")  # the chat chose Kyojin; DeepSeek is what serves
        self.assertEqual(self.parts(self.bodies[-1]), [])
        for body, code in (({"status": "ok", "source": "kyojin", "vision": False}, 200), ({"source": "gufo", "vision": True}, 200),
                           ({"status": "loading"}, 503), ([], 200)):
            halo_chat._VISION.clear()
            self.health.update(body=body, code=code)
            self.send(c, "again", [self.photo()], KYOJIN)
            b = self.bodies[-1]
            self.assertEqual(self.parts(b), [], body)
            self.assertIn("This model reads text only", b["messages"][-1]["content"])

    def test_the_answer_about_vision_is_kept_for_a_while(self):
        self.assertTrue(halo_chat.sees_images(self.ep, "qwen38-kyojin", now=1000))
        self.health["body"] = {"source": "kyojin", "vision": False}
        self.assertTrue(halo_chat.sees_images(self.ep, "qwen38-kyojin", now=1000 + halo_chat.VISION_TTL_S - 1))
        self.assertFalse(halo_chat.sees_images(self.ep, "qwen38-kyojin", now=1000 + halo_chat.VISION_TTL_S + 1))
        self.assertEqual(self.health["asked"], 2)
        self.assertFalse(halo_chat.sees_images(self.ep, "dsv41", now=5000))  # never asks an engine that isn't a vision model
        self.assertFalse(halo_chat.sees_images("http://127.0.0.1:9/v1", "qwen38-kyojin"))  # no answer: no
        self.assertEqual(self.health["asked"], 2)

    def test_only_the_newest_photos_go_as_pictures(self):
        c = self.st.create_chat(kind="chat", model=KYOJIN)
        ups = [self.photo(name=f"p{i}.png", text=f"label {i}") for i in range(6)]
        self.send(c, "first three", ups[:3], KYOJIN)
        self.send(c, "next three", ups[3:], KYOJIN)
        b = self.bodies[-1]
        self.assertEqual(len(self.parts(b)), halo_chat.VISION_PICTURES)
        users = [m for m in b["messages"] if m["role"] == "user"]
        first = users[0]["content"]  # the older turn: one picture (p2), p0 and p1 as their text
        self.assertEqual(sum(1 for p in first if p["type"] == "image_url"), 1)
        self.assertIn("A photo was attached here (p0.png); it is not shown again as a picture, only the text recognised in it:]",
                      first[0]["text"])
        self.assertIn("label 1", first[0]["text"])
        self.assertEqual(sum(1 for p in users[1]["content"] if p["type"] == "image_url"), 3)
        self.assertTrue(all(p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in self.parts(b)))

    def test_a_big_photo_is_shrunk_and_a_small_one_kept(self):
        big, small = self.photo(3000, 2000, text=None), self.photo(320, 180, text=None)
        for u, most in ((big, halo_chat.VISION_PIXELS * 1.02), (small, 320 * 180)):
            uri = halo_chat.picture(self.st, u["id"])
            inf = mediainfo.info(base64.b64decode(uri.split(",", 1)[1]))
            self.assertLessEqual(inf["width"] * inf["height"], most, u)
        inf = mediainfo.info(base64.b64decode(halo_chat.picture(self.st, small["id"]).split(",", 1)[1]))
        self.assertEqual((inf["width"], inf["height"]), (320, 180))  # never enlarged
        txt = self.st.save_text("notes", "notes.txt", None)
        self.assertIsNone(halo_chat.picture(self.st, txt["id"]))
        self.assertIsNone(halo_chat.picture(self.st, "u-missing"))

    def test_deleting_the_chat_removes_the_copy_it_was_shown(self):
        c = self.st.create_chat(kind="chat", model=KYOJIN)
        u = self.st.add_media(mediainfo.test_pattern(320, 180, 3), "board.png", c["id"])
        self.assertTrue(halo_chat.picture(self.st, u["id"]))
        see = self.st.path("refs", f"{u['id']}.see.jpg")
        self.assertTrue(os.path.exists(see))
        self.st.delete_chat(c["id"])
        self.assertFalse(os.path.exists(see))

    def jpeg(self, w, h, orientation):
        """A JPEG as a phone stores it: w×h pixels plus an EXIF Orientation tag (made with sips, the tag put in after SOI)."""
        d = tempfile.mkdtemp(prefix="hs-exif-")
        self.addCleanup(shutil.rmtree, d, True)
        src, out = os.path.join(d, "p.png"), os.path.join(d, "p.jpg")
        with open(src, "wb") as f:
            f.write(mediainfo.test_pattern(w, h, 3))
        subprocess.run(["/usr/bin/sips", "-s", "format", "jpeg", src, "--out", out], capture_output=True, check=True)
        with open(out, "rb") as f:
            data = mediainfo.strip_metadata(f.read())
        return data[:2] + mediainfo._orientation_app1(orientation) + data[2:]

    def test_a_portrait_phone_photo_reaches_the_model_upright(self):
        # the store keeps the tag (the photo shows upright) but Kyojin reads pixels only, so a portrait
        # photo went sideways. Every orientation: 5-8 turn a quarter (taller than wide here); none keeps the tag.
        for o in range(1, 9):
            u = self.st.add_media(self.jpeg(320, 180, o), f"o{o}.jpg")
            with open(self.st.path("refs", f"{u['id']}.jpeg"), "rb") as f:
                self.assertEqual(mediainfo.jpeg_orientation(f.read()), o if o > 1 else None)  # the store keeps a turn
            data = base64.b64decode(halo_chat.picture(self.st, u["id"]).split(",", 1)[1])
            inf = mediainfo.info(data)
            self.assertEqual((inf["width"], inf["height"]), (180, 320) if o >= 5 else (320, 180), o)
            self.assertIn(mediainfo.jpeg_orientation(data), (None, 1), o)

    def test_a_photo_picked_before_its_chat_goes_with_the_chat(self):
        u = self.photo()  # the composer uploads before a new chat exists: no chat yet
        self.assertIsNone(self.st.upload(u["id"])["chat_id"])
        c = self.st.create_chat(kind="chat", model=KYOJIN)
        self.assertNotIn("error", [e["type"] for e in self.send(c, "what does it say?", [u], KYOJIN)])
        self.assertEqual(self.st.upload(u["id"])["chat_id"], c["id"])
        self.st.write_file(f"refs/{u['id']}.look.json", b"{}")  # its description by the Mac's vision model (look.py)
        files = [self.st.path("refs", f"{u['id']}.{x}") for x in ("png", "thumb.jpg", "txt", "see.jpg", "look.json")]
        self.assertTrue(all(os.path.exists(p) for p in files), files)
        self.st.delete_chat(c["id"])
        self.assertEqual([p for p in files if os.path.exists(p)], [])
        with self.assertRaises(KeyError):
            self.st.upload(u["id"])

    def test_a_waiting_message_claims_its_photo_and_another_chats_stays_put(self):
        a, b = self.st.create_chat(kind="chat"), self.st.create_chat(kind="chat")
        mine, theirs = self.photo(text=None), self.st.add_media(mediainfo.test_pattern(64, 64, 1), "a.png", a["id"])
        self.st.enqueue(b["id"], "later", {"attachments": [{"id": mine["id"]}, {"id": theirs["id"]}, {"id": "u-bogus"}, "x"]})
        self.assertEqual(self.st.upload(mine["id"])["chat_id"], b["id"])
        self.assertEqual(self.st.upload(theirs["id"])["chat_id"], a["id"])  # never moved from the chat that holds it

    def test_linking_old_uploads_only_links(self):
        a, b, c = (self.st.create_chat(kind="chat") for _ in range(3))
        both, waiting, job, nobody = (self.photo(text=None) for _ in range(4))
        held = self.st.add_media(mediainfo.test_pattern(64, 64, 1), "held.png", c["id"])
        raw = lambda cid, ids: self.st.x("INSERT INTO messages (chat_id, role, content, created, meta) VALUES (?,?,?,?,?)",  # noqa: E731
                                         (cid, "user", "", 0, json.dumps({"attachments": [{"id": i} for i in ids]})))
        raw(b["id"], [both["id"], held["id"]])  # as before claim_uploads: rows written without claiming
        raw(a["id"], [both["id"]])  # a later message in another chat: the earliest one wins
        self.st.x("INSERT INTO outbox (chat_id, content, meta, created) VALUES (?,?,?,?)",
                  (a["id"], "", json.dumps({"attachments": [{"id": waiting["id"]}]}), 0))
        n, theirs = 2, None
        self.assertEqual(self.st.link_orphans(), n)
        got = {k: self.st.upload(u["id"])["chat_id"] for k, u in (("both", both), ("waiting", waiting), ("job", job),
                                                                    ("nobody", nobody), ("held", held))}
        self.assertEqual(got, {"both": b["id"], "waiting": a["id"], "job": theirs, "nobody": None, "held": c["id"]})
        self.assertTrue(os.path.exists(self.st.path("refs", f"{nobody['id']}.png")))  # nothing listed it: left as it is
        self.assertEqual(self.st.link_orphans(), 0)

    def test_pictures_count_in_the_window(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "x" * 30}, {"type": "image_url", "image_url": {"url": "data:"}}]}]
        self.assertGreaterEqual(halo_chat.est_tokens(msgs), halo_chat.VISION_TOKENS)
        self.assertEqual(halo_chat.content_chars("abc"), 3)

    def test_the_model_sheet_says_which_model_sees_photos(self):
        rows = {m["name"]: m for m in halo_chat.models({"state": "idle"})["models"]}
        self.assertTrue(rows["Qwen 3.8"]["vision"])  # Halogen reads photos (Kyojin's row did before)
        self.assertNotIn("Qwen 3.8 (Kyojin)", rows)
        self.assertFalse(any(rows[n].get("vision") for n in ("DeepSeek V4.1", "DeepSeek uncensored")))


class Page(unittest.TestCase):
    """halo-chat.js's attsend block in node: every uploaded attachment goes; the composer's words for each model."""

    def test_what_goes_and_what_the_composer_says(self):
        node = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)
        if not node:
            self.skipTest("node is not installed")
        js = open(os.path.join(ROOT, "mac", "ui", "halo-chat.js")).read()
        block = js[js.index("/* attsend: pure"):js.index("/* attsend: end */")]
        script = block + """
const ky = { name: "Qwen 3.8 (Kyojin)", vision: true }, ds = { name: "DeepSeek V4.1" };
const photo = { id: "u1", kind: "image" }, memo = { id: "u2", kind: "audio", detail: "audio · 0:03 · 6 words" };
console.log(JSON.stringify([
  sendable([{ id: "u0", kind: "text" }, photo, memo, { kind: "image", uploading: true }, { id: "u9", uploading: true }]),
  attNote(ky, [photo]), attNote(ds, [photo, photo]), attNote(ds, [memo]), attNote(ky, [photo, { id: "u3", kind: "video", detail: "video" }]),
  attNote(ds, [{ id: "u4", kind: "image", detail: "900×600 · no text found" }]),
  photoWords({ pictures: 1 }), photoWords({ pictures: 3 }), photoWords({ photo_text: 1 }), photoWords({ photo_text: 2 }), photoWords({})]));
"""
        r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [
            [{"id": "u0"}, {"id": "u1"}, {"id": "u2"}],
            "Qwen 3.8 (Kyojin) looks at the photo itself.",
            "DeepSeek V4.1 can't see photos: it gets the text read from them. Qwen 3.8 (Kyojin) can look at photos.",
            "DeepSeek V4.1 gets what was said in the recording.",
            "Qwen 3.8 (Kyojin) looks at the photo itself. It gets only the details of the recording: no speech was read.",
            "DeepSeek V4.1 can't see photos, and no text was found in this one. Qwen 3.8 (Kyojin) can look at photos.",
            "saw 1 photo", "saw 3 photos", "read the photo's text only", "read the photos' text only", None])
        # the old filter (text files only) and the photo-only refusal are gone
        self.assertNotIn('C.atts.filter((a) => a.kind === "text")', js)
        self.assertNotIn("These models read text only", js)


if __name__ == "__main__":
    unittest.main()
