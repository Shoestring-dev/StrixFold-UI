"""The egress guard: every web request an agent's tools make goes through here (spec v4 section 3).

- http and https only, ports 80 and 443 only, no IP literals (dotted, integer, octal or hex forms), no user:password@.
- The guard resolves DNS itself, checks every A and AAAA answer against the fixed deny list below, and connects to the
  checked IP. TLS is verified against the hostname (SNI and Host), so DNS rebinding cannot move the connection.
- Redirects are followed by hand, at most 5; every hop is re-resolved and re-checked.
- 10 MB cap on the body, counted after decompression too (a compression bomb stops at the cap); 20 s for the whole
  fetch, all hops, enforced by a watchdog that shuts the socket.
- No proxies or CA overrides from the environment: http.client never reads HTTP(S)_PROXY, and the CA file is the
  system one by path (SSL_CERT_FILE is ignored). No cookies, no credentials, GET only.
- Tracking parameters and the fragment are removed before the URL guard runs (clean_url).

Deny list (always on, not editable per agent):
  IPv4 0/8, 10/8, 100.64/10 (tailnet), 127/8, 169.254/16, 172.16/12, 192.168/16, 224/4, 240/4, 192.0.0/24 (IETF
  protocol assignments), 198.18/15 (benchmarking; fake-IP VPN and proxy tools use it), 192.88.99/24 (6to4 relay);
  IPv6 ::1, ::, fc00::/7 (the tailnet's fd7a:115c:a1e0::/48 is inside it), fe80::/10, fec0::/10 (old site-local),
  ff00::/8, and every transition range, denied outright rather than unpacked, since a network's own translation can
  map them anywhere (egress research): ::ffff:0:0/96 (mapped), 64:ff9b::/96 and 64:ff9b:1::/48 (NAT64),
  2002::/16 (6to4), 2001::/32 (Teredo), 100::/64 (discard), and ::/96 (IPv4-compatible) and ::ffff:0:0:0/96
  (IPv4-translated);
  names *.ts.net, *.local and any single-label name (box1, box2, localhost).
The lists are explicit networks, never ipaddress's is_private / is_global (100.64/10 is neither, and their meaning
changed in Python 3.13); tests check every class on the Python the app runs (/usr/bin/python3, 3.9).

The only way past the deny list is a test route, which exists only on a guard built with fake=True (halo_control
--fake) and only for names under .halo-test.invalid, a reserved name that never resolves.
"""
import http.client
import ipaddress
import os
import re
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from .decision import ALLOW, Deny, Denied, FetchFailed

MAX_BYTES = 10 * 1024 * 1024
TIMEOUT_S = 20.0
MAX_REDIRECTS = 5
PORTS = (80, 443)
SCHEMES = ("http", "https")
MAX_URL = 4096
TEST_SUFFIX = ".halo-test.invalid"
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/18.5 Safari/605.1.15")
REDIRECTS = (301, 302, 303, 307, 308)

DENY_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
    "224.0.0.0/4", "240.0.0.0/4",
    "192.0.0.0/24", "198.18.0.0/15", "192.88.99.0/24")]  # IANA special-purpose (198.18/15 is fake-IP VPNs')
DENY_V6 = [ipaddress.ip_network(n) for n in (
    "::1/128", "::/128", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8")]
TRANSITION_V6 = [ipaddress.ip_network(n) for n in (
    "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "100::/64", "::/96",
    "::ffff:0:0:0/96")]

TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_", "oly_", "_hs")
TRACKING = {"fbclid", "gclid", "gclsrc", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid", "ttclid",
            "li_fat_id", "igshid", "igsh", "mkt_tok", "ref", "ref_src", "ref_url", "spm", "_ga", "_gl", "vero_id",
            "s_kwcid", "ncid", "cmpid", "si"}
LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
NUMERIC_LABEL = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)$")
NETLOC = re.compile(r"[^:/?#\s]+(?::[0-9]{1,5})?$")
SAFE_PATH = "/%:@!$&'()*+,;=-._~"
SAFE_QUERY = SAFE_PATH + "?"


def _deny(rule, reason):
    return Deny(rule, reason)


def _v4_denied(v4):
    return any(v4 in n for n in DENY_V4)


def check_ip(ip):
    """Allow, or Deny(address) for an address on the deny list. IPv6 transition ranges are denied outright."""
    try:
        a = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return _deny("address", "The address could not be read.")
    if a.version == 4:
        if _v4_denied(a):
            return _deny("address", "The site resolves to a private or reserved address.")
    elif any(a in n for n in DENY_V6):
        return _deny("address", "The site resolves to a private or reserved address.")
    elif any(a in n for n in TRANSITION_V6):
        return _deny("address", "The site resolves to an IPv6 transition address (NAT64, 6to4, Teredo, mapped).")
    return ALLOW


def _is_ip_literal(host):
    """True for any host the resolver would read as an address: dotted or bracketed IPs, and the integer, octal, hex
    and short forms inet_aton accepts (127.1, 0x7f.1, 2130706433). A numeric last label counts too, as browsers do."""
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except (OSError, ValueError):
        pass
    return bool(NUMERIC_LABEL.match(host.rsplit(".", 1)[-1]))


def check_host(host):
    """Allow or Deny for a host name already lower-cased and IDNA-encoded, without a trailing dot."""
    if not host:
        return _deny("bad_url", "The address has no host name.")
    if ":" in host or _is_ip_literal(host):
        return _deny("ip_literal", "The address is an IP address, not a host name.")
    labels = host.split(".")
    if len(host) > 253 or not all(LABEL.match(lb) for lb in labels):
        return _deny("name", "The host name is not valid.")
    if len(labels) < 2:
        return _deny("name", "Single-label names (local machines) are not allowed.")
    if host == "ts.net" or host.endswith(".ts.net") or host == "local" or host.endswith(".local"):
        return _deny("name", "Tailnet and local network names are not allowed.")
    return ALLOW


def _matches(host, pattern):
    p = str(pattern).lower().rstrip(".")
    if p == "*":
        return True
    p = p[2:] if p.startswith("*.") else p
    return host == p or host.endswith("." + p)


def host_in(host, network):
    """Whether host passes an agent's own lists: not under any "deny" pattern, and under an "allow" one."""
    if any(_matches(host, p) for p in network.get("deny") or []):
        return False
    return any(_matches(host, p) for p in network.get("allow") or ["*"])


def _is_tracking(segment):
    key = unquote(segment.split("=", 1)[0].replace("+", " ")).strip().lower()
    return key in TRACKING or key.startswith(TRACKING_PREFIXES)


def _split(url):
    """(scheme, host, port, path, query) of a URL, or raise Denied. The host is IDNA-encoded and lower-cased."""
    if not isinstance(url, str) or not url.strip():
        raise Denied(_deny("bad_url", "The address is empty."), "")
    url = url.strip()
    if len(url) > MAX_URL:
        raise Denied(_deny("too_long", "The address is too long."), url[:200])
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url) or "\\" in url:
        raise Denied(_deny("bad_url", "The address has control characters or a backslash."), url)
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in SCHEMES:
            raise Denied(_deny("scheme", "Only http and https addresses are allowed."), url)
        if "@" in parts.netloc:
            raise Denied(_deny("userinfo", "Addresses with a user name or password are not allowed."), url)
        if "[" in parts.netloc or "]" in parts.netloc:
            raise Denied(_deny("ip_literal", "The address is an IP address, not a host name."), url)
        if not NETLOC.match(parts.netloc):  # host[:port] and nothing else (urlsplit is lenient about the rest)
            raise Denied(_deny("bad_url", "The address could not be read."), url)
        port = parts.port
        raw = (parts.hostname or "").rstrip(".")
        host = raw.encode("idna").decode("ascii").lower().rstrip(".") if raw else ""
    except Denied:
        raise
    except (ValueError, UnicodeError):
        raise Denied(_deny("bad_url", "The address could not be read."), url)
    if not host:  # never rebuilt into a URL whose path would read as a host
        raise Denied(_deny("bad_url", "The address has no host name."), url)
    if port is not None and port not in PORTS:
        raise Denied(_deny("port", "Only ports 80 and 443 are allowed."), url)
    path = quote(parts.path or "/", safe=SAFE_PATH)
    query = quote(parts.query, safe=SAFE_QUERY)
    return scheme, host, port, path, query


def _join(scheme, host, port, path, query):
    netloc = host if port is None or port == {"http": 80, "https": 443}[scheme] else f"{host}:{port}"
    return urlunsplit((scheme, netloc, path, query, ""))


def clean_url(url):
    """The URL with tracking parameters and the fragment removed, the host IDNA-encoded and lower-cased and a default
    port dropped. Raises Denied for a URL that cannot be read or uses another scheme or port."""
    scheme, host, port, path, query = _split(url)
    query = "&".join(s for s in query.split("&") if s and not _is_tracking(s))
    return _join(scheme, host, port, path, query)


def check_static(url):
    """The rules that need no DNS: scheme, port, user:password@, IP literals and names. Allow or Deny."""
    try:
        _, host, _, _, _ = _split(url)
    except Denied as e:
        return e.decision
    return check_host(host)


@dataclass
class Response:
    url: str
    requested: str
    status: int
    headers: dict
    content_type: str
    charset: str
    body: bytes
    hops: list = field(default_factory=list)
    ip: str = ""
    elapsed_s: float = 0.0


class _Decoder:
    """Content-Encoding gzip or deflate, decompressed with a cap on the output."""

    def __init__(self, encoding):
        enc = (encoding or "identity").strip().lower()
        if enc in ("", "identity"):
            self.d = None
        elif enc in ("gzip", "x-gzip"):
            self.d, self.wbits = zlib.decompressobj(16 + zlib.MAX_WBITS), 16 + zlib.MAX_WBITS
        elif enc == "deflate":
            self.d, self.wbits = zlib.decompressobj(zlib.MAX_WBITS), zlib.MAX_WBITS
        else:
            raise FetchFailed("The page uses a compression this tool does not read.")
        self.started = False

    def feed(self, data, room):
        """Decompressed bytes of `data`; FetchFailed as soon as they pass `room`."""
        if self.d is None:
            if len(data) > room:
                raise FetchFailed("The page is over the 10 MB limit.")
            return data
        out = bytearray()
        buf = data
        while buf:
            try:
                part = self.d.decompress(buf, room + 1 - len(out))
            except zlib.error:
                if self.wbits == zlib.MAX_WBITS and not self.started:  # deflate sent raw, without the zlib header
                    self.d, self.wbits = zlib.decompressobj(-zlib.MAX_WBITS), -zlib.MAX_WBITS
                    continue
                raise FetchFailed("The page's compressed data is damaged.")
            self.started = True
            out += part
            if len(out) > room:
                raise FetchFailed("The page is over the 10 MB limit once decompressed.")
            buf = self.d.unconsumed_tail
            if not buf and self.d.eof and self.d.unused_data:  # another gzip member follows
                buf = self.d.unused_data
                self.d = zlib.decompressobj(self.wbits)
        return bytes(out)

    def finish(self):
        if self.d is not None and self.started and not self.d.eof:
            raise FetchFailed("The page's compressed data ended early.")


class _Conn(http.client.HTTPConnection):
    """An HTTP connection over a socket the guard already opened to a checked address; it never connects by itself."""

    def __init__(self, sock, timeout):
        super().__init__("guard.invalid", timeout=timeout)
        self.sock = sock

    def connect(self):
        raise FetchFailed("The connection closed.")


def _ca_file():
    p = ssl.get_default_verify_paths()
    return p.openssl_cafile, p.openssl_capath


class EgressGuard:
    """fake=True (halo_control --fake) allows test routes and, unless internet=True, reaches nothing else."""

    def __init__(self, *, fake=False, internet=None, network=None, max_bytes=MAX_BYTES, timeout_s=TIMEOUT_S,
                 max_redirects=MAX_REDIRECTS):
        """network: an agent definition's own lists, {"allow": [...], "deny": [...]} of "*", "example.com" or
        "*.example.com", checked on every hop before DNS; the fixed deny list always applies on top."""
        self.fake = bool(fake)
        net = network or {}
        self.network = {"allow": list(net.get("allow") or ["*"]), "deny": list(net.get("deny") or [])}
        self.internet = (not self.fake) if internet is None else bool(internet)
        self.max_bytes, self.timeout_s, self.max_redirects = int(max_bytes), float(timeout_s), int(max_redirects)
        self._routes = {}
        self._extra_ca = None
        self._ctx = None

    # ---------------------------------------------------------------- test routes (--fake only)
    def add_test_route(self, host, port):
        """Send http(s)://<host>/ to 127.0.0.1:<port>. Only on a guard built with fake=True, and only for names under
        .halo-test.invalid; the live app's guard has none."""
        if not self.fake:
            raise RuntimeError("test routes exist only on a --fake guard")
        host = str(host).lower()
        if not host.endswith(TEST_SUFFIX) or check_host(host).kind != "allow" or not 0 < int(port) < 65536:
            raise ValueError("a test route needs a name under .halo-test.invalid and a port")
        self._routes[host] = int(port)

    def add_test_ca(self, cafile):
        """Trust one more CA (a test CA for TLS test routes). --fake only."""
        if not self.fake:
            raise RuntimeError("test CAs exist only on a --fake guard")
        self._extra_ca, self._ctx = cafile, None

    @property
    def test_routes(self):
        return dict(self._routes)

    # ---------------------------------------------------------------- pieces
    def _context(self):
        if self._ctx is None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # check_hostname on, CERT_REQUIRED
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            cafile, capath = _ca_file()
            if not ((cafile and os.path.exists(cafile)) or (capath and os.path.isdir(capath))):
                raise FetchFailed("This Mac's certificate store was not found.")
            ctx.load_verify_locations(cafile=cafile if cafile and os.path.exists(cafile) else None,
                                      capath=capath if capath and os.path.isdir(capath) else None)
            if self._extra_ca:
                ctx.load_verify_locations(cafile=self._extra_ca)
            self._ctx = ctx
        return self._ctx

    def _resolve(self, host, port, deadline):
        """Every address the system resolver gives for host (A and AAAA), with a deadline."""
        box = {}

        def run():
            try:
                box["v"] = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            except (OSError, UnicodeError) as e:
                box["e"] = e

        t = threading.Thread(target=run, daemon=True, name="guard-dns")
        t.start()
        t.join(max(0.05, deadline - time.monotonic()))
        if t.is_alive():
            raise FetchFailed("The DNS lookup timed out.")
        if "e" in box:
            raise FetchFailed("The host name did not resolve.")
        out = []
        for fam, _, _, _, sa in box["v"]:
            if fam in (socket.AF_INET, socket.AF_INET6) and sa[0] not in out:
                out.append(sa[0])
        if not out:
            raise FetchFailed("The host name did not resolve.")
        return out

    def _addresses(self, url, host, port, deadline):
        """[(ip, port)] to try for host: a test route (fake only), or every resolved address, all checked."""
        if host in self._routes:
            return [("127.0.0.1", self._routes[host])]
        if not self.internet:
            raise Denied(_deny("name", "Only test pages are reachable under --fake."), url)
        ips = self._resolve(host, port, deadline)
        for ip in ips:
            d = check_ip(ip)
            if d.kind != "allow":
                raise Denied(d, url)
        return [(ip, port) for ip in ips]

    def _open(self, addrs, scheme, host, deadline):
        """A socket to the first checked address that answers, wrapped in TLS verified against host for https."""
        ctx = self._context() if scheme == "https" else None
        failed = None
        for ip, port in addrs:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                sock = socket.create_connection((ip, port), timeout=min(left, 10.0))
            except OSError as e:
                failed = e
                continue
            if scheme == "http":
                return sock, ip
            try:
                sock.settimeout(max(0.05, deadline - time.monotonic()))
                return ctx.wrap_socket(sock, server_hostname=host), ip
            except ssl.SSLCertVerificationError:
                sock.close()
                raise FetchFailed("The site's certificate did not verify for its name.")
            except ssl.SSLError:
                sock.close()
                raise FetchFailed("The secure connection failed.")
            except OSError as e:
                sock.close()
                failed = e
        if failed is None or isinstance(failed, socket.timeout):
            raise FetchFailed("The site did not answer.")
        raise FetchFailed("The connection to the site failed.")

    def _one(self, url, deadline, accept):
        """One GET of one hop: (status, headers, body or None for a redirect, ip, location)."""
        scheme, host, port, path, query = _split(url)
        d = check_host(host)
        if d.kind != "allow":
            raise Denied(d, url)
        if not host_in(host, self.network):
            raise Denied(_deny("not_allowed", "The site is outside this agent's network list."), url)
        port = port or {"http": 80, "https": 443}[scheme]
        addrs = self._addresses(url, host, port, deadline)
        sock, ip = self._open(addrs, scheme, host, deadline)
        fired = []
        watchdog = threading.Timer(max(0.01, deadline - time.monotonic()), _shut, (sock, fired))
        watchdog.daemon = True
        watchdog.start()
        conn = _Conn(sock, timeout=max(0.05, deadline - time.monotonic()))
        try:
            target = path + ("?" + query if query else "")
            conn.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host if port == {"http": 80, "https": 443}[scheme] else f"{host}:{port}")
            conn.putheader("User-Agent", USER_AGENT)
            conn.putheader("Accept", accept)
            conn.putheader("Accept-Language", "en-US,en;q=0.8")
            conn.putheader("Accept-Encoding", "gzip, deflate")
            conn.putheader("Connection", "close")
            conn.endheaders()
            resp = conn.getresponse()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            if resp.status in REDIRECTS and headers.get("location"):
                return resp.status, headers, None, ip, headers["location"]
            body = self._body(resp, headers, deadline)
            if fired:  # the watchdog shut the socket: what arrived is cut short
                raise FetchFailed("The fetch took longer than %d seconds." % self.timeout_s)
            return resp.status, headers, body, ip, None
        except FetchFailed:
            raise
        except (socket.timeout, TimeoutError):
            raise FetchFailed("The fetch took longer than %d seconds." % self.timeout_s)
        except (http.client.HTTPException, OSError, ValueError):
            if fired or time.monotonic() >= deadline:
                raise FetchFailed("The fetch took longer than %d seconds." % self.timeout_s)
            raise FetchFailed("The site sent a broken answer.")
        finally:
            watchdog.cancel()
            try:
                conn.close()
            except OSError:
                pass

    def _body(self, resp, headers, deadline):
        try:
            if int(headers.get("content-length") or 0) > self.max_bytes:
                raise FetchFailed("The page is over the 10 MB limit.")
        except ValueError:
            pass
        dec = _Decoder(headers.get("content-encoding"))
        out = bytearray()
        raw = 0
        while True:
            if time.monotonic() >= deadline:
                raise FetchFailed("The fetch took longer than %d seconds." % self.timeout_s)
            chunk = resp.read1(65536)
            if not chunk:
                break
            raw += len(chunk)
            if raw > self.max_bytes:
                raise FetchFailed("The page is over the 10 MB limit.")
            out += dec.feed(chunk, self.max_bytes - len(out))
        dec.finish()
        return bytes(out)

    # ---------------------------------------------------------------- the fetch
    def fetch(self, url, *, accept=ACCEPT):
        """GET url through the guard. Returns a Response (any status that is not a redirect); raises Denied for a
        refused URL or address on any hop, FetchFailed for everything else."""
        start = time.monotonic()
        deadline = start + self.timeout_s
        requested = clean_url(url)
        cur, hops = requested, []
        while True:
            hops.append(cur)
            try:
                status, headers, body, ip, location = self._one(cur, deadline, accept)
            except FetchFailed as e:
                e.url = e.url or cur
                raise
            if location is None:
                break
            if len(hops) > self.max_redirects:
                raise FetchFailed("The site redirected more than 5 times.", cur)
            try:
                nxt = urljoin(cur, location.strip())
            except ValueError:  # e.g. an unclosed "[" in the Location header
                raise Denied(_deny("bad_url", "The site redirected to an address that could not be read."), cur)
            d = check_static(nxt)
            if d.kind != "allow":
                raise Denied(d, nxt)
            scheme, host, port, path, query = _split(nxt)
            cur = _join(scheme, host, port, path, query)
        ctype, _, params = headers.get("content-type", "").partition(";")
        m = re.search(r"charset\s*=\s*\"?([A-Za-z0-9._:-]+)", params)
        return Response(url=cur, requested=requested, status=status, headers=headers,
                        content_type=ctype.strip().lower(), charset=m.group(1).lower() if m else None, body=body,
                        hops=hops, ip=ip, elapsed_s=round(time.monotonic() - start, 3))


def live_guard(internet=True, network=None):
    """A guard that can never have test routes (fake=False), for a lead's network filter (mac/lead_proxy.py): the live
    app's reaches the internet; internet=False (the --fake app's) reaches nothing at all."""
    return EgressGuard(fake=False, internet=bool(internet), network=network)


def _shut(sock, fired):
    fired.append(True)
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
