"""Documents in Chat: halo-extract's doc mode through docs.py (fields and their labels, fill, write, combine, page
pictures, page text by OCR, a PDF from Markdown), md_html's escaping, the store's kept originals (sealed in a Private
chat), the chat_docs plug-in (offered, refused, each tool) and a whole turn on a stub engine, the open/download route,
and the container's copies and shown documents. Test forms made by the helper itself (make-form): no network, no model,
no box."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import base64
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chat_box  # noqa: E402
import chat_docs  # noqa: E402
import chat_tools as T  # noqa: E402
import chats  # noqa: E402
import docs  # noqa: E402
import filetext  # noqa: E402
import halo_chat  # noqa: E402
import private_store as P  # noqa: E402
import studio  # noqa: E402

PASS = "correct horse battery staple"
EXE = None
FORM = SCAN = None


def setUpModule():
    """The helper built once (as the app does), a fillable form and a scanned page made by it, and Private's reader."""
    global EXE, FORM, SCAN, _READER
    EXE = filetext.helper()
    if not EXE:
        raise unittest.SkipTest("no swiftc on this Mac")
    d = tempfile.mkdtemp(prefix="hc-docs-forms-")
    try:
        subprocess.run([EXE, "make-form", os.path.join(d, "form.pdf")], check=True, capture_output=True, timeout=120)
        subprocess.run([EXE, "make-pdf", os.path.join(d, "scan.pdf"), "INVOICE 4471", "scan"], check=True, capture_output=True, timeout=120)
        FORM, SCAN = open(os.path.join(d, "form.pdf"), "rb").read(), open(os.path.join(d, "scan.pdf"), "rb").read()
    finally:
        shutil.rmtree(d, True)
    import private_reader as R
    _READER = R.READER
    R.READER = R.Reader(open(os.path.join(ROOT, "mac", "halo_extract.swift"), "rb").read(), True)


def tearDownModule():
    import private_reader as R
    if EXE:
        R.READER.close()
        R.READER = _READER


def plain(pdf):
    """A PDF's text, as the chat reads it."""
    return docs.text(pdf, list(range(1, int(docs.info(pdf)["pages"]) + 1)))["text"]


def values(pdf):
    return {f["name"]: f["value"] for f in docs.fields(pdf)["fields"]}


class Helper(unittest.TestCase):
    def test_a_forms_fields_with_the_words_printed_next_to_them(self):
        i = docs.info(FORM)
        self.assertEqual((i["pages"], i["fields"], i["locked"], i["placeholder"]), (2, 5, False, False))
        self.assertEqual(i["sizes"], [[612, 792], [792, 612]])  # page 2 is turned a quarter: shown landscape
        fs = docs.fields(FORM)["fields"]
        self.assertEqual([(f["n"], f["name"], f["type"], f["label"]) for f in fs], [
            (1, "full_name", "text", "Full name:"), (2, "email", "text", "Email address:"),
            (3, "agree", "checkbox", "I agree to the terms"), (4, "plan", "radio", "Plan:"), (5, "country", "choice", "Country:")])
        self.assertEqual(fs[1]["maxlen"], 40)
        self.assertEqual(fs[3]["options"], [{"value": "Basic", "label": "Basic"}, {"value": "Pro", "label": "Pro"}])
        self.assertEqual(fs[4]["options"], ["Canada", "Mexico", "United States"])
        self.assertEqual(fs[0]["rect"], [140, 76, 300, 20])  # from the page's top left, in points

    def test_fill_by_number_or_name_and_read_back(self):
        got = docs.fill(FORM, {"1": "Alex Example", "email": "x" * 50, "3": "yes", "4": "pro", "5": "mexico", "9": "nope",
                               "country2": "Peru"})
        res = {r["key"]: r for r in got["results"]}
        self.assertTrue(all(res[k]["ok"] for k in ("1", "email", "3", "4", "5")), res)
        self.assertEqual(res["email"]["why"], "cut to its 40 characters")
        self.assertEqual((res["9"]["ok"], res["9"]["why"]), (False, "no such field"))
        self.assertFalse(res["country2"]["ok"])
        self.assertEqual(values(got["pdf"]), {"full_name": "Alex Example", "email": "x" * 40, "agree": True, "plan": "Pro",
                                              "country": "Mexico"})
        self.assertEqual(values(FORM)["full_name"], "")  # the original is untouched
        bad = docs.fill(FORM, {"5": "Peru", "3": "maybe"})["results"]
        self.assertEqual([r["ok"] for r in bad], [False, False])
        self.assertIn("not one of its options: Canada, Mexico, United States", bad[1]["why"])
        self.assertEqual(bad[0]["why"], "a checkbox takes true or false")

    def test_flatten_makes_it_final(self):
        got = docs.fill(FORM, {"1": "Flat Name"}, flatten=True)
        self.assertEqual(docs.info(got["pdf"])["fields"], 0)
        self.assertIn("Flat Name", plain(got["pdf"])[0])

    def test_write_after_a_label_at_a_point_and_refusals(self):
        got = docs.write(FORM, [{"page": 1, "text": "555-0100", "after": "Phone:"}, {"page": 1, "text": "J. E.", "x": 72, "y": 400},
                                {"page": 1, "text": "x", "after": "Fax:"}, {"page": 1, "text": "x", "x": 640, "y": 10},
                                {"page": 3, "text": "x", "x": 1, "y": 1}], flatten=True)
        p = got["placed"]
        self.assertEqual((p[0]["page"], p[0]["x"] > 100, 270 < p[0]["y"] < 290), (1, True, True))  # beside "Phone:"
        self.assertEqual((p[1]["x"], p[1]["y"]), (70, 400))
        self.assertEqual(p[2], {"error": "“Fax:” isn't on page 1"})
        self.assertIn("run off page 1", p[3]["error"])
        self.assertEqual(p[4], {"error": "there is no page 3"})
        self.assertIn("555-0100", plain(got["pdf"])[0])

    def test_combine_turn_and_page_numbers(self):
        got = docs.combine([FORM, SCAN], [{"file": 1, "pages": [1]}, {"file": 0, "pages": [2, 1], "rotate": 90}])
        self.assertEqual(got["pages"], 3)
        self.assertEqual(docs.info(got["pdf"])["sizes"], [[612, 792], [612, 792], [792, 612]])
        with self.assertRaises(docs.DocError):
            docs.combine([FORM], [{"file": 0, "pages": [3]}])

    def test_a_page_as_a_picture_and_its_text_by_ocr(self):
        p = docs.page(FORM, 1, side=800, marks=True)
        self.assertEqual((p["jpeg"][:3], max(p["width"], p["height"]) <= 800, p["pages"]), (b"\xff\xd8\xff", True, 2))
        self.assertIn("Membership form", p["text"])
        top = docs.page(FORM, 1, side=800, part="top")
        self.assertLess(top["height"], p["height"])
        self.assertIn("INVOICE 4471", docs.text(SCAN, [1])["text"][0])  # no text layer: read by OCR
        for bad in (lambda: docs.page(FORM, 3), lambda: docs.info(b"%PDF-1.4 not really"), lambda: docs.fields(b"hello")):
            with self.assertRaises(docs.DocError):
                bad()

    def test_make_pdf_from_markdown(self):
        md = ("# Report\n\nHello **the user** <script>alert(1)</script>.\n\n| A | B |\n|---|--:|\n| 1 | 2 |\n\n- one\n  - two\n\n"
              "\\pagebreak\n\n## Second page\n\n[a link](javascript:alert(1)) and [ok](https://example.com)")
        got = docs.make_pdf(md, title="Report", paper="a4")
        self.assertEqual(got["pages"], 2)
        self.assertEqual(docs.info(got["pdf"])["sizes"], [[595, 842], [595, 842]])
        t = plain(got["pdf"])
        self.assertIn("<script>alert(1)</script>", t[0])  # shown as the words it is, never run
        self.assertIn("Second page", t[1])
        self.assertNotIn("Second page", t[0])
        self.assertIn("1 of 2", t[0])
        long_ = "# Fit\n\n" + "\n\n".join(f"Paragraph {i}: " + "words " * 40 for i in range(10))
        self.assertEqual((docs.make_pdf(long_)["pages"], docs.make_pdf(long_, fit=1)["pages"]), (2, 1))  # the type shrinks to fit
        land = docs.make_pdf("hi", landscape=True)
        self.assertEqual(docs.info(land["pdf"])["sizes"], [[792, 612]])

    def test_the_private_reader_does_the_same_in_memory(self):
        got = docs.fill(FORM, {"1": "Private the user"}, private=True)
        self.assertEqual(values(got["pdf"])["full_name"], "Private the user")
        self.assertEqual(docs.make_pdf("# Hi", private=True)["pages"], 1)


class Markdown(unittest.TestCase):
    def test_everything_written_is_escaped(self):
        h = docs.md_html('<img src="https://evil.example/x.png"> & <b>bold</b> **real**')
        self.assertIn("&lt;img src=\"https://evil.example/x.png\"&gt; &amp; &lt;b&gt;bold&lt;/b&gt; <b>real</b>", h)
        self.assertNotIn("<img", h)

    def test_links_and_pictures(self):
        h = docs.md_html("[x](javascript:alert(1)) [y](https://a.example/?q=1&r=2) ![cat](u0123456789abcdef) ![dog](https://x/y.png)",
                         images={"u0123456789abcdef": (b"\xff\xd8\xffjpeg", 1000, 500)}, content_width=500)
        self.assertNotIn("javascript:", h)
        self.assertIn('<a href="https://a.example/?q=1&amp;r=2">y</a>', h)
        self.assertIn('src="data:image/jpeg;base64,', h)
        self.assertIn('width="500" height="250"', h)  # fits the page's width
        self.assertIn("[picture: dog]", h)
        self.assertNotIn("https://x/y.png", h)

    def test_blocks(self):
        h = docs.md_html("## H\n\n1. a\n2. b\n   - c\n\n> q\n\n```\n<x> & y\n```\n\n| L | C | R |\n|:--|:-:|--:|\n| 1 | 2 | 3 |\n\n- [x] done")
        self.assertIn("<h2>H</h2>", h)
        self.assertIn("<ol><li>a</li><li>b<ul><li>c</li></ul></li></ol>", h)
        self.assertIn("<blockquote><p>q</p></blockquote>", h)
        self.assertIn("<pre>&lt;x&gt; &amp; y</pre>", h)
        self.assertIn('<th width="33%">L</th><th width="33%" class="c">C</th><th width="33%" class="r">R</th>', h)
        self.assertIn("<li>☑ done</li>", h)

    def test_table_columns_keep_their_longest_word(self):
        w = docs._widths([["Line", "Field", "Entry"], ["Part II", "Signature of U.S. person", "Not signed, to be signed by the user"]], 3)
        self.assertEqual(sum(w) <= 100, True)
        self.assertGreaterEqual(w[0] * 504 / 100, 4 * 7.5 + 30 - 5)  # "Line" fits on one line
        self.assertGreater(w[2], w[0])

    def test_pages_of(self):
        self.assertEqual(docs.pages_of("1-3, 5, 2", 6), [1, 2, 3, 5])
        self.assertEqual(docs.pages_of("all", 3), [1, 2, 3])
        self.assertEqual(docs.pages_of("4-end", 5), [4, 5])
        for spec in ("7", "3-1", "two", "0"):
            with self.assertRaises(docs.DocError):
                docs.pages_of(spec, 6)
        with self.assertRaises(docs.DocError):
            docs.pages_of("1-20", 30, cap=10)

    def test_file_names(self):
        self.assertEqual(docs.file_name("../W-9: filled?.pdf", "x"), "W-9 filled.pdf")
        self.assertEqual(docs.file_name("", "form (filled)"), "form (filled).pdf")
        self.assertEqual(len(docs.file_name("a" * 300)), 80)


def docx_bytes(text="Hello from Word"):
    import io
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return b.getvalue()


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-docs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.st = studio.Store(os.path.join(self.tmp, "S"))

    def attach(self, cid, data=None, name="form.pdf"):
        return self.st.add_upload(FORM if data is None else data, name, cid, text_ok=True)


class Store(StoreBase):
    def test_a_pdf_is_kept_whole_beside_its_text(self):
        cid = self.st.create_chat(kind="chat")["id"]
        u = self.attach(cid)
        self.assertEqual((u["kind"], u["orig"]), ("text", "pdf"))
        self.assertIn("a form with 5 fields", u["detail"])
        self.assertEqual(self.st.upload_orig(u["id"]), FORM)
        self.assertTrue(os.path.exists(self.st.path("refs", f"{u['id']}.thumb.jpg")))
        self.assertIn("Membership form", self.st.upload_text(u["id"]))
        self.assertEqual(oct(os.stat(self.st.path("refs", f"{u['id']}.orig")).st_mode & 0o777), "0o600")
        w = self.attach(cid, docx_bytes(), "letter.docx")
        self.assertEqual((w["orig"], self.st.upload_orig(w["id"])[:2]), ("docx", b"PK"))
        t = self.attach(cid, b"just text\n", "notes.txt")
        self.assertIsNone(t["orig"])
        with self.assertRaises(ValueError):
            self.st.upload_orig(t["id"])
        self.assertEqual([d["id"] for d in self.st.documents(cid)], [u["id"], w["id"]])
        self.st.remove_upload_files(u["id"], u["type"])
        self.assertFalse(os.path.exists(self.st.path("refs", f"{u['id']}.orig")))

    def test_a_private_chats_pdf_is_sealed_and_opens_with_the_grant(self):
        self.st.vault.unlock(PASS)
        with P.granted():
            cid = self.st.create_chat(kind="chat", private=True)["id"]
            u = self.attach(cid)
            self.assertEqual(u["orig"], "pdf")
            self.assertEqual(self.st.upload_orig(u["id"]), FORM)
            made = self.st.add_document(cid, docs.fill(FORM, {"1": "Sealed the user"}, private=True)["pdf"], "filled.pdf")
            self.assertEqual((made["made"], made["orig"], made["name"]), (1, "pdf", "filled.pdf"))
            with self.assertRaises(ValueError):
                self.st.add_document(cid, docx_bytes(), "x.docx")
        refs = os.listdir(self.st.path("refs"))
        self.assertIn(f"{u['id']}.orig.hpv", refs)
        self.assertFalse([f for f in refs if f.endswith((".orig", ".thumb.jpg"))], refs)  # nothing in plaintext, no thumbnail
        for f in refs:
            blob = open(self.st.path("refs", f), "rb").read()
            self.assertNotIn(b"Sealed the user", blob)
            self.assertNotIn(b"Membership form", blob)
        with self.assertRaises(P.Locked):
            self.st.upload_orig(u["id"])


class Engine:
    """A stub OpenAI endpoint (test_chat_tools.engine) for a turn's tool calls, and one that sees pictures (its /health
    says vision) answering a page question without streaming."""

    @staticmethod
    def seeing(answer, bodies):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                out = json.dumps({"source": "kyojin", "vision": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                out = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


class Tools(StoreBase):
    def setUp(self):
        super().setUp()
        self.chat = self.st.create_chat(kind="chat", model="quick-chat")
        self.cid = self.chat["id"]

    def ctx(self, chat=None, **kw):
        c = self.st.chat((chat or self.chat)["id"])
        return T.TurnCtx(c["id"], c, self.st, kw.pop("model_id", "dsv41"), private=bool(c.get("private")), **kw)

    def say(self, text, atts=()):
        meta = {"attachments": [{"id": u["id"], "kind": u["kind"], "name": u["name"], "detail": u.get("detail"),
                                 **({"doc": u["orig"]} if u.get("orig") else {})} for u in atts]}
        self.st.add_message(self.cid, "user", text, meta)

    def test_offered_only_with_a_pdf_or_when_asked_for_one(self):
        self.say("hello")
        self.assertIsNone(chat_docs.toolset(self.ctx()))  # a chat without a PDF stays on its plain path
        self.say("can you make that a PDF?")
        t = chat_docs.toolset(self.ctx())
        self.assertEqual([s["function"]["name"] for s in t.specs()], ["make_pdf"])
        self.say("letter size please")  # the answer to its question: still offered
        self.assertIsNotNone(chat_docs.toolset(self.ctx()))
        self.say("thanks")
        self.say("ok")
        self.assertIsNotNone(chat_docs.toolset(self.ctx()))  # from then on: tools that came and went re-read the whole chat
        u = self.attach(self.cid)
        self.say("fill this in", [u])
        t = chat_docs.toolset(self.ctx())
        self.assertEqual([s["function"]["name"] for s in t.specs()], list(chat_docs.TOOLS))
        note = t.turn_note(self.ctx())  # after the user's newest message (in the system words, a new PDF re-read the whole chat)
        self.assertIn(f"file_id {u['id']}: “form.pdf” (", note)
        self.assertIn("the user attached it", note)
        cz = self.st.create_chat(kind="chat")
        self.st.x("UPDATE chats SET cousteau='p1' WHERE id=?", (cz["id"],))
        self.assertIsNone(chat_docs.toolset(self.ctx(cz)))

    def test_another_chats_pdf_is_refused(self):
        other = self.st.create_chat(kind="chat")["id"]
        theirs = self.attach(other)
        mine = self.attach(self.cid)
        self.say("x", [mine])
        t = chat_docs.toolset(self.ctx())
        r = t.call("form_fields", {"file_id": theirs["id"]}, self.ctx())
        self.assertEqual(r["status"], "refused")
        self.assertIn(mine["id"], r["text"])
        r = t.call("read_pages", {"file_id": "/etc/passwd", "pages": "1"}, self.ctx())
        self.assertEqual(r["status"], "refused")

    def test_fields_fill_add_text_combine_and_make(self):
        u = self.attach(self.cid)
        self.say("x", [u])
        t, c = chat_docs.toolset(self.ctx()), self.ctx()
        r = t.call("form_fields", {"file_id": u["id"]}, c)
        self.assertIn("1. text · page 1 · empty · “Full name:” [full_name]", r["text"])
        self.assertIn("4. radio · page 1 · empty · “Plan:” [plan] · options: Basic (“Basic”); Pro (“Pro”)", r["text"])
        r = t.call("fill_form", {"file_id": u["id"], "values": {"1": "the user", "3": True, "4": "Basic", "8": "x"}}, c)
        self.assertEqual(r["status"], "ok", r["text"])
        card = r["audit"]["file"]
        self.assertEqual((card["name"], card["kind"], card["thumb"]), ("form (filled).pdf", "pdf", True))
        self.assertIn("Filled 3 fields", r["text"])
        self.assertIn("Not filled (1): 8: no such field", r["text"])
        made = self.st.upload(card["id"])
        self.assertEqual((made["made"], made["chat_id"]), (1, self.cid))
        self.assertEqual(values(self.st.upload_orig(card["id"]))["full_name"], "the user")
        r = t.call("add_text", {"file_id": card["id"], "items": [{"page": 1, "text": "555-0100", "after": "Phone:"}],
                                "name": "final.pdf", "flatten": True}, c)
        self.assertEqual((r["status"], r["audit"]["file"]["name"]), ("ok", "final.pdf"), r["text"])
        final = r["audit"]["file"]["id"]
        self.assertIn("555-0100", plain(self.st.upload_orig(final))[0])
        r = t.call("combine_pdfs", {"parts": [{"file_id": final, "pages": "2"}, {"file_id": u["id"], "pages": "1", "rotate": 180}],
                                    "name": "packet"}, c)
        self.assertEqual((r["status"], r["audit"]["file"]["name"]), ("ok", "packet.pdf"), r["text"])
        self.assertEqual(docs.info(self.st.upload_orig(r["audit"]["file"]["id"]))["pages"], 2)
        r = t.call("make_pdf", {"content": "# Summary\n\nAll done.", "title": "Summary for the user"}, c)
        self.assertEqual((r["status"], r["audit"]["file"]["name"]), ("ok", "Summary for the user.pdf"))
        r = t.call("read_pages", {"file_id": u["id"], "pages": "1"}, c)
        self.assertIn("## Page 1 of 2\nMembership form", r["text"])
        r = t.call("read_pages", {"file_id": u["id"], "pages": "9"}, c)
        self.assertEqual(r["status"], "failed")
        self.assertIn("Page 9 isn't in it: it has 2 pages.", r["text"])
        # the list after the user's newest message now shows what it made; the system note stays the same (the prompt's start)
        t2 = chat_docs.toolset(self.ctx())
        note = t2.turn_note(self.ctx())
        self.assertIn("“final.pdf” (", note)
        self.assertIn("you made it", note)
        self.assertNotIn("final.pdf", t2.system_note(self.ctx()))

    def test_make_pdf_places_this_chats_photos_only(self):
        png = subprocess.run([EXE, "make-png", os.path.join(self.tmp, "p.png"), "PHOTO"], capture_output=True, timeout=60)
        self.assertEqual(png.returncode, 0)
        ph = self.st.add_upload(open(os.path.join(self.tmp, "p.png"), "rb").read(), "p.png", self.cid, text_ok=True)
        self.say("make a pdf with my photo", [ph])
        t = chat_docs.toolset(self.ctx())
        r = t.call("make_pdf", {"content": f"# Photo\n\n![mine]({ph['id']})\n\n![theirs](u0000000000000000)", "title": "P"}, self.ctx())
        self.assertEqual(r["status"], "ok", r["text"])
        self.assertIn("u0000000000000000", r["text"])  # said: not a photo here
        pdf = self.st.upload_orig(r["audit"]["file"]["id"])
        self.assertIn(b"/Subtype /Image", pdf.replace(b"/Subtype/Image", b"/Subtype /Image"))

    def test_look_at_page_asks_the_seeing_engine_with_the_picture(self):
        u = self.attach(self.cid)
        self.say("x", [u])
        bodies = []
        srv = Engine.seeing("Field 1 is the full name.", bodies)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        halo_chat._VISION.clear()
        self.addCleanup(halo_chat._VISION.clear)
        c = self.ctx(model_id="qwen38-halogen")
        c.endpoint, c.served_model = f"http://127.0.0.1:{srv.server_address[1]}/v1", "served-qwen"
        t = chat_docs.toolset(c)
        r = t.call("look_at_page", {"file_id": u["id"], "page": 1, "question": "Which box is the name?", "show_fields": True}, c)
        self.assertEqual(r["status"], "ok", r["text"])
        self.assertIn("Field 1 is the full name.", r["text"])
        b = bodies[0]
        self.assertEqual(b["model"], "served-qwen")
        parts = b["messages"][0]["content"]
        self.assertIn("page 1 of 2", parts[0]["text"])
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/"))

    def test_look_at_page_without_eyes_says_so(self):
        u = self.attach(self.cid)
        self.say("x", [u])
        c = self.ctx()
        r = chat_docs.toolset(c).call("look_at_page", {"file_id": u["id"], "page": 1, "question": "what?"}, c)
        self.assertEqual(r["status"], "refused")
        self.assertIn("read_pages gives the page's text", r["text"])

    def test_look_at_page_with_the_macs_photo_model(self):
        from test_look import FAKE
        import look
        lk = look.Looker(self.st, worker_cmd=[sys.executable, FAKE, "--log", os.path.join(self.tmp, "w.log"), "--say", "A form."])
        self.addCleanup(lk.close)
        u = self.attach(self.cid)
        self.say("x", [u])
        c = self.ctx(app=types.SimpleNamespace(fake=True, studio={"looker": lk}))
        r = chat_docs.toolset(c).call("look_at_page", {"file_id": u["id"], "page": 2, "question": "what does it say?"}, c)
        self.assertEqual(r["status"], "ok", r["text"])
        self.assertEqual([f for f in os.listdir(lk.tmp) if f.endswith(".jpg")], [])  # the page's picture went at once


class Turn(StoreBase):
    def setUp(self):
        super().setUp()
        from test_chat_tools import engine
        self.script, self.bodies = [], []
        self.srv = engine(self.script, self.bodies)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.ep = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        self.agent = halo_chat.ChatAgent(self.st, chats.Relay())

    def test_a_turn_fills_the_form_and_the_answer_carries_its_card(self):
        chat = self.st.create_chat(kind="chat", model="quick-chat")
        u = self.attach(chat["id"])
        self.script += [{"calls": [("form_fields", {"file_id": u["id"]})]},
                        {"calls": [("fill_form", {"file_id": u["id"], "values": {"1": "Alex Example", "5": "Canada"}})]},
                        {"text": "Done: your filled form is below."}]
        evs = list(self.agent.turn(chat["id"], self.ep, "served", "quick-chat", "Fill in my name, I'm in Canada", [{"id": u["id"]}]))
        self.assertNotIn("error", [e["type"] for e in evs], evs)
        first = self.bodies[0]
        self.assertIn("fill_form", [t["function"]["name"] for t in first["tools"]])
        user = first["messages"][-1]["content"]
        self.assertIn(f"[form.pdf: a PDF the user attached, file_id {u['id']} (", user)
        self.assertIn(f"file_id {u['id']}: “form.pdf”", user)  # the chat's PDFs: after the user's message, not in the system words
        self.assertNotIn(u["id"], first["messages"][0]["content"])
        done = [e for e in evs if e["type"] == "tool_done"]
        self.assertEqual([bool(e.get("file")) for e in done], [False, True])
        card = done[1]["file"]
        ans = [m for m in self.st.messages(chat["id"]) if m["role"] == "assistant"][-1]
        tools = [p["tool"] for p in ans["meta"]["parts"] if p.get("tool")]
        self.assertEqual(tools[1]["file"], card)
        self.assertEqual(tools[1]["line"], "Filled 2 fields of form.pdf")
        self.assertEqual(values(self.st.upload_orig(card["id"]))["country"], "Canada")
        self.assertIn("Fill in my name", json.dumps(self.bodies[0]))
        audit = [json.loads(x) for x in open(self.st.path("chat-tools", f"{chat['id']}.jsonl"))]
        self.assertEqual(audit[1]["made"], card["id"])
        self.assertNotIn("file", audit[1])

    def test_a_file_made_again_under_its_name_replaces_the_first(self):
        chat = self.st.create_chat(kind="chat", model="quick-chat")
        self.script += [{"calls": [("make_pdf", {"content": "# One\n\n" + "Long. " * 900, "title": "Summary"})]},
                        {"calls": [("make_pdf", {"content": "# One", "title": "Summary"})]},
                        {"text": "Here it is, on one page."}]
        evs = list(self.agent.turn(chat["id"], self.ep, "served", "quick-chat", "Make me a one-page PDF summary", None))
        done = [e for e in evs if e["type"] == "tool_done"]
        first, second = done[0]["file"]["id"], done[1]["file"]["id"]
        self.assertEqual((done[0].get("replaces"), done[1]["replaces"]), (None, first))
        self.assertIn("It replaces the one you made before", self.bodies[2]["messages"][-1]["content"])
        made = [d["id"] for d in self.st.documents(chat["id"]) if d.get("made")]
        self.assertEqual(made, [second])
        with self.assertRaises(KeyError):
            self.st.upload(first)
        ans = [m for m in self.st.messages(chat["id"]) if m["role"] == "assistant"][-1]
        self.assertEqual([(p["tool"].get("file") or {}).get("id") for p in ans["meta"]["parts"] if p.get("tool")], [None, second])

    def test_a_chat_without_a_pdf_has_no_document_tools(self):
        chat = self.st.create_chat(kind="chat", model="quick-chat")
        self.script.append({"text": "Hi."})
        list(self.agent.turn(chat["id"], self.ep, "served", "quick-chat", "hello", None))
        self.assertNotIn("tools", self.bodies[0])


class Route(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("hc_test_studio_d", os.path.join(ROOT, "tests", "test_studio.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.ts = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ts)
        self.ts.Http.setUp(self)
        self.ts.Http.unlock(self)
        self.st = self.app.studio["store"]
        self.app.code_check = types.SimpleNamespace(view=lambda: {"clean": True, "problems": [], "line": "Running code: test, clean"})

    def req(self, *a, **k):
        return self.ts.Http.req(self, *a, **k)

    def test_upload_open_and_download(self):
        cid = self.st.create_chat(kind="chat")["id"]
        code, body, _ = self.req("POST", "/studio/api/uploads", raw=FORM, Content_Type="application/pdf", X_Upload_For="chat",
                                 X_Filename=quote("W-9 2026 ✓.pdf"), X_Chat=cid)
        self.assertEqual(code, 200, body)
        v = json.loads(body)
        self.assertEqual(v["doc"], "pdf")
        self.assertIn("a form with 5 fields", v["detail"])
        code, body, h = self.req("GET", f"/studio/doc/{v['id']}")
        self.assertEqual((code, body, h["Content-Type"]), (200, FORM, "application/pdf"))
        self.assertTrue(h["Content-Disposition"].startswith('inline; filename="W-9 2026 _.pdf"; filename*=UTF-8\'\'W-9%202026%20%E2%9C%93.pdf'))
        self.assertIsNone(h["Content-Security-Policy"])  # the browser's PDF viewer is a plugin
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        code, body, h = self.req("GET", f"/studio/doc/{v['id']}?dl=1")
        self.assertTrue(h["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(h["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual(self.req("GET", f"/studio/doc/{v['id']}", cookie=False)[0], 401)
        t = json.loads(self.req("POST", "/studio/api/uploads", raw=b"notes\n", Content_Type="text/plain", X_Upload_For="chat",
                                X_Filename="n.txt", X_Chat=cid)[1])
        self.assertEqual(self.req("GET", f"/studio/doc/{t['id']}")[0], 404)
        self.assertEqual(self.req("GET", "/studio/doc/u00000000000000000000")[0], 404)

    def test_a_private_chats_document_needs_its_token(self):
        code, body, _ = self.req("POST", "/studio/api/private/unlock", {"passcode": "correct horse"})
        tok = json.loads(body)["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        code, body, _ = self.req("POST", "/studio/api/uploads", raw=FORM, Content_Type="application/pdf", X_Upload_For="chat",
                                 X_Filename="secret.pdf", X_Chat=cid, X_Private="1", X_Halo_Private=tok)
        self.assertEqual(code, 200, body)
        uid = json.loads(body)["id"]
        self.assertEqual(self.req("GET", f"/studio/doc/{uid}")[0], 404)
        code, body, _ = self.req("GET", f"/studio/doc/{uid}", X_Halo_Private=tok)
        self.assertEqual((code, body), (200, FORM))


    def test_the_viewer_draws_a_pdfs_pages(self):
        """The app's viewer (an opened PDF left the installed app with no way back): the page count
        and each page as a JPEG the Mac draws, kept for the next look."""
        import studio_http
        cid = self.st.create_chat(kind="chat")["id"]
        v = json.loads(self.req("POST", "/studio/api/uploads", raw=FORM, Content_Type="application/pdf", X_Upload_For="chat",
                                X_Filename="form.pdf", X_Chat=cid)[1])
        code, body, _ = self.req("GET", f"/studio/doc/{v['id']}/info")
        self.assertEqual(code, 200, body)
        info = json.loads(body)
        self.assertGreaterEqual(info["pages"], 1)
        self.assertEqual(len(info["sizes"]), info["pages"])
        code, body, h = self.req("GET", f"/studio/doc/{v['id']}/page/1")
        self.assertEqual((code, h["Content-Type"], body[:3]), (200, "image/jpeg", b"\xff\xd8\xff"))
        self.assertEqual(h["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertIn((v["id"], 1), studio_http.PAGES)
        self.assertEqual(self.req("GET", f"/studio/doc/{v['id']}/page/1")[1], body)  # the kept copy
        code, body, _ = self.req("GET", f"/studio/doc/{v['id']}/page/{info['pages'] + 1}")
        self.assertEqual(code, 422, body)
        self.assertNotEqual(self.req("GET", f"/studio/doc/{v['id']}/page/0")[0], 200)
        self.assertEqual(self.req("GET", f"/studio/doc/{v['id']}/info", cookie=False)[0], 401)
        t = json.loads(self.req("POST", "/studio/api/uploads", raw=b"notes\n", Content_Type="text/plain", X_Upload_For="chat",
                                X_Filename="n.txt", X_Chat=cid)[1])
        self.assertEqual(self.req("GET", f"/studio/doc/{t['id']}/info")[0], 404)

    def test_the_viewer_draws_a_few_pages_at_once(self):
        """Review: each page drawn runs the helper on the whole PDF; a fast scroll asked for a dozen at once."""
        import studio_http
        cid = self.st.create_chat(kind="chat")["id"]
        v = json.loads(self.req("POST", "/studio/api/uploads", raw=FORM, Content_Type="application/pdf", X_Upload_For="chat",
                                X_Filename="form.pdf", X_Chat=cid)[1])
        mu, now, most = threading.Lock(), [0], [0]
        real = docs.page
        def page(*a, **k):
            with mu:
                now[0] += 1
                most[0] = max(most[0], now[0])
            time.sleep(0.2)
            with mu:
                now[0] -= 1
            return {"jpeg": b"\xff\xd8\xff"}
        docs.page = page
        self.addCleanup(setattr, docs, "page", real)
        ts = [threading.Thread(target=self.req, args=("GET", f"/studio/doc/{v['id']}/page/{n}")) for n in range(1, 9)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(most[0], getattr(studio_http, "PAGES_AT_ONCE", 2))

    def test_a_private_chats_pages_need_its_token_and_are_kept_nowhere(self):
        import studio_http
        tok = json.loads(self.req("POST", "/studio/api/private/unlock", {"passcode": "correct horse"})[1])["token"]
        cid = json.loads(self.req("POST", "/studio/api/chats", {"kind": "chat", "private": True}, X_Halo_Private=tok)[1])["id"]
        uid = json.loads(self.req("POST", "/studio/api/uploads", raw=FORM, Content_Type="application/pdf", X_Upload_For="chat",
                                  X_Filename="secret.pdf", X_Chat=cid, X_Private="1", X_Halo_Private=tok)[1])["id"]
        self.assertEqual(self.req("GET", f"/studio/doc/{uid}/info")[0], 404)
        self.assertEqual(self.req("GET", f"/studio/doc/{uid}/page/1")[0], 404)
        code, body, _ = self.req("GET", f"/studio/doc/{uid}/info", X_Halo_Private=tok)
        self.assertEqual(code, 200, body)
        code, body, h = self.req("GET", f"/studio/doc/{uid}/page/1", X_Halo_Private=tok)
        self.assertEqual((code, body[:3]), (200, b"\xff\xd8\xff"))
        self.assertNotIn((uid, 1), studio_http.PAGES)

class Container(StoreBase):
    def test_attachment_files_and_a_shown_document(self):
        cid = self.st.create_chat(kind="chat")["id"]
        u = self.attach(cid)
        files = chat_box.attachment_files(self.st, u["id"])
        self.assertEqual([n for _, n in files], ["form.pdf", "form.pdf.txt"])
        self.assertEqual(files[0][0], FORM)
        made = docs.make_pdf("# From the box")["pdf"]
        box = chat_box.BoxTools.__new__(chat_box.BoxTools)
        box._call = lambda ctx, req, wait, **k: {"ok": True, "b64": base64.b64encode(made).decode(), "name": "report.pdf"}
        c = self.st.chat(cid)
        ctx = T.TurnCtx(cid, c, self.st, "dsv41")
        r = box.show({"path": "out/report.pdf"}, ctx, 0)
        self.assertEqual(r["status"], "ok", r["text"])
        card = r["audit"]["file"]
        self.assertEqual((card["name"], card["kind"]), ("report.pdf", "pdf"))
        self.assertEqual(self.st.upload_orig(card["id"]), made)
        box._call = lambda ctx, req, wait, **k: {"ok": True, "b64": base64.b64encode(docx_bytes()).decode(), "name": "memo.docx"}
        r = box.show({"path": "memo.docx"}, ctx, 0)
        self.assertEqual((r["status"], r["audit"]["file"]["kind"]), ("ok", "docx"))


if __name__ == "__main__":
    unittest.main()
