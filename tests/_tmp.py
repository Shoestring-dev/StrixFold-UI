"""One temp folder per test run (every test module imports this first, before anything can make a folder).

Every tempfile folder the tests make goes under it (tempfile.tempdir), and so does every folder the processes they start
make (TMPDIR in the environment). When the run ends it stops any process still running from that folder (a fake halod's
rank or lock holder, fake_serve.py) and removes it. Before this, ~20,000 folders (28 GB) piled up in $TMPDIR in two days.

A run that was killed can't clean up: the next run removes its folder once the process that made it is gone (our prefix,
our marker file, a dead pid). Nothing else in $TMPDIR is touched. A test process a test starts keeps its parent's folder
(HC_TEST_RUN). A full suite in an empty TMPDIR leaves it empty and no process behind.
"""
import atexit
import os
import shutil
import signal
import subprocess
import tempfile
import time

PREFIX = "hct-"
MARK = ".hc-test-run"  # the pid of the run that made the folder


# where run folders are made: $TMPDIR as this process started (set below). Kept on the tempfile module, which is this
# process's own: a second import of this file in the same process (as "_tmp" and as "tests._tmp", when a test puts
# tests/ on sys.path) keeps the same OUTER instead of reading the run folder as its outer one; a child process (a new
# interpreter) still gets OUTER = its run folder, so it can never stop its parent's run
OUTER = getattr(tempfile, "_hc_test_outer", None) or os.path.realpath(tempfile.gettempdir())
tempfile._hc_test_outer = OUTER
ENV_KEYS = ("HC_TEST_RUN", "TMPDIR")  # what a process a test started carries in its environment (as this module sets them)


def _ere(s):
    """s as a literal in pgrep's (extended) regular expression."""
    return "".join("\\" + c if c in ".[]()*+?{}|^$\\" else c for c in s)


def ours(root):
    """Only a run folder may have its processes stopped (matched by its environment, $TMPDIR itself or a folder
    above it would be every one of the user's processes): a folder named with our prefix, inside OUTER."""
    real = os.path.realpath(str(root or "/").rstrip("/") or "/")
    return os.path.basename(real).startswith(PREFIX) and real.startswith(OUTER.rstrip("/") + "/")


def env_pids(root):
    """Our processes whose environment's HC_TEST_RUN or TMPDIR is `root` or a folder inside it (a process a test started
    may name its folder only there, as Startup's fake app does). `ps -E` shows a process's environment as it started."""
    try:
        out = subprocess.run(["ps", "-E", "-ww", "-x", "-o", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    root, pids = root.rstrip("/"), []
    for line in out.splitlines():
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        for t in parts[1:]:
            k, eq, v = t.partition("=")
            v = v.rstrip("/")
            if eq and k in ENV_KEYS and (v == root or v.startswith(root + "/")):
                pids.append(int(parts[0]))
                break
    return pids


def _pids(root):
    """Processes whose command line names `root`, or whose environment points into it (never this one); none for a folder
    that isn't a run folder (ours)."""
    if not ours(root):
        return []
    try:
        out = subprocess.run(["pgrep", "-f", _ere(root)], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    pids = {int(x) for x in out.split() if x.isdigit()} | set(env_pids(root))
    return sorted(p for p in pids if p != os.getpid())


def stop_processes(root, wait_s=5.0):
    """Stop what still runs from `root`, again until nothing is left (a daemon thread may start one more), at most
    wait_s seconds. What is left, if anything; None, and nothing signalled, for a folder that isn't a run folder (ours)."""
    if not ours(root):
        return None
    end = time.time() + wait_s
    while True:
        pids = _pids(root)
        if not pids or time.time() > end:
            return pids
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.2)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # another user's: alive


def sweep(outer):
    """Folders of earlier runs that were killed before they could clean up: ours only, and only once their run is gone."""
    try:
        names = os.listdir(outer)
    except OSError:
        return
    for name in names:
        d = os.path.join(outer, name)
        if not name.startswith(PREFIX):
            continue
        try:
            with open(os.path.join(d, MARK)) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if not _alive(pid):
            stop_processes(d, 2.0)
            shutil.rmtree(d, ignore_errors=True)


def _end(root, owner):
    if os.getpid() != owner:  # a fork() of the run keeps the hook: only the process that made the folder ends it
        return
    stop_processes(root)
    shutil.rmtree(root, ignore_errors=True)


RUN = os.environ.get("HC_TEST_RUN")
if not (RUN and os.path.isdir(RUN)):
    outer = tempfile.gettempdir()
    sweep(outer)
    RUN = tempfile.mkdtemp(prefix=PREFIX, dir=outer)
    with open(os.path.join(RUN, MARK), "w") as f:
        f.write(str(os.getpid()))
    os.environ["HC_TEST_RUN"] = RUN
    atexit.register(_end, RUN, os.getpid())
os.environ["TMPDIR"] = RUN
tempfile.tempdir = RUN
