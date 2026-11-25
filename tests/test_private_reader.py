"""A Private chat's file reader (mac/private_reader.py): its own halo-extract built from the source bytes
checked at start and given to swiftc on stdin, checked before every run; PDFs, photo text, a phone's HEIC and the
vision model's picture read in memory; nothing of the file on disk in any form. Synthetic files only, made by the
helper's own test modes. Needs Xcode's swiftc (skipped without it)."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import private_reader as R  # noqa: E402
import private_store as P  # noqa: E402
import studio  # noqa: E402

SRC = open(os.path.join(ROOT, "mac", "halo_extract.swift"), "rb").read()
CANARY = "tangerine-canary-reader-3c1"
PASS = "correct horse battery staple"


def forms(s):
    b = s.encode()
    out = {b, s.encode("utf-16-le"), b.hex().encode()}
    for pad in range(3):
        out.add(base64.b64encode(b"\0" * pad + b)[4:-4])
    return out


def hits(root, since=None):
    found = []
    walk = os.walk(root) if os.path.isdir(root) else [(os.path.dirname(root), [], [os.path.basename(root)])]
    for dp, dn, fn in walk:
        for f in fn:
            p = os.path.join(dp, f)
            try:
                st = os.stat(p)
                if (since and st.st_mtime < since) or st.st_size > 50 << 20:
                    continue
                data = open(p, "rb").read()
            except OSError:
                continue
            found += [p for x in forms(CANARY) if x in data]
    return sorted(set(found))


def new_temp(since):
    """The temp folder's entries made or changed since `since` (where mkdtemp and temp files go): its 5 million old files
    aren't walked. A new file deep inside an old folder would be missed; the app makes its temp files at the top."""
    root = tempfile.gettempdir()
    out = []
    for e in os.scandir(root):
        try:
            if e.stat(follow_symlinks=False).st_mtime >= since:
                out.append(e.path)
        except OSError:
            pass
    return out

@unittest.skipUnless(shutil.which("xcrun") and subprocess.run(["/usr/bin/xcrun", "--find", "swiftc"], capture_output=True).returncode == 0,
                     "needs Xcode's swiftc")
class Reader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rd = R.Reader(SRC, True)
        cls.exe = cls.rd.binary()
        # the synthetic files, made by the helper's own test modes, read into memory, then their folder removed
        d = tempfile.mkdtemp(prefix="hc-reader-fixtures-")
        try:
            run = lambda *a: subprocess.run([cls.exe, *a], capture_output=True, check=True)  # noqa: E731
            run("make-pdf", os.path.join(d, "t.pdf"), f"the plan is {CANARY}")
            run("make-pdf", os.path.join(d, "s.pdf"), f"scanned {CANARY}", "scan")
            run("make-png", os.path.join(d, "t.png"), f"note {CANARY}")
            run("make-photo", os.path.join(d, "p.jpg"), f"photo {CANARY}")
            subprocess.run(["sips", "-s", "format", "heic", os.path.join(d, "t.png"), "--out", os.path.join(d, "t.heic")],
                           capture_output=True)
            cls.files = {f: open(os.path.join(d, f), "rb").read() for f in os.listdir(d)}
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls.rd.close()

    def test_its_own_binary_from_the_checked_bytes(self):
        self.assertEqual(os.stat(os.path.dirname(self.exe)).st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(os.path.dirname(self.exe)), ["halo-extract"])  # no source file was written
        with self.assertRaisesRegex(R.ReaderError, "changed since StrixFold UI started"):
            R.Reader(SRC, False).binary()  # the source wasn't HEAD's at start
        other = R.Reader(SRC, True)
        exe = other.binary()
        with open(exe, "ab") as f:
            f.write(b"\0")  # swapped after the build
        with self.assertRaisesRegex(R.ReaderError, "changed since StrixFold UI started"):
            other.run("image", self.files["t.png"])
        other.close()
        self.assertFalse(os.path.exists(exe))

    def test_files_go_in_on_stdin(self):
        self.assertEqual(self.rd.run("pdf", self.files["t.pdf"], 50, 5)["text"], [f"the plan is {CANARY}"])
        s = self.rd.run("pdf", self.files["s.pdf"], 50, 5)
        self.assertEqual((s["ocr"], s["text"]), (1, [f"scanned {CANARY}"]))
        self.assertEqual(self.rd.run("image", self.files["t.png"])["text"], f"note {CANARY}")
        self.assertIsNone(self.rd.run("image", b"not an image"))

    def test_see_is_upright_and_has_no_metadata(self):
        src = self.files["p.jpg"]
        self.assertIn(b"HaloTestPhone", src)
        jpeg, w, h = self.rd.see(src, 1 << 20)
        self.assertEqual(jpeg[:3], b"\xff\xd8\xff")
        self.assertLess(w, h)  # EXIF orientation 6: turned upright
        self.assertLessEqual(w * h, 1 << 20)
        for x in (b"HaloTestPhone", b"Model Q", b"halo test comment"):
            self.assertNotIn(x, jpeg)

    def test_a_private_chats_files_are_read_in_memory_and_kept_sealed(self):
        tmp = tempfile.mkdtemp(prefix="hs-private-reader-")
        self.addCleanup(shutil.rmtree, tmp, True)
        st = studio.Store(os.path.join(tmp, "S"))
        st.vault.unlock(PASS)
        old = R.READER
        R.READER = self.rd
        self.addCleanup(setattr, R, "READER", old)
        t0 = time.time() - 1
        with P.granted():
            cid = st.create_chat(kind="chat", private=True)["id"]
            pdf = st.add_upload(self.files["t.pdf"], "plan.pdf", cid, text_ok=True)
            scan = st.add_upload(self.files["s.pdf"], "scan.pdf", cid, text_ok=True)
            photo = st.add_upload(self.files["p.jpg"], "IMG_1.jpg", cid, text_ok=True)
            self.assertEqual((pdf["kind"], pdf["detail"]), ("text", "1 page · 4 words"))
            self.assertIn(CANARY, st.upload_text(pdf["id"]))
            self.assertIn(CANARY, st.upload_text(scan["id"]))
            self.assertEqual((photo["kind"], photo["type"]), ("image", "jpeg"))
            self.assertEqual(st.upload_text(photo["id"]), f"photo {CANARY}")
            self.assertEqual(photo["detail"], "Photo · OCR · 2 words")
            self.assertNotIn(b"HaloTestPhone", st.upload_bytes(photo["id"]))
            if "t.heic" in self.files:  # a phone photo: a JPEG made in memory
                heic = st.add_upload(self.files["t.heic"], "IMG_2.HEIC", cid, text_ok=True)
                self.assertEqual(heic["type"], "jpeg")
                self.assertEqual(st.upload_bytes(heic["id"])[:3], b"\xff\xd8\xff")
            import halo_chat
            uri = halo_chat.picture(st, photo["id"])
            self.assertTrue(uri.startswith("data:image/jpeg;base64,"))
            with self.assertRaisesRegex(ValueError, "Office files"):
                st.add_upload(b"PK\x03\x04 a docx", "a.docx", cid, text_ok=True)
        self.assertTrue(all(f.endswith(".hpv") for f in os.listdir(st.path("refs"))), os.listdir(st.path("refs")))
        self.assertEqual(hits(st.root), [])
        self.assertEqual([h for p in new_temp(t0) for h in hits(p, since=t0)], [])

    def test_without_a_reader_a_private_chat_says_why(self):
        tmp = tempfile.mkdtemp(prefix="hs-private-reader2-")
        self.addCleanup(shutil.rmtree, tmp, True)
        st = studio.Store(os.path.join(tmp, "S"))
        st.vault.unlock(PASS)
        old = R.READER
        R.READER = R.Reader(SRC, False)
        self.addCleanup(setattr, R, "READER", old)
        with P.granted():
            cid = st.create_chat(kind="chat", private=True)["id"]
            for f in ("t.pdf", "p.jpg"):
                with self.assertRaisesRegex(ValueError, "changed since StrixFold UI started"):
                    st.add_upload(self.files[f], f, cid, text_ok=True)
            self.assertEqual(st.add_upload(b"plain words\n", "n.txt", cid, text_ok=True)["kind"], "text")  # text still works
        self.assertEqual([f for f in os.listdir(st.path("refs"))], [f for f in os.listdir(st.path("refs")) if f.endswith(".txt.hpv")])


if __name__ == "__main__":
    unittest.main()
