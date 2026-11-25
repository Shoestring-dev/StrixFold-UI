"""Private chats' keys and their lock. studio.Store asks this
module for every read and write of a Private chat; nothing else holds a key.

- The private master key (PMK): 32 random bytes made the first time Private is used, stored only wrapped under the key
  the user's passcode gives (private_crypto.passcode_key: PBKDF2-HMAC-SHA256, 1,000,000 rounds, its own salt) in kv
  `private_wrap`. It is in memory only after the passcode is typed in this app run (unlock), until lock(), an app
  restart, or IDLE_S with no private request.
- A chat's key (CK): 32 random bytes, wrapped under the PMK in the chat's row (chats.pkey, AD ["chat-key", chat id]).
  Deleting the chat removes the row first (crypto-shred), then its files.
- The grant: a context variable that Studio's HTTP layer sets for a request that carries the unlocked page's private
  token (slice 3), and that a turn's thread inherits. Store methods for a Private chat raise Locked without both the
  grant and an unlocked PMK, so the other sections, Agents, the lead tools, search and the outbox for another chat
  never read one.
- Field values are stored as "hpv:" + base64(blob); AD = [chat id, record kind, record id, field].
"""
import base64
import contextlib
import contextvars
import hashlib
import hmac
import json
import threading
import time

import private_crypto as C

PREFIX = "hpv:"
IDLE_S = 30 * 60
MIN_PASSCODE = 6  # P4: any passcode opens Private, 6 digits included (it was 10 characters)
GRANT = contextvars.ContextVar("halo_private_grant", default=False)


class Locked(Exception):
    """A Private chat without the grant or with Private locked."""

    def __init__(self, why="locked"):
        super().__init__(why)


class Unreadable(Locked):
    """A Private chat whose key doesn't open (damaged, or already shredded by a delete). Raised only past require(), so
    only to a request that holds the grant; the chat can still be deleted (Store.delete_chat never opens its key)."""


UNREADABLE_CHAT = "This Private chat can't be read: its key is damaged. You can still delete it."


@contextlib.contextmanager
def granted():
    """Within it, this thread may read and write Private chats (when the vault is unlocked)."""
    tok = GRANT.set(True)
    try:
        yield
    finally:
        GRANT.reset(tok)


class Vault:
    def __init__(self, store):
        self.s = store
        self.mu = threading.Lock()
        self.pmk = None
        self.used = 0.0
        self._keys = {}  # chat id -> CK, while unlocked
        self.server_key = C.random_bytes(32)  # this app run's: a page's private token is checked by its HMAC under it
        self.tokens = {}  # device session id -> HMAC of its private token (the token lives only in the page's JS memory)

    # ---------------------------------------------------------------- the lock
    def is_set(self):
        return bool(self.s.kv("private_wrap"))

    def unlocked(self, now=None):
        with self.mu:
            if self.pmk is None:
                return False
            if (now or time.time()) - self.used > IDLE_S:
                self._drop()
                return False
            return True

    def unlock(self, passcode):
        """The PMK from the passcode (made and wrapped the first time). The caller has checked the passcode with Studio's
        own verifier and its throttle; a wrap that doesn't open is refused (Locked) and nothing changes."""
        rec = self.s.kv("private_wrap")
        if rec:
            kek = C.passcode_key(passcode, bytes.fromhex(rec["salt"]), rec.get("rounds", C.PBKDF2_ROUNDS))
            try:
                pmk = C.unwrap_key(kek, list(C.PMK_AD), base64.b64decode(rec["wrap"]))
            except (C.Tampered, ValueError, KeyError):
                raise Locked("the passcode doesn't open Private") from None
        else:
            salt, pmk = C.random_bytes(16), C.new_key()
            kek = C.passcode_key(passcode, salt)
            self.s.set_kv("private_wrap", {"salt": salt.hex(), "rounds": C.PBKDF2_ROUNDS, "t": time.time(),
                                           "wrap": base64.b64encode(C.wrap_key(kek, pmk, list(C.PMK_AD))).decode()})
        with self.mu:
            self.pmk, self.used, self._keys = pmk, time.time(), {}

    def rewrapped(self, old, new):
        """A passcode change: the record of the same PMK wrapped under the new passcode (Private keeps its chats), for
        the caller to write together with the new passcode (Auth.change: one transaction). None when Private isn't set;
        Locked when the old passcode doesn't open it (nothing is written)."""
        rec = self.s.kv("private_wrap")
        if not rec:
            return None
        kek = C.passcode_key(old, bytes.fromhex(rec["salt"]), rec.get("rounds", C.PBKDF2_ROUNDS))
        try:
            pmk = C.unwrap_key(kek, list(C.PMK_AD), base64.b64decode(rec["wrap"]))
        except (C.Tampered, ValueError, KeyError):
            raise Locked("the passcode doesn't open Private") from None
        salt = C.random_bytes(C.SALT)
        return {"salt": salt.hex(), "rounds": C.PBKDF2_ROUNDS, "t": time.time(),
                "wrap": base64.b64encode(C.wrap_key(C.passcode_key(new, salt), pmk, list(C.PMK_AD))).decode()}

    def lock(self):
        with self.mu:
            self._drop()

    def _drop(self):
        self.pmk, self._keys, self.tokens = None, {}, {}

    # ---------------------------------------------------------------- the page's private token
    def issue(self, device_id):
        """A new private token for one device session (after the passcode): 32 random bytes the page keeps in memory only;
        the server keeps only their HMAC. A reload, Lock, the idle lock or a restart ends it."""
        tok = C.random_bytes(32)
        with self.mu:
            self.tokens[str(device_id)] = hmac.new(self.server_key, tok, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(tok).decode().rstrip("=")

    def check(self, device_id, token):
        """Whether a request's private token is this device session's, and Private still unlocked."""
        if not token or not device_id or not self.unlocked():
            return False
        try:
            raw = base64.urlsafe_b64decode(str(token) + "=" * (-len(str(token)) % 4))
        except (ValueError, TypeError):
            return False
        with self.mu:
            want = self.tokens.get(str(device_id))
        return bool(want) and hmac.compare_digest(want, hmac.new(self.server_key, raw, hashlib.sha256).digest())

    def touch(self):
        with self.mu:
            if self.pmk is not None:
                self.used = time.time()

    def require(self):
        """Raise Locked unless this thread holds the grant and Private is unlocked."""
        if not GRANT.get():
            raise Locked("locked")
        if not self.unlocked():
            raise Locked("Private is locked")
        self.touch()

    # ---------------------------------------------------------------- chat keys
    def new_chat_key(self, cid):
        """A new chat's key, wrapped under the PMK (for chats.pkey)."""
        self.require()
        ck = C.new_key()
        with self.mu:
            w = C.wrap_key(self.pmk, ck, ["chat-key", cid])
            self._keys[cid] = ck
        return base64.b64encode(w).decode()

    def chat_key(self, cid, wrapped):
        self.require()
        with self.mu:
            if cid in self._keys:
                return self._keys[cid]
            if not wrapped:
                raise Unreadable(UNREADABLE_CHAT)
            try:
                ck = C.unwrap_key(self.pmk, ["chat-key", cid], base64.b64decode(wrapped))
            except (C.Tampered, ValueError):
                raise Unreadable(UNREADABLE_CHAT) from None
            self._keys[cid] = ck
            return ck

    def forget(self, cid):
        with self.mu:
            self._keys.pop(cid, None)

    # ---------------------------------------------------------------- fields and files
    def enc(self, ck, cid, kind, rid, field, text):
        return PREFIX + base64.b64encode(C.seal_text(ck, [cid, kind, str(rid), field], "" if text is None else text)).decode()

    def dec(self, ck, cid, kind, rid, field, value):
        """A stored field's text. A value that isn't sealed (a row's empty placeholder) comes back as it is; a sealed one
        that doesn't open says so plainly instead of raising (the page shows the rest of the chat)."""
        if not isinstance(value, str) or not value.startswith(PREFIX):
            return value
        try:
            return C.open_text(ck, [cid, kind, str(rid), field], base64.b64decode(value[len(PREFIX):]))
        except (C.Tampered, ValueError):
            return UNREADABLE

    def enc_json(self, ck, cid, kind, rid, field, obj):
        return self.enc(ck, cid, kind, rid, field, json.dumps(obj or {}))

    def dec_json(self, ck, cid, kind, rid, field, value):
        t = self.dec(ck, cid, kind, rid, field, value)
        if t == UNREADABLE:
            return {"unreadable": True}
        try:
            return json.loads(t or "{}")
        except ValueError:
            return {}

    def enc_bytes(self, ck, cid, rid, field, data):
        return C.seal(ck, [cid, "file", str(rid), field], data)

    def dec_bytes(self, ck, cid, rid, field, blob):
        return C.open_(ck, [cid, "file", str(rid), field], blob)


UNREADABLE = "(This part can't be read: it was changed or damaged on disk.)"
