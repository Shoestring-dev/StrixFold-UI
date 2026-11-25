"""The page's first open, on the bare address (what `open http://127.0.0.1:8711/` shows a new user): chat.js sends it to
#/talk, so studio.js, loaded after it, finds an address to route and shows the passcode form. Without the address it
showed a composer whose messages went nowhere."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FirstOpen(unittest.TestCase):
    def test_the_bare_address_goes_to_the_chat(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("no node")
        with open(os.path.join(ROOT, "mac", "ui", "chat.js"), encoding="utf-8") as f:
            src = f.read()
        head = src[src.index("  function route() {"):]
        head = head[:head.index("    route.seen = true;")] + "    went.push(m[1]); }\n"  # up to where it has a page to show
        retired = src[src.index("  const RETIRED = "):].split("\n")[0]
        js = retired + "\nlet went; const location = { hash: \"\", replace: (x) => went.push(x) };\n" + head + """
        const out = {};
        for (const h of ["", "#", "#/talk", "#/studio-settings", "#/chat", "#/nowhere", "#/talk/0123456789ab"]) { went = []; location.hash = h; route.seen = false; route(); out[h] = went; }
        console.log(JSON.stringify(out));"""
        r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"": ["#/talk"], "#": ["#/talk"], "#/talk": ["talk"], "#/studio-settings": ["studio-settings"], "#/chat": ["#/talk"],
                                                "#/nowhere": ["#/talk"], "#/talk/0123456789ab": ["talk"]})


if __name__ == "__main__":
    unittest.main()
