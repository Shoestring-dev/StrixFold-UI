"""fetch_child.py: one GET through the egress guard, in its own Seatbelt sandbox (profiles/fetch.sb): network out on
tcp 80/443 and the DNS resolver only, no other program, no file reads beyond Python, this package and the CA store.

stdin: {"url", "accept", "network", "max_bytes", "timeout_s", "max_redirects", "fake", "internet", "routes", "ca"}
stdout: {"ok": true, "response": {..., "body_b64"}} or {"ok": false, "kind": "denied" | "failed", "decision", "url",
"reason"}.
--probe (the tests and SandboxedGuard.check): tries to run a program, spawn one, read PROBE_FILE and connect to
PROBE_HOST:PROBE_PORT (a listener on this Mac), and says which worked; in force, none does.
"""
import base64
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.realpath(__file__))
# the package without its __init__ (which imports the whole toolset): only the guard and its decisions are needed
pkg = types.ModuleType("agents_web")
pkg.__path__ = [HERE]
sys.modules["agents_web"] = pkg
from agents_web import guard as G  # noqa: E402
from agents_web.decision import Denied, FetchFailed  # noqa: E402


def probe():
    import socket
    out = {}
    try:
        pid = os.posix_spawn("/usr/bin/true", ["/usr/bin/true"], {})
        os.waitpid(pid, 0)
        out["spawn"] = True
    except OSError:
        out["spawn"] = False
    try:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        out["fork"] = True
    except OSError:
        out["fork"] = False
    try:
        with open(os.environ["PROBE_FILE"]) as f:
            f.read()
        out["read"] = True
    except OSError:
        out["read"] = False
    try:
        s = socket.create_connection((os.environ["PROBE_HOST"], int(os.environ["PROBE_PORT"])), timeout=2)
        s.close()
        out["connect"] = True
    except OSError:
        out["connect"] = False
    try:
        os.listdir(os.environ["PROBE_HOME"])  # the user's home (the parent passes it): not even a listing
        out["list_home"] = True
    except OSError:
        out["list_home"] = False
    sys.stdout.write(json.dumps(out))


def main():
    if "--probe" in sys.argv:
        return probe()
    req = json.loads(sys.stdin.buffer.read() or b"{}")
    g = G.EgressGuard(fake=bool(req.get("fake")), internet=req.get("internet"), network=req.get("network"),
                      max_bytes=int(req.get("max_bytes") or G.MAX_BYTES), timeout_s=float(req.get("timeout_s") or G.TIMEOUT_S),
                      max_redirects=int(req.get("max_redirects") or G.MAX_REDIRECTS))
    for host, port in (req.get("routes") or {}).items():
        g.add_test_route(host, port)
    if req.get("ca"):
        g.add_test_ca(req["ca"])
    try:
        r = g.fetch(req["url"], accept=req.get("accept") or G.ACCEPT)
    except Denied as e:
        out = {"ok": False, "kind": "denied", "decision": e.decision.to_dict(), "url": e.url, "reason": str(e)}
    except FetchFailed as e:
        out = {"ok": False, "kind": "failed", "reason": e.reason, "url": e.url}
    else:
        out = {"ok": True, "response": {"url": r.url, "requested": r.requested, "status": r.status, "headers": r.headers,
                                        "content_type": r.content_type, "charset": r.charset, "hops": r.hops, "ip": r.ip,
                                        "elapsed_s": r.elapsed_s, "body_b64": base64.b64encode(r.body).decode()}}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
