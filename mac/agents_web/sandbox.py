"""Seatbelt (sandbox-exec) for the tools' child processes: the parser and SearXNG.

Profiles live in profiles/*.sb, deny by default. A profile is rendered with its @ANCESTORS@ line (stat-only access to
the folders above the paths it reads, which Python's path handling needs) and run with `sandbox-exec -p`. Every path
handed to a profile is resolved first (Seatbelt matches real paths: /var is /private/var). Nothing here runs a child
without the sandbox; if sandbox-exec is missing the callers refuse.
"""
import ctypes
import os
import subprocess
import threading
import time

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PROFILES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")


class Unavailable(Exception):
    pass


def available():
    """(True, "") when sandbox-exec can run here, else (False, why)."""
    if not os.path.isfile(SANDBOX_EXEC) or not os.access(SANDBOX_EXEC, os.X_OK):
        return False, "sandbox-exec is missing on this Mac."
    return True, ""


def venv_runtime(venv):
    """(the real Python binary, the Python runtime folder) behind a venv, from its pyvenv.cfg."""
    cfg = os.path.join(venv, "pyvenv.cfg")
    try:
        with open(cfg) as f:
            home = next(ln.split("=", 1)[1].strip() for ln in f if ln.split("=", 1)[0].strip() == "home")
    except (OSError, StopIteration):
        raise Unavailable("The venv at %s is missing or broken." % venv)
    py = os.path.realpath(os.path.join(venv, "bin", "python"))
    runtime = os.path.realpath(os.path.dirname(os.path.realpath(home)))
    if not os.path.isfile(py) or not py.startswith(runtime + os.sep):
        raise Unavailable("The venv at %s does not point at its Python runtime." % venv)
    return py, runtime


def _quoted(path):
    if any(c in path for c in '"\\\n\r') or not path.startswith("/"):
        raise Unavailable("A sandbox path has characters the profile cannot hold.")
    return '"%s"' % path


def render(name, paths):
    """The profile text of profiles/<name> with stat-only access to every folder above `paths`."""
    with open(os.path.join(PROFILES, name)) as f:
        text = f.read()
    above = set()
    for p in paths:
        d = os.path.dirname(os.path.realpath(p))
        while d and d != "/":
            above.add(d)
            d = os.path.dirname(d)
    return text.replace("@ANCESTORS@", " ".join("(literal %s)" % _quoted(a) for a in sorted(above)))


def command(profile_text, params, argv):
    cmd = [SANDBOX_EXEC, "-p", profile_text]
    for k, v in params.items():
        cmd += ["-D", "%s=%s" % (k, v)]
    return cmd + list(argv)


class _TaskInfo(ctypes.Structure):
    _fields_ = [("virtual", ctypes.c_uint64), ("resident", ctypes.c_uint64), ("user", ctypes.c_uint64),
                ("system", ctypes.c_uint64), ("threads_user", ctypes.c_uint64), ("threads_system", ctypes.c_uint64),
                ("policy", ctypes.c_int32), ("faults", ctypes.c_int32), ("pageins", ctypes.c_int32),
                ("cow_faults", ctypes.c_int32), ("msgs_sent", ctypes.c_int32), ("msgs_received", ctypes.c_int32),
                ("syscalls_mach", ctypes.c_int32), ("syscalls_unix", ctypes.c_int32), ("csw", ctypes.c_int32),
                ("threadnum", ctypes.c_int32), ("numrunning", ctypes.c_int32), ("priority", ctypes.c_int32)]


_libproc = None


def resident_bytes(pid):
    """A process's resident memory (proc_pidinfo PROC_PIDTASKINFO), or 0 if it cannot be read."""
    global _libproc
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        ti = _TaskInfo()
        n = _libproc.proc_pidinfo(int(pid), 4, ctypes.c_uint64(0), ctypes.byref(ti), ctypes.sizeof(ti))
        return int(ti.resident) if n == ctypes.sizeof(ti) else 0
    except OSError:
        return 0


def run(cmd, *, stdin=b"", timeout_s=20.0, mem_bytes=768 << 20, max_out=8 << 20, env=None, cwd=None):
    """Run cmd; kill it past timeout_s, past mem_bytes resident or past max_out bytes of output.
    Returns (returncode, stdout bytes, stderr text, killed: None | "time" | "memory" | "output")."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
                         start_new_session=True)
    killed = []
    out, err = bytearray(), bytearray()

    def feed():
        try:
            p.stdin.write(stdin)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass

    def drain(stream, buf, cap):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            if len(buf) + len(chunk) > cap:
                if not killed:
                    killed.append("output")
                p.kill()
                return
            buf.extend(chunk)

    threads = [threading.Thread(target=feed, daemon=True),
               threading.Thread(target=drain, args=(p.stdout, out, max_out), daemon=True),
               threading.Thread(target=drain, args=(p.stderr, err, 65536), daemon=True)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout_s
    while p.poll() is None:
        if time.monotonic() > deadline:
            killed.append("time")
            p.kill()
            break
        if mem_bytes and resident_bytes(p.pid) > mem_bytes:
            killed.append("memory")
            p.kill()
            break
        time.sleep(0.05)
    p.wait()
    for t in threads:
        t.join(2)
    for s in (p.stdout, p.stderr):
        s.close()
    return p.returncode, bytes(out), err.decode("utf-8", "replace"), (killed[0] if killed else None)
