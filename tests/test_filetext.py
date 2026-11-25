"""Any file in StrixFold UI's chat: PDFs by their text layer and, when scanned, by on-device OCR; images by OCR; text in
UTF-8/16, Windows-1252 and Mac Roman; HTML without its scripts or links; .rtf/.doc/.odt through textutil; a .zip's
listing and readable members within caps (a bomb member, a nested archive and an encrypted member stay closed); anything
else as name, type, size and sha256; text cut to the limit with a note. Every file is synthetic: the PDFs and pictures
are drawn here by the helper's test commands."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import filetext as T  # noqa: E402
import office_fixtures as F  # noqa: E402

HAVE_SWIFT = os.path.exists("/usr/bin/xcrun") and subprocess.run(["/usr/bin/xcrun", "--find", "swiftc"], capture_output=True).returncode == 0


class Helper(unittest.TestCase):
    """PDFKit and Vision through halo-extract (built into a temporary directory here)."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_SWIFT:
            raise unittest.SkipTest("Xcode's swiftc is not installed")
        cls.old, T.BIN_DIR = T.BIN_DIR, tempfile.mkdtemp(prefix="halo-extract-")
        cls.tmp = tempfile.mkdtemp(prefix="halo-files-")
        cls.exe = T.helper()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(T.BIN_DIR, True)
        shutil.rmtree(cls.tmp, True)
        T.BIN_DIR = cls.old

    def make(self, cmd, text, *extra):
        out = os.path.join(self.tmp, f"{cmd}-{len(os.listdir(self.tmp))}")
        subprocess.run([self.exe, cmd, out, text, *extra], check=True, capture_output=True)
        return open(out, "rb").read()

    def test_a_pdf_with_text(self):
        d = T.read(self.make("make-pdf", "Quarterly sales report: North 1,234 units"), "report.pdf")
        self.assertEqual((d.kind, d.detail), ("pdf", "1 page · 6 words"))
        self.assertEqual(d.text, "## Page 1\nQuarterly sales report: North 1,234 units")

    def test_a_scanned_pdf_by_ocr(self):
        d = T.read(self.make("make-pdf", "Scanned invoice 4471 total 99 dollars", "scan"), "scan.pdf")
        self.assertEqual(d.detail, "1 page · OCR · 6 words")
        self.assertIn("invoice 4471", d.text)

    def test_an_image_by_ocr(self):
        d = T.read(self.make("make-png", "Whiteboard: ship on Friday"), "board.png", media_kind="image")
        self.assertEqual((d.kind, d.detail, d.text), ("image", "1400×500 · OCR · 4 words", "Whiteboard: ship on Friday"))
        blank = self.make("make-png", " ")
        self.assertEqual(T.read(blank, "blank.png", media_kind="image").detail, "1400×500 · no text found")

    def test_a_picture_in_a_chat_keeps_its_image_and_gives_its_text(self):
        import chats
        import halo_chat
        import studio
        st = studio.Store(os.path.join(self.tmp, "store"))
        u = st.add_upload(self.make("make-png", "Whiteboard: ship on Friday"), "board.png", text_ok=True)
        self.assertEqual((u["kind"], u["detail"], st.upload_text(u["id"])), ("image", "1400×500 · OCR · 4 words", "Whiteboard: ship on Friday"))
        c = st.create_chat(kind="chat")
        st.add_message(c["id"], "user", "what does it say?", {"attachments": [{"id": u["id"], "kind": "image", "name": "board.png",
                                                                                "chars": u["chars"], "detail": u["detail"]}]})
        msgs, _ = halo_chat.ChatAgent(st, chats.Relay()).history(st.chat(c["id"]), 16384)
        self.assertIn("the text recognised in it on the user's Mac:]\n```\nWhiteboard: ship on Friday\n```", msgs[1]["content"])
        pdf = st.add_upload(self.make("make-pdf", "Scanned invoice 4471", "scan"), "scan.pdf", text_ok=True)
        self.assertEqual((pdf["kind"], pdf["detail"]), ("text", "1 page · OCR · 3 words"))

    def test_a_broken_pdf_is_attached_by_its_details(self):
        d = T.read(b"%PDF-1.7\n%broken", "broken.pdf")
        self.assertEqual(d.kind, "file")
        self.assertIn("this PDF couldn't be read", d.text)


class Text(unittest.TestCase):
    def test_encodings(self):
        for data, want in (("name,city\nRené,Zürich\n".encode("cp1252"), "name,city\nRené,Zürich\n"),
                           ("name,city\nRené,Zürich – café\n".encode("mac_roman"), "name,city\nRené,Zürich – café\n"),
                           ("a,b\n1,2\n".encode("utf-16"), "a,b\n1,2\n"), ("﻿a,b\nä,ö\n".encode(), "a,b\nä,ö\n"),
                           ("x\ty\n".encode("utf-32"), "x\ty\n")):
            d = T.read(data, "export.csv")
            self.assertEqual((d.kind, d.text), ("text", want), data[:20])
        self.assertEqual(T.read("a,b\n1,2\n\n3,4\n".encode(), "x.csv").detail, "3 rows")
        self.assertEqual(T.read(b"notes", "notes.txt").detail, "5 characters")

    def test_html_without_scripts_or_links(self):
        d = T.read(b"<html><head><style>x{}</style><script>alert(1)</script></head><body><h1>Title</h1><p>Hello <b>there</b> &amp; you"
                   b"</p><img src='http://example.com/x.png'></body></html>", "page.html")
        self.assertEqual(d.text, "Title\n\nHello there & you")

    def test_rich_text_and_word_through_textutil(self):
        for fmt, name in (("rtf", "letter.rtf"), ("doc", "old.doc"), ("odt", "x.odt")):
            data = subprocess.run(["/usr/bin/textutil", "-convert", fmt, "-stdout", "-stdin"], input=b"Dear Alex, the plan holds",
                                  capture_output=True, check=True).stdout
            d = T.read(data, name)
            self.assertEqual((d.kind, d.text, d.detail), ("document", "Dear Alex, the plan holds", "5 words"), name)

    def test_cut_to_the_limit_with_a_note(self):
        d = T.read(("line of text\n" * 10000).encode(), "big.txt", limit=5000)
        self.assertTrue(d.truncated)
        self.assertTrue(d.text.endswith("(truncated here: the rest of this file did not fit)"))
        self.assertLess(len(d.text), 5100)
        self.assertEqual(d.detail, "5,044 characters · first part read")


class OldExcel(unittest.TestCase):
    """.xls through xls_reader.py (xlrd 2.0.1 in its pinned venv, tools/setup_office_reader.sh); tests/fixtures/sample.xls
    is synthetic."""

    def setUp(self):
        if not os.path.exists(os.path.join(T.OFFICE_VENV, "bin", "python")):
            self.skipTest("the office reader venv is not set up (tools/setup_office_reader.sh)")
        self.data = open(os.path.join(ROOT, "tests", "fixtures", "sample.xls"), "rb").read()

    def test_an_old_workbook_as_tables(self):
        d = T.read(self.data, "report.xls")
        self.assertEqual((d.kind, d.detail, d.truncated), ("xls", "3 sheets · 3,006 rows", False))
        self.assertEqual(d.text.split("\n\n")[0].splitlines(), [
            '## Sheet "Sales": 5 rows, columns B-F, as CSV with the sheet\'s row numbers', "row,B,C,D,E,F",
            "1,Region,Item,Sales,Share,Date", '2,North,Widgets,"$1,234.50",45.67%,2025-10-20', "3,North,Gadgets,$99.00,,2025-10-21",
            '5,South,Gizmos,-$20.00,"inline, ""quoted""",', "6,Total,,,,TRUE"])
        self.assertIn('## Sheet "Notes" (hidden): 1 row', d.text)
        cut = T.read(self.data, "report.xls", limit=3000)
        self.assertRegex(cut.detail, r"^3 sheets · 3,006 rows · first [\d,]+ read$")
        self.assertRegex(cut.text, r"\(truncated after [\d,]+ rows of 3,000: the rest did not fit\)$")

    def test_without_the_reader_its_details_and_what_to_do(self):
        old = T.OFFICE_VENV
        T.OFFICE_VENV = os.path.join(tempfile.gettempdir(), "no-such-venv")
        self.addCleanup(setattr, T, "OFFICE_VENV", old)
        d = T.read(self.data, "report.xls")
        self.assertEqual(d.kind, "file")
        self.assertIn("Save it as .xlsx or .csv", d.text)


class Archives(unittest.TestCase):
    def test_a_zip_within_caps(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("notes/readme.txt", "Read me first")
            z.writestr("data.csv", "a,b\n1,2\n")
            z.writestr("sales.xlsx", F.xlsx())
            z.writestr("bomb.txt", b"\x00" * (20 << 20))     # far past the ratio cap: never opened
            z.writestr("inner.zip", b"PK\x03\x04 nested")      # archives inside stay closed
            z.writestr("blob.bin", bytes(range(256)) * 8)
        d = T.read(b.getvalue(), "pack.zip")
        self.assertEqual((d.kind, d.detail), ("zip", "6 files · 3 read"))
        self.assertIn("- bomb.txt (20 MB)", d.text)
        self.assertIn("## notes/readme.txt\nRead me first", d.text)
        self.assertIn('## Sheet "Sales"', d.text)
        self.assertNotIn("nested", d.text)

    def test_member_caps(self):
        old = T.ZIP_MAX_MEMBERS
        T.ZIP_MAX_MEMBERS = 3
        self.addCleanup(setattr, T, "ZIP_MAX_MEMBERS", old)
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for i in range(10):
                z.writestr(f"f{i}.txt", f"file {i}")
        d = T.read(b.getvalue(), "many.zip")
        self.assertIn("- … and 7 more", d.text)
        self.assertEqual(d.detail, "10 files · 3 read")


class Anything(unittest.TestCase):
    def test_unreadable_files_come_as_their_details(self):
        blob = bytes(range(256)) * 20
        d = T.read(blob, "model.bin")
        self.assertEqual((d.kind, d.detail), ("file", "5 KB · content not readable"))
        self.assertIn("type BIN, 5 KB, sha256 ", d.text)
        d = T.read(F.O.CFB + b"\x00" * 600 if hasattr(F, "O") else b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600, "report.xls")
        self.assertEqual(d.detail, "608 bytes · content not readable")
        self.assertIn("Save it as .xlsx or .csv", d.text)
        self.assertEqual(T.read(b"", "empty.txt").detail, "0 characters")


if __name__ == "__main__":
    unittest.main()
