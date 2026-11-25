"""Test helpers the chat's sandbox tests share (core: chat_full's and the Command Centre's tests import them): macOS
probes run inside a profile, and a stand-in internet site behind a --fake egress guard."""
import os
import shutil
import socket
import subprocess
import tempfile
import threading


LS_PROBE = ("import ctypes\n"
            "cs = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/CoreServices.framework/CoreServices')\n"
            "cf = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')\n"
            "cf.CFStringCreateWithCString.restype = ctypes.c_void_p\n"
            "cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]\n"
            "cs.LSCopyApplicationURLsForBundleIdentifier.restype = ctypes.c_void_p\n"
            "cs.LSCopyApplicationURLsForBundleIdentifier.argtypes = [ctypes.c_void_p, ctypes.c_void_p]\n"
            "s = cf.CFStringCreateWithCString(None, b'com.apple.finder', 0x08000100)\n"
            "print('LS:found' if cs.LSCopyApplicationURLsForBundleIdentifier(s, None) else 'LS:none')\n")
# connects to each address it's given; prints one line each: <name> CONNECTED | <name> refused <errno>
NET_PROBE = ("import socket, sys\n"
             "for spec in sys.argv[1:]:\n"
             "    name, fam, host, port = spec.split('|')\n"
             "    s = socket.socket(getattr(socket, fam)); s.settimeout(2)\n"
             "    try:\n"
             "        s.connect(host if fam == 'AF_UNIX' else (host, int(port))); print(name, 'CONNECTED')\n"
             "    except Exception as e:\n"
             "        print(name, 'refused', getattr(e, 'errno', None) or type(e).__name__)\n")


def fake_guard():
    """A --fake egress guard (test routes to stand-in sites on 127.0.0.1, nothing else): built here, never in the app."""
    from agents_web.guard import EgressGuard
    return EgressGuard(fake=True)


class Upstream:
    """A stand-in internet site on 127.0.0.1 (reached only through a --fake guard's test route): plain HTTP, or HTTPS
    with a throwaway certificate for its name. It keeps each request's head."""

    def __init__(self, test, body=b"SIMPLE-OK", tls_name=None):
        import ssl
        self.heads, self.body = [], body
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        test.addCleanup(self.sock.close)
        self.ctx = None
        if tls_name:
            d = tempfile.mkdtemp(prefix="up-", dir="/tmp")
            test.addCleanup(shutil.rmtree, d, True)
            self.cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
            subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", self.cert,
                            "-days", "1", "-subj", f"/CN={tls_name}", "-addext", f"subjectAltName=DNS:{tls_name}"],
                           check=True, capture_output=True)
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(self.cert, key)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.one, args=(c,), daemon=True).start()

    def one(self, c):
        try:
            if self.ctx:
                c = self.ctx.wrap_socket(c, server_side=True)
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = c.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.heads.append(data.split(b"\r\n\r\n")[0].decode("latin-1"))
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(self.body) + self.body)
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
