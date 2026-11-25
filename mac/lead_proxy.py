"""The network filter for a lead's commands in Run anything.
The commands' sandbox (lead_tools.profile) reaches one thing: this proxy's port on 127.0.0.1. The proxy
runs in the app's process, one per lead turn, and takes HTTP CONNECT (HTTPS, and anything else tunnelled) and plain
http:// forward requests. For each one it:

  - allows ports 80 and 443 only, unless the project's rules name others (lead_rules net_ports);
  - checks the name with agents_web's egress guard rules (guard.check_host): no IP literals in any form, no
    single-label names, nothing under .ts.net or .local;
  - resolves the name itself and checks every address it gets (guard.EgressGuard._addresses, guard.check_ip):
    loopback, RFC 1918, the tailnet's 100.64.0.0/10, link-local, multicast, ::1, fc00::/7, the IPv6 transition
    ranges (so a ::ffff:-mapped private address too) are refused, and so are this Mac's own addresses;
  - connects to the exact address it checked, so a second lookup can't swap it.

Each refusal is kept for the column ("the filter refused 192.168.1.1:443: ..."). Under --fake it reaches nothing; only
the tests give a proxy a --fake guard with test routes (names under .halo-test.invalid)."""
import ipaddress
import re
import socket
import socketserver
import subprocess
import threading
import time

PORTS = (80, 443)
CONNECT_S = 20.0   # the lookup and the connect
IDLE_S = 120.0     # a tunnel with nothing moving either way for this long is closed
MAX_CONNS = 32     # connections at once through one turn's proxy
HEAD_MAX = 16 << 10
CONNECT_RE = re.compile(r"CONNECT (?:\[([0-9A-Fa-f:.%]+)\]|([^\s:/\[\]]+)):(\d{1,5}) HTTP/1\.[01]$")
FORWARD_RE = re.compile(r"(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) http://(?:\[([0-9A-Fa-f:.%]+)\]|([^\s:/\[\]?#]+))(?::(\d{1,5}))?(/\S*)? HTTP/1\.[01]$")
WORDS = {400: "Bad Request", 403: "Forbidden", 431: "Request Header Fields Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}


OWN_TTL_S = 5  # this Mac's own addresses are read again when older than this


def own_addresses():
    """This Mac's interface addresses (ifconfig), so one of its own public ones (an IPv6 global address) is refused too."""
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    addrs = set()
    for m in re.finditer(r"^\s+inet6?\s+([0-9A-Fa-f:.%]+)", out, re.M):
        try:
            addrs.add(ipaddress.ip_address(m.group(1).split("%", 1)[0]))
        except ValueError:
            pass
    return addrs


def guard_for(fake):
    """The egress guard the proxy checks with (agents_web.guard.live_guard: never any test routes): the live app's
    reaches the internet; the --fake app's reaches nothing."""
    from agents_web.guard import live_guard
    return live_guard(internet=not fake)


def words(reason):
    """A guard's reason as part of the column's line: "The site resolves to ..." -> "the site resolves to ..."."""
    r = str(reason or "refused").strip().rstrip(".")
    return r[:1].lower() + r[1:]


class Refusal(Exception):
    def __init__(self, status, why):
        super().__init__(why)
        self.status, self.why = status, why


class LeadProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, guard, ports=PORTS, on_refused=None, own=None):
        self.guard, self.ports, self.on_refused = guard, tuple(sorted({int(p) for p in ports})), on_refused
        self._own_fixed = own is not None  # tests pass their own; else re-read, at most every OWN_TTL_S (an interface that comes up mid-turn)
        self.own, self._own_at = (set(own) if own is not None else own_addresses()), time.monotonic()
        self.slots = threading.BoundedSemaphore(MAX_CONNS)
        self.mu = threading.Lock()
        self.socks, self.refused = set(), []
        self.connect = socket.create_connection  # (address, timeout): the one place the proxy connects out
        super().__init__(("127.0.0.1", 0), _Handler)
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, name="lead-proxy", daemon=True).start()

    def target(self, host, port):
        """[(ip, port)] checked for host:port, or Refusal saying why not."""
        from agents_web import guard as G
        from agents_web.decision import Denied, FetchFailed
        if port not in self.ports:
            raise Refusal(403, f"port {port} isn't allowed (only {', '.join(map(str, self.ports))})")
        h = str(host or "").lower().rstrip(".")
        try:
            h = h.encode("idna").decode("ascii") if h and ":" not in h else h
        except UnicodeError:
            raise Refusal(403, "the host name is not valid")
        d = G.check_host(h)
        if d.kind != "allow":
            raise Refusal(403, words(d.reason))
        if not G.host_in(h, self.guard.network):
            raise Refusal(403, "the site is outside this agent's network list")
        try:
            addrs = self.guard._addresses(f"https://{h}/", h, port, time.monotonic() + CONNECT_S)
        except Denied as e:
            raise Refusal(403, words(e.decision.reason))
        except FetchFailed as e:
            raise Refusal(502, words(e.reason))
        if h not in self.guard.test_routes:
            if not self._own_fixed and time.monotonic() - self._own_at > OWN_TTL_S:
                self.own, self._own_at = own_addresses(), time.monotonic()
            for ip, _ in addrs:
                if ipaddress.ip_address(ip.split("%", 1)[0]) in self.own:
                    raise Refusal(403, "it resolves to an address of this Mac")
        return addrs

    def refuse(self, host, port, why):
        with self.mu:
            self.refused = (self.refused + [(str(host)[:120], port, why)])[-50:]
        if self.on_refused:
            try:
                self.on_refused(host, port, why)
            except Exception:  # noqa: BLE001
                pass

    def take(self):
        """The refusals since the last take (the column's line for one command)."""
        with self.mu:
            out, self.refused = self.refused, []
        return out

    def track(self, *socks):
        with self.mu:
            self.socks.update(socks)

    def untrack(self, *socks):
        with self.mu:
            self.socks.difference_update(socks)

    def close(self):
        """The turn ended: no new connections, and every tunnel still open is cut."""
        self.shutdown()
        self.server_close()
        with self.mu:
            socks, self.socks = list(self.socks), set()
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


class _Handler(socketserver.StreamRequestHandler):
    rbufsize = 0  # unbuffered: nothing the client sends after the head is held back from the tunnel
    timeout = 30  # reading the request's head

    def say(self, status, text):
        body = (f"StrixFold UI's network filter: {text}.\r\n").encode("utf-8", "replace")
        try:
            self.wfile.write(f"HTTP/1.1 {status} {WORDS.get(status, 'Error')}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                             f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            self.wfile.flush()
        except OSError:
            pass

    def handle(self):
        srv = self.server
        if not srv.slots.acquire(blocking=False):
            return self.say(503, "too many connections at once")
        try:
            self.go(srv)
        finally:
            srv.slots.release()

    def go(self, srv):
        try:
            line = self.rfile.readline(4096).decode("latin-1", "replace").rstrip("\r\n")
            head, n = [], len(line)
            while True:
                h = self.rfile.readline(8192)
                if not h or h in (b"\r\n", b"\n"):
                    break
                n += len(h)
                if n > HEAD_MAX or len(head) >= 100:
                    return self.say(431, "the request's headers are too large")
                head.append(h)
        except OSError:
            return
        m = CONNECT_RE.match(line)
        if m:
            method, host, port, path = "CONNECT", m.group(1) or m.group(2), int(m.group(3)), None
        else:
            m = FORWARD_RE.match(line)
            if not m:
                return self.say(400, "only CONNECT host:port, or a plain http:// request through it")
            method, host, port, path = m.group(1), m.group(2) or m.group(3), int(m.group(4) or 80), m.group(5) or "/"
        try:
            addrs = srv.target(host, port)
        except Refusal as e:
            srv.refuse(host, port, e.why)
            return self.say(e.status, f"refused {host}:{port}: {e.why}")
        up = None
        for ip, p in addrs:  # the exact addresses checked above, never the name again
            try:
                up = srv.connect((ip, p), CONNECT_S)
                break
            except OSError:
                continue
        if up is None:
            return self.say(502, f"{host}:{port} did not answer")
        srv.track(up, self.connection)
        try:
            up.settimeout(IDLE_S)
            self.connection.settimeout(IDLE_S)
            if method == "CONNECT":
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
            else:  # origin-form to the site; the proxy's own headers stay here; one request per connection
                keep = [h for h in head if not h.lower().startswith((b"proxy-", b"connection:", b"keep-alive:"))]
                up.sendall(f"{method} {path} HTTP/1.1\r\n".encode("latin-1") + b"".join(keep) + b"Connection: close\r\n\r\n")
            pipe(self.connection, up)
        finally:
            srv.untrack(up, self.connection)
            try:
                up.close()
            except OSError:
                pass


def pipe(a, b):
    def one(x, y):
        try:
            while True:
                d = x.recv(65536)
                if not d:
                    break
                y.sendall(d)
        except OSError:
            pass
        finally:
            for z in (x, y):
                try:
                    z.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
    t = threading.Thread(target=one, args=(b, a), daemon=True)
    t.start()
    one(a, b)
    t.join(5)
