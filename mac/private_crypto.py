"""The cipher for Private chats. Python's standard library
plus macOS's own CommonCrypto (by ctypes): no package, nothing in a user site-packages.

  seal(key, ad, plaintext) -> blob        open_(key, ad, blob) -> plaintext (raises Tampered, never returns bad data)

- Subkeys for every key K: Kenc = HMAC-SHA256(K, "hpv1 enc"), Kmac = HMAC-SHA256(K, "hpv1 mac").
- AES-256-CTR (CCCryptorCreateWithMode, kCCModeCTR, kCCModeOptionCTR_BE) with a fresh 16-byte nonce from
  CCRandomGenerateBytes as the first counter block, on every write.
- MAC = HMAC-SHA256(Kmac, "hpv1" || nonce || len(AD) || AD || ct). AD is a list of parts (chat id, record kind, record
  id, field), each length-prefixed (4 bytes, big-endian), so a blob can't be moved to another row or field.
- The blob: "hpv1" | nonce (16) | ct | tag (32). The tag is compared (hmac.compare_digest) before anything is
  decrypted.
- wrap_key / unwrap_key: a 32-byte key in the same envelope (the PMK under the passcode's key: AD ["pmk-wrap"]).
- passcode_key: PBKDF2-HMAC-SHA256, 1,000,000 rounds (hashlib.scrypt isn't in this Mac's Python).
Known-answer tests: NIST SP 800-38A F.5.5/F.5.6 (CTR-AES256) and RFC 4231 (HMAC-SHA256), tests/test_private_crypto.py.
"""
import ctypes
import hashlib
import hmac
import struct
import threading

MAGIC = b"hpv1"
NONCE, TAG, KEY, SALT = 16, 32, 32, 16
PBKDF2_ROUNDS = 1_000_000
PMK_AD = ("pmk-wrap",)  # the PMK's wrap under the passcode's key: one label, here only

# CommonCrypto (CommonCryptor.h): the public API only
kCCEncrypt, kCCDecrypt = 0, 1
kCCAlgorithmAES = 0
kCCModeCTR = 4
ccNoPadding = 0
kCCModeOptionCTR_BE = 2
kCCSuccess = 0

_cc = None


class Tampered(Exception):
    """A blob that isn't one of ours, was changed, moved to another place, or cut short."""


class CryptoUnavailable(RuntimeError):
    pass


_LIB_MU = threading.Lock()  # one first load of CommonCrypto, whichever thread asks first


def _lib():
    global _cc
    if _cc is not None:
        return _cc
    with _LIB_MU:
        if _cc is not None:
            return _cc
        try:
            cc = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
        except OSError as e:
            raise CryptoUnavailable(f"CommonCrypto isn't available: {e}") from None
        cc.CCCryptorCreateWithMode.restype = ctypes.c_int32
        cc.CCCryptorCreateWithMode.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                               ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                               ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        cc.CCCryptorUpdate.restype = ctypes.c_int32
        cc.CCCryptorUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
        cc.CCCryptorRelease.restype = ctypes.c_int32
        cc.CCCryptorRelease.argtypes = [ctypes.c_void_p]
        cc.CCRandomGenerateBytes.restype = ctypes.c_int32
        cc.CCRandomGenerateBytes.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        _cc = cc
    return _cc


def random_bytes(n):
    """n bytes from CommonCrypto's CSPRNG (CCRandomGenerateBytes)."""
    buf = ctypes.create_string_buffer(n)
    if _lib().CCRandomGenerateBytes(buf, n) != kCCSuccess:
        raise CryptoUnavailable("CCRandomGenerateBytes failed")
    return buf.raw


def aes_ctr(key, counter, data, op=kCCEncrypt):
    """AES-256-CTR over data, the 16-byte counter block big-endian (kCCModeOptionCTR_BE). Encrypt and decrypt are one."""
    if len(key) != KEY or len(counter) != NONCE:
        raise ValueError("a 32-byte key and a 16-byte counter")
    cc = _lib()
    ref = ctypes.c_void_p()
    kb, cb = ctypes.create_string_buffer(key, KEY), ctypes.create_string_buffer(counter, NONCE)
    st = cc.CCCryptorCreateWithMode(op, kCCModeCTR, kCCAlgorithmAES, ccNoPadding, cb, kb, KEY, None, 0, 0, kCCModeOptionCTR_BE,
                                    ctypes.byref(ref))
    if st != kCCSuccess:
        ctypes.memset(kb, 0, KEY)
        raise CryptoUnavailable(f"CCCryptorCreateWithMode: {st}")
    try:
        n = len(data)
        out = ctypes.create_string_buffer(max(1, n))
        moved = ctypes.c_size_t(0)
        st = cc.CCCryptorUpdate(ref, data, n, out, max(1, n), ctypes.byref(moved))
        if st != kCCSuccess or moved.value != n:
            raise CryptoUnavailable(f"CCCryptorUpdate: {st} ({moved.value} of {n} bytes)")
        return out.raw[:n]
    finally:
        cc.CCCryptorRelease(ref)
        ctypes.memset(kb, 0, KEY)


def subkeys(key):
    if len(key) != KEY:
        raise ValueError("a 32-byte key")
    return hmac.new(key, b"hpv1 enc", hashlib.sha256).digest(), hmac.new(key, b"hpv1 mac", hashlib.sha256).digest()


def ad_bytes(ad):
    """The associated data: each part (str or bytes) length-prefixed with 4 bytes, big-endian."""
    out = b""
    for p in ad:
        b = p.encode("utf-8") if isinstance(p, str) else bytes(p)
        out += struct.pack(">I", len(b)) + b
    return out


def _tag(kmac, nonce, ad, ct):
    a = ad_bytes(ad)
    return hmac.new(kmac, MAGIC + nonce + struct.pack(">I", len(a)) + a + ct, hashlib.sha256).digest()


def seal(key, ad, plaintext):
    """A blob of plaintext (bytes) under key, bound to ad (a list of parts)."""
    kenc, kmac = subkeys(key)
    nonce = random_bytes(NONCE)
    ct = aes_ctr(kenc, nonce, bytes(plaintext))
    return MAGIC + nonce + ct + _tag(kmac, nonce, ad, ct)


def open_(key, ad, blob):
    """The plaintext of a blob made by seal with the same key and ad; Tampered for anything else (checked first)."""
    blob = bytes(blob or b"")
    if len(blob) < len(MAGIC) + NONCE + TAG or blob[:len(MAGIC)] != MAGIC:
        raise Tampered("not a Private blob")
    kenc, kmac = subkeys(key)
    nonce, ct, tag = blob[len(MAGIC):len(MAGIC) + NONCE], blob[len(MAGIC) + NONCE:-TAG], blob[-TAG:]
    if not hmac.compare_digest(_tag(kmac, nonce, ad, ct), tag):
        raise Tampered("the blob was changed, moved or cut short")
    return aes_ctr(kenc, nonce, ct, kCCDecrypt)


def seal_text(key, ad, text):
    return seal(key, ad, str(text).encode("utf-8"))


def open_text(key, ad, blob):
    return open_(key, ad, blob).decode("utf-8")


def new_key():
    return random_bytes(KEY)


def passcode_key(passcode, salt, rounds=PBKDF2_ROUNDS):
    """The key a passcode gives (PBKDF2-HMAC-SHA256); a salt of at least SALT bytes."""
    if len(bytes(salt)) < SALT:
        raise ValueError(f"a salt of at least {SALT} bytes")
    return hashlib.pbkdf2_hmac("sha256", str(passcode).encode("utf-8"), bytes(salt), int(rounds), KEY)


def wrap_key(kek, key, ad):
    if len(key) != KEY:
        raise ValueError("a 32-byte key")
    return seal(kek, ad, key)


def unwrap_key(kek, ad, blob):
    k = open_(kek, ad, blob)
    if len(k) != KEY:
        raise Tampered("not a wrapped key")
    return k
