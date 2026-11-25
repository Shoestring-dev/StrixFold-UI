"""A Private chat's file reader.

The shared halo-extract (filetext.helper) is a file under ~/Library/Application Support that any agent running as the
user could swap, and filetext hands it a temp file. A Private chat's PDFs and photos go through this reader instead:
  - its own halo-extract, built once per app run by swiftc from the Swift source bytes read at start (private_code
    checks them against HEAD's blob; changed ones refuse) and handed to swiftc on stdin, into a fresh 0700 folder;
  - before every run the binary's sha256 is checked against the build's;
  - the file goes in on stdin ("-") and only JSON comes back on stdout: its text (PDFKit, Vision OCR) and, for a vision
    model, a fresh upright JPEG without metadata ("see"). Nothing of the file is written anywhere.
When the reader can't be used, the words say why; a Private chat then takes text files only.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading

FRAMEWORKS = ("PDFKit", "Vision", "AppKit")
CHANGED = "The photo and PDF reader changed since StrixFold UI started. Restart StrixFold UI to use it in a Private chat."
NO_READER = "The photo and PDF reader isn't available on this Mac (it needs Xcode's swiftc)."


class ReaderError(RuntimeError):
    """Plain words for the page: why a Private chat can't read this file now."""


class Reader:
    def __init__(self, source, verified, swiftc=("/usr/bin/xcrun", "swiftc")):
        self.source, self.verified, self.swiftc = source, bool(verified), tuple(swiftc)
        self.mu = threading.Lock()
        self.dir = self.exe = self.sha = None

    def _sha(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()

    def binary(self):
        """The reader's own halo-extract, built on first use from the start-verified bytes; ReaderError otherwise."""
        with self.mu:
            if self.exe is None:
                if not self.verified or not self.source:
                    raise ReaderError(CHANGED)
                d = tempfile.mkdtemp(prefix="halo-private-reader-")
                os.chmod(d, 0o700)
                exe = os.path.join(d, "halo-extract")
                argv = list(self.swiftc) + ["-O", "-o", exe, "-"] + [x for f in FRAMEWORKS for x in ("-framework", f)]
                env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.path.expanduser("~"), "TMPDIR": d + "/",
                       "LANG": "C"}  # nothing else from the app's environment (no DEVELOPER_DIR, TOOLCHAINS or SDKROOT)
                try:
                    r = subprocess.run(argv, input=self.source, capture_output=True, timeout=300, cwd=d, env=env)
                except (OSError, subprocess.TimeoutExpired):
                    r = None
                if r is None or r.returncode != 0 or not os.path.exists(exe):
                    shutil.rmtree(d, ignore_errors=True)
                    raise ReaderError(NO_READER)
                self.dir, self.exe, self.sha = d, exe, self._sha(exe)
            try:
                if self._sha(self.exe) != self.sha:
                    raise ReaderError(CHANGED)
            except OSError:
                raise ReaderError(CHANGED) from None
            return self.exe

    def run(self, mode, data, *args, timeout=120, errors=False):
        """The helper's JSON for one file given on stdin, or None when it can't read it (an "error" answer; with errors,
        that answer itself: docs.py words it)."""
        exe = self.binary()
        try:
            r = subprocess.run([exe, mode, "-"] + [str(a) for a in args], input=bytes(data), capture_output=True, timeout=timeout,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C"}, cwd=self.dir)
        except subprocess.TimeoutExpired:
            return None
        try:
            out = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None
        return None if out.get("error") and not errors else out

    def see(self, data, max_pixels, quality=0.85):
        """(jpeg bytes, width, height) of a photo for a vision model: upright, at most max_pixels, no metadata; or None."""
        r = self.run("see", data, int(max_pixels), quality)
        if not r or not r.get("jpeg"):
            return None
        return base64.b64decode(r["jpeg"]), int(r.get("width") or 0), int(r.get("height") or 0)

    def close(self):
        with self.mu:
            if self.dir:
                shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = self.exe = self.sha = None


READER = None  # set by private_code.install at app start


def reader():
    """The app's reader; ReaderError when the app didn't set one up (no code check at start)."""
    if READER is None:
        raise ReaderError(CHANGED)
    return READER
