"""The Mac's tunnels to the boxes (mac/halo_control.py Tunnel), with a stand-in ssh. They go straight to the tailnet
address (ssh -o ProxyCommand=none) rather than through ~/.ssh/config's userspace-SOCKS hop, which carried a cached
1.31 MB request in 6-8.5 s against 2.7-3.3 s direct; the ssh config's path when direct does not come up;
and a long-lived tunnel is rebuilt once nothing uses it."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import os
import socket
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


FAKE_SSH = r"""
import socket, sys, time
argv = sys.argv[1:]
spec = argv[argv.index("-L") + 1]
lport = int(spec.split(":")[1])
direct = "ProxyCommand=none" in argv
if direct and open(FLAG).read().strip() == "no-direct":
    sys.stderr.write("ssh: connect to host 100.64.0.31 port 22: No route to host\n"); sys.exit(255)
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", lport)); s.listen(4)
open(LOG, "a").write(("direct" if direct else "config") + "\n")
while True:
    time.sleep(1)
"""


class Tunnels(unittest.TestCase):
    """The Mac's tunnels (mac/halo_control.py Tunnel) with a stand-in ssh: the direct path first, the ssh config's when
    direct does not come up, and a long-lived tunnel rebuilt once nothing uses it."""

    def tunnel(self, flag, **kw):
        sys.path.insert(0, os.path.join(ROOT, "mac"))
        import halo_control as HC
        import tempfile
        tmp = tempfile.mkdtemp(prefix="hc-tun-")
        fl, lg = os.path.join(tmp, "flag"), os.path.join(tmp, "log")
        open(fl, "w").write(flag)
        script = os.path.join(tmp, "ssh.py")
        open(script, "w").write(f"FLAG = {fl!r}\nLOG = {lg!r}\n" + FAKE_SSH)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        lport = sock.getsockname()[1]
        sock.close()

        class T(HC.Tunnel):
            def argv(self, direct=False):
                return [sys.executable, script] + super().argv(direct)[2:]
        t = T("endpoint", "box1", lport, 18081, ["-o", "BatchMode=yes"], **kw)
        threading.Thread(target=t.run, daemon=True).start()
        self.addCleanup(t.stop)
        return t, lg, fl

    def wait(self, f, s=10):
        t0 = time.time()
        while time.time() - t0 < s and not f():
            time.sleep(0.05)
        return f()

    def test_direct_first_then_the_ssh_configs_path(self):
        t, lg, _ = self.tunnel("ok", direct=True)
        self.assertTrue(self.wait(lambda: t.state == "up"))
        self.assertEqual((t.via, open(lg).read().split()), ("direct", ["direct"]))
        t2, lg2, _ = self.tunnel("no-direct", direct=True)
        self.assertTrue(self.wait(lambda: t2.state == "up"))
        self.assertEqual((t2.via, open(lg2).read().split()), ("ssh config", ["config"]))
        self.assertGreater(t2.direct_failed, 0)
        self.assertFalse(t2.use_direct())  # direct again only DIRECT_RETRY_S later
        t3, lg3, _ = self.tunnel("ok", direct=False)  # tunnel_direct false: the ssh config's path only
        self.assertTrue(self.wait(lambda: t3.state == "up"))
        self.assertEqual(t3.via, "ssh config")

    def test_a_long_lived_tunnel_is_rebuilt_only_when_unused(self):
        busy = {"n": 1}
        t, lg, _ = self.tunnel("ok", direct=True, refresh_s=0.5, clients=lambda port: busy["n"])
        self.assertTrue(self.wait(lambda: t.state == "up"))
        time.sleep(6)  # past refresh_s, but in use: kept
        self.assertEqual(open(lg).read().split(), ["direct"])
        busy["n"] = 0
        self.assertTrue(self.wait(lambda: open(lg).read().split() == ["direct", "direct"], 15))  # rebuilt
        self.assertEqual(t.restarts, 0)  # a refresh is not a failure

    def test_the_tunnel_command_line(self):
        sys.path.insert(0, os.path.join(ROOT, "mac"))
        import halo_control as HC
        t = HC.Tunnel("endpoint", "box1", 18080, 18081, ["-o", "BatchMode=yes"])
        self.assertEqual(t.argv(True), ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ProxyCommand=none", "-o", "StrictHostKeyChecking=yes",
                                        "-L", "127.0.0.1:18080:127.0.0.1:18081", "box1"])  # a pinned key only
        self.assertNotIn("ProxyCommand=none", t.argv(False))
        self.assertEqual((HC.DEFAULTS["tunnel_direct"], HC.DEFAULTS["tunnel_refresh_h"]), (True, 6))

    def test_the_path_is_logged(self):
        sys.path.insert(0, os.path.join(ROOT, "mac"))
        import halo_control as HC
        lines = []
        real = HC.log
        HC.log = lines.append
        self.addCleanup(setattr, HC, "log", real)
        t, _, _ = self.tunnel("no-direct", direct=True)
        self.assertTrue(self.wait(lambda: t.state == "up"))
        self.assertTrue(any("the direct path did not come up" in ln for ln in lines), lines)
        self.assertIn("tunnel endpoint: up via ssh config", lines)
        t2, _, _ = self.tunnel("ok", direct=True)
        self.assertTrue(self.wait(lambda: t2.state == "up"))
        self.assertIn("tunnel endpoint: up via direct", lines)


if __name__ == "__main__":
    unittest.main()
