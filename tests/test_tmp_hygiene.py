"""tests/_tmp.py, the one temp folder per test run: a killed run's folder is swept once its process is gone (ours only:
our prefix and marker), a live run's and anything else are left alone, and what still runs from a folder is stopped."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


class RunFolder(unittest.TestCase):
    def setUp(self):
        self.outer = tempfile.mkdtemp(prefix="sweep-")
        self.addCleanup(shutil.rmtree, self.outer, True)

    def folder(self, name, pid=None):
        d = os.path.join(self.outer, name)
        os.makedirs(d)
        if pid is not None:
            open(os.path.join(d, _tmp.MARK), "w").write(str(pid))
        return d

    def test_this_run_is_under_one_folder(self):
        self.assertEqual(tempfile.gettempdir(), _tmp.RUN)
        self.assertEqual(os.environ["TMPDIR"], _tmp.RUN)
        self.assertTrue(os.path.basename(_tmp.RUN).startswith(_tmp.PREFIX))
        self.assertTrue(os.path.exists(os.path.join(_tmp.RUN, _tmp.MARK)) or os.environ.get("HC_TEST_RUN") == _tmp.RUN)

    def test_a_killed_runs_folder_is_swept_and_nothing_else(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()  # a pid that is gone
        dead = self.folder(_tmp.PREFIX + "dead", p.pid)
        live = self.folder(_tmp.PREFIX + "live", os.getpid())
        unmarked = self.folder(_tmp.PREFIX + "unmarked")
        other = self.folder("hc-test-someone-elses", p.pid)
        _tmp.sweep(self.outer)
        self.assertEqual([os.path.exists(d) for d in (dead, live, unmarked, other)], [False, True, True, True])

    def test_what_runs_from_a_folder_is_stopped(self):
        d = self.folder(_tmp.PREFIX + "procs")
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", d])
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.assertEqual(_tmp.stop_processes(d), [])
        self.assertIsNotNone(p.wait(5))

    def sleeper(self, *argv, **env):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv], env=dict(os.environ, **env))
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait(5)))
        return p

    def test_a_process_that_names_the_folder_only_in_its_environment(self):
        # Startup's fake app carries the folder only in its environment (HC_TEST_RUN, TMPDIR)
        d = self.folder(_tmp.PREFIX + "envrun")
        by_run, by_tmp = self.sleeper(HC_TEST_RUN=d), self.sleeper(TMPDIR=os.path.join(d, "inner") + "/")
        other = self.sleeper(HC_TEST_RUN=d + "x", TMPDIR=d + "x")  # a folder whose name only starts the same
        for _ in range(50):  # until ps sees them
            if {by_run.pid, by_tmp.pid} <= set(_tmp.env_pids(d)):
                break
            time.sleep(0.1)
        self.assertEqual(_tmp.stop_processes(d), [])
        self.assertIsNotNone(by_run.wait(5))
        self.assertIsNotNone(by_tmp.wait(5))
        self.assertIsNone(other.poll())

    def test_the_folder_is_matched_as_written(self):
        d = self.folder(_tmp.PREFIX + "dot.ted+(1)")
        same = self.sleeper(d)
        look_alike = self.sleeper(os.path.join(self.outer, _tmp.PREFIX + "dotXted+(1)"))  # "." would match any character unescaped
        self.assertEqual(_tmp.stop_processes(d), [])
        self.assertIsNotNone(same.wait(5))
        self.assertIsNone(look_alike.poll())

    def test_only_a_run_folder_is_ever_stopped(self):
        # matched by its environment, $TMPDIR itself (or a folder above it) would be every one of
        # the user's processes. os.kill is a stand-in here, so even a broken guard signals nothing for real.
        home = os.path.expanduser("~")
        wrong = [_tmp.OUTER, _tmp.OUTER + "/", os.path.dirname(_tmp.OUTER), "/", "", home, self.outer,
                 os.path.join(home, _tmp.PREFIX + "nope"), "/" + _tmp.PREFIX + "nope", os.path.join(_tmp.OUTER, "..", _tmp.PREFIX + "x")]
        with mock.patch("os.kill") as kill:
            for root in wrong:
                self.assertFalse(_tmp.ours(root), root)
                self.assertEqual(_tmp._pids(root), [], root)
                self.assertIsNone(_tmp.stop_processes(root, 0.5), root)
        kill.assert_not_called()
        self.assertTrue(_tmp.ours(self.folder(_tmp.PREFIX + "mine")))
        self.assertTrue(_tmp.ours(_tmp.RUN))

    def test_a_fork_of_the_run_leaves_its_folder_alone(self):
        # a fork() child keeps the atexit hook; only the process that made the run folder ends it. A run of its
        # own in a fresh interpreter, inside this test's folder
        outer = self.folder(_tmp.PREFIX + "forks")
        code = ("import os, sys\nsys.path.insert(0, sys.argv[1])\nimport _tmp\npid = os.fork()\n"
                "if pid == 0:\n    sys.exit(0)  # the fork's own exit runs the hooks it inherited\n"
                "os.waitpid(pid, 0)\nprint(_tmp.RUN, os.path.isdir(_tmp.RUN))\n")
        env = {k: v for k, v in os.environ.items() if k != "HC_TEST_RUN"}
        env["TMPDIR"] = outer
        out = subprocess.run([sys.executable, "-c", code, os.path.dirname(os.path.abspath(__file__))], env=env,
                             capture_output=True, text=True, timeout=60).stdout.split()
        self.assertEqual(out[1:], ["True"], out)  # still there after the fork ended
        self.assertTrue(out[0].startswith(outer))
        self.assertFalse(os.path.exists(out[0]))  # and gone once the run itself ended

    def test_only_the_maker_ends_it(self):
        d = self.folder(_tmp.PREFIX + "made")
        _tmp._end(d, os.getpid() + 1)
        self.assertTrue(os.path.isdir(d))
        _tmp._end(d, os.getpid())
        self.assertFalse(os.path.exists(d))


class EveryModule(unittest.TestCase):
    def test_every_test_module_imports_it_first(self):
        # its folder must exist before anything can make one: the first statement after the docstring
        here = os.path.dirname(os.path.abspath(__file__))
        late = []
        for name in sorted(os.listdir(here)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            with open(os.path.join(here, name)) as f:
                body = ast.parse(f.read()).body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            first = body[0] if body else None
            ok = (isinstance(first, ast.Try) and isinstance(first.body[0], ast.Import)
                  and [a.name for a in first.body[0].names] == ["_tmp"])
            if not ok:
                late.append(name)
        self.assertEqual(late, [], "these must start with tests/_tmp.py's four-line import (see any test module)")


if __name__ == "__main__":
    unittest.main()
