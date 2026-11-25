"""The running code, checked before Private unlocks (mac/private_code.py), on a
fixture git repository: a changed tracked file (even with git's caches told to look past it), skip-worktree and
assume-unchanged, an untracked file under mac/, this app's own bytecode (another Python's doesn't count), HEAD moving,
GitHub's main; the page's files as read at start; and the import guard, in a child Python: a planted mac/array.py never
loads, a file that wasn't there at start doesn't, a changed one doesn't, and a .py loads from the bytes that were
hashed."""
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
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import private_code as PC  # noqa: E402


def sh(repo, *args):
    return subprocess.run(["/usr/bin/git", "-C", repo, *args], capture_output=True, check=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                   GIT_COMMITTER_EMAIL="t@t")).stdout.decode().strip()


class Check(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp(prefix="hc-code-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        sh(self.repo, "init", "-q", "-b", "main")
        for rel, text in (("mac/a.py", "A = 1\n"), ("mac/ui/page.js", "x()\n"), ("box/b.py", "B = 2\n"), ("docs/plan.md", "x\n"),
                          ("mac/extra/seal.py", "S = 1\n")):
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
            open(os.path.join(self.repo, rel), "w").write(text)
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-q", "-m", "landed")
        self.gh = {"sha": None}
        self.c = PC.CodeCheck(self.repo, github=lambda: self.gh["sha"])
        self.gh["sha"] = self.c.head

    def test_clean_and_its_line(self):
        self.assertEqual(self.c.findings(), [])
        v = self.c.view()
        self.assertTrue(v["clean"])
        self.assertEqual(v["github"], "on GitHub's main")
        self.assertRegex(v["line"], r"^Running code: [0-9a-f]{12}, landed .+, clean \(on GitHub's main\)$")
        open(os.path.join(self.repo, "docs", "new.md"), "w").write("untracked docs don't count\n")
        open(os.path.join(self.repo, "mac", ".DS_Store"), "w").write("x")
        self.assertEqual(self.c.findings(), [])

    def test_a_changed_tracked_file_even_with_gits_caches_looking_away(self):
        p = os.path.join(self.repo, "mac", "a.py")
        st = os.stat(p)
        open(p, "w").write("A = 9\n")
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # the same size and mtime: git's stat cache would miss it
        subprocess.run(["/usr/bin/git", "-C", self.repo, "update-index", "--refresh"], capture_output=True)  # git may or may not notice
        self.assertIn("mac/a.py was changed", self.c.findings())
        self.assertFalse(self.c.view()["clean"])

    def test_skip_worktree_and_assume_unchanged(self):
        for flag in ("--skip-worktree", "--assume-unchanged"):
            sh(self.repo, "update-index", flag, "box/b.py")
            self.assertIn("box/b.py is marked for git to look past", self.c.findings(), flag)
            sh(self.repo, "update-index", flag.replace("--", "--no-"), "box/b.py")
        self.assertEqual(self.c.findings(), [])

    def test_untracked_files_and_bytecode(self):
        os.makedirs(os.path.join(self.repo, "mac", "__pycache__"))
        os.makedirs(os.path.join(self.repo, "mac", "extra", "__pycache__"))
        open(os.path.join(self.repo, "mac", "extra", "__pycache__", "seal.cpython-312.pyc"), "wb").write(b"x")
        self.assertEqual(self.c.findings(), [])  # another Python's bytecode isn't this app's
        os.makedirs(os.path.join(self.repo, "mac", "extra", ".cache"))
        self.addCleanup(setattr, PC, "DEPLOY_COPY", PC.DEPLOY_COPY)
        PC.DEPLOY_COPY = ("mac/extra/.cache/",)
        for f in ("job.py", "job.commit"):  # a deploy's copy, never imported
            open(os.path.join(self.repo, "mac", "extra", ".cache", f), "w").write("x = 1\n")
        self.assertEqual(self.c.findings(), [])
        open(os.path.join(self.repo, "mac", "extra", "job.py"), "w").write("x = 1\n")  # beside it: refused
        self.assertEqual(self.c.findings(), ["mac/extra/job.py is there but not in git"])
        os.remove(os.path.join(self.repo, "mac", "extra", "job.py"))
        open(os.path.join(self.repo, "mac", "__pycache__", f"a.{PC.OWN_PYC}.pyc"), "wb").write(b"x")
        open(os.path.join(self.repo, "mac", "array.py"), "w").write("raise SystemExit\n")
        f = self.c.findings()
        self.assertIn("mac/array.py is there but not in git", f)
        self.assertTrue(any(x.startswith(f"mac/__pycache__/a.{PC.OWN_PYC}.pyc is there but not in git (Python's own cache") for x in f), f)

    def test_head_moving_and_githubs_main(self):
        open(os.path.join(self.repo, "mac", "a.py"), "a").write("# more\n")
        sh(self.repo, "commit", "-q", "-am", "later")
        f = self.c.findings()
        self.assertIn("the checkout moved to another commit since StrixFold UI started", f)
        sh(self.repo, "reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(self.c.findings(), [])
        self.gh["sha"] = "0" * 40
        v = self.c.view()
        self.assertEqual((v["clean"], v["github"]), (False, "not GitHub's main"))
        self.assertIn("the running commit isn't GitHub's main", v["problems"])
        self.gh["sha"] = None
        v = self.c.view()
        self.assertEqual((v["clean"], v["github"]), (True, "not checked on GitHub"))

    def test_the_page_files_are_held_to_head_as_read_at_start(self):
        """A page file changed before the start and put back after, or one that wasn't in git then, still refuses: the
        app serves the bytes it read at start, not what is on disk now."""
        ui = os.path.join(self.repo, "mac", "ui")
        page, extra = os.path.join(ui, "page.js"), os.path.join(ui, "extra.js")
        open(page, "w").write("steal()\n")
        open(extra, "w").write("more()\n")
        self.c.ui = PC.ui_snapshot(ui)  # the start
        open(page, "w").write("x()\n")  # both put back after it
        os.remove(extra)
        self.assertEqual(self.c.findings(), ["mac/ui/extra.js wasn't in git when StrixFold UI started", "mac/ui/page.js was changed when StrixFold UI started"])
        open(os.path.join(ui, ".DS_Store"), "w").write("x")
        self.c.ui = PC.ui_snapshot(ui)  # a clean start
        self.assertEqual(self.c.ui, {"page.js": b"x()\n"})
        self.assertEqual(self.c.findings(), [])

    def test_git_runs_without_the_users_config_or_hooks(self):
        env = PC._env()
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(PC.SAFE, ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=/dev/null"))
        sh(self.repo, "config", "core.fsmonitor", "/bin/false")  # a repo setting that would run a program
        self.assertEqual(self.c.findings(), [])


GUARD_CHILD = textwrap.dedent('''
    import importlib, json, os, sys
    app = sys.argv[1]
    sys.path.insert(0, app)
    sys.path.insert(0, sys.argv[2])  # mac/, for private_code itself
    import private_code as PC
    out = {}
    open(os.path.join(app, "mod_ok.py"), "w").write("X = 1\\n")
    os.makedirs(os.path.join(app, "pkg"), exist_ok=True)
    open(os.path.join(app, "pkg", "__init__.py"), "w").write("")
    open(os.path.join(app, "pkg", "sub.py"), "w").write("Y = 2\\n")
    allowed = {os.path.realpath(os.path.join(app, f)): PC._sha(os.path.join(app, f)) for f in ("mod_ok.py", "pkg/__init__.py", "pkg/sub.py")}
    sys.meta_path.insert(0, PC.Guard(allowed))
    # planted after start
    open(os.path.join(app, "array.py"), "w").write("raise SystemExit('planted array ran')\\n")
    open(os.path.join(app, "traceback.py"), "w").write("raise SystemExit('planted traceback ran')\\n")
    open(os.path.join(app, "newmod.py"), "w").write("Z = 3\\n")
    import array, traceback
    out["array"] = array.__file__ if hasattr(array, "__file__") else "builtin"
    out["array_ok"] = hasattr(array, "array")
    out["traceback"] = traceback.__file__
    import mod_ok
    out["mod_ok"] = mod_ok.X
    out["mod_ok_loader"] = type(mod_ok.__spec__.loader).__name__
    out["mod_ok_pyc"] = bool(mod_ok.__spec__.cached and os.path.exists(mod_ok.__spec__.cached))
    import pkg.sub
    out["sub"] = pkg.sub.Y
    try:
        import newmod
        out["newmod"] = "loaded"
    except ImportError as e:
        out["newmod"] = str(e)
    open(os.path.join(app, "pkg", "late.py"), "w").write("L = 1\\n")
    try:
        import pkg.late
        out["late"] = "loaded"
    except ImportError as e:
        out["late"] = str(e)
    open(os.path.join(app, "mod_two.py"), "w").write("T = 1\\n")
    sys.meta_path[0].allowed[os.path.realpath(os.path.join(app, "mod_two.py"))] = PC._sha(os.path.join(app, "mod_two.py"))
    open(os.path.join(app, "mod_two.py"), "w").write("T = 666\\n")  # changed after it was recorded
    try:
        import mod_two
        out["mod_two"] = mod_two.T
    except ImportError as e:
        out["mod_two"] = str(e)
    print(json.dumps(out))
''')


class PageCopy(unittest.TestCase):
    """halo_control serves the page (index.html, the Studio page, /ui/) from the copy read at start while Private is set
    up (start_copy), and from disk otherwise."""

    def test_after_start_the_page_comes_from_the_copy(self):
        import halo_control as HC
        self.addCleanup(setattr, PC, "UI_FILES", PC.UI_FILES)
        PC.UI_FILES = None  # before serve(): from disk
        disk = HC.ui_bytes("halo-chat.js")
        self.assertTrue(disk)
        self.assertIsNone(HC.ui_bytes("../halo_control.py"))
        self.assertIsNone(HC.ui_bytes("nope.js"))
        snap = PC.ui_snapshot(HC.UI)
        self.assertEqual(snap["halo-chat.js"], disk)
        self.assertIn("fonts/InterTight-normal.woff2", snap)
        PC.UI_FILES = dict(snap, **{"halo-chat.js": b"as read at start"})
        self.assertEqual(HC.ui_bytes("halo-chat.js", start_copy=True), b"as read at start")  # not the file on disk
        self.assertEqual(HC.ui_bytes("halo-chat.js"), disk)  # Private not set up: from disk, as before
        self.assertIsNone(HC.ui_bytes("nope.js", start_copy=True))
        PC.UI_FILES["studio-shell.html"] = snap["studio-shell.html"].replace(b"__HALO_STUDIO_TOKEN__", b"__HALO_STUDIO_TOKEN__<!-- start copy -->")
        self.assertIn("<!-- start copy -->", HC.studio_page("tok", start_copy=True))
        self.assertNotIn("<!-- start copy -->", HC.studio_page("tok"))


class ImportGuard(unittest.TestCase):
    def test_after_start_only_the_recorded_bytes_load(self):
        app = os.path.realpath(tempfile.mkdtemp(prefix="hc-guard-"))
        self.addCleanup(shutil.rmtree, app, True)
        r = subprocess.run([sys.executable, "-B", "-c", GUARD_CHILD, app, os.path.join(ROOT, "mac")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(out["array_ok"])
        self.assertNotIn(app, out["array"])          # the standard library's array, never the planted one
        self.assertNotIn(app, out["traceback"])
        self.assertEqual((out["mod_ok"], out["mod_ok_loader"], out["mod_ok_pyc"], out["sub"]), (1, "_HashedLoader", False, 2))  # no .pyc read or written
        self.assertIn("wasn't part of StrixFold UI when it started", out["newmod"])
        self.assertIn("wasn't part of StrixFold UI when it started", out["late"])
        self.assertIn("changed since StrixFold UI started", out["mod_two"])

    def test_the_manifest_covers_the_apps_files_and_nothing_from_the_user_site(self):
        import site
        repo = os.path.realpath(ROOT)
        c = PC.CodeCheck(repo, github=lambda: None)
        m = PC.manifest(repo, c)
        self.assertIn(os.path.join(repo, "mac", "studio.py"), m)
        self.assertIn(os.path.join(repo, "box", "profiles.py"), m)
        self.assertEqual(PC.USER_SITE_PACKAGES, ())  # numpy, pypdf and xlrd live in child processes only
        user = os.path.realpath(site.getusersitepackages())
        self.assertEqual([p for p in m if p.startswith(user)], [])


if __name__ == "__main__":
    unittest.main()
