"""The Private chats cipher (mac/private_crypto.py): known-answer tests against NIST SP
800-38A (CTR-AES256) and RFC 4231 (HMAC-SHA256), then the envelope: a fresh nonce per write, the tag checked before
anything is decrypted, and a changed, moved, cut or foreign blob refused."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import hashlib
import hmac
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import private_crypto as C  # noqa: E402

H = bytes.fromhex


class KnownAnswers(unittest.TestCase):
    # NIST SP 800-38A, F.5.5 CTR-AES256.Encrypt and F.5.6 CTR-AES256.Decrypt
    KEY = H("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
    CTR = H("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
    PT = H("6bc1bee22e409f96e93d7e117393172a" "ae2d8a571e03ac9c9eb76fac45af8e51"
           "30c81c46a35ce411e5fbc1191a0a52ef" "f69f2445df4f9b17ad2b417be66c3710")
    CT = H("601ec313775789a5b7a7f504bbf3d228" "f443e3ca4d62b59aca84e990cacaf5c5"
           "2b0930daa23de94ce87017ba2d84988d" "dfc9c58db67aada613c2dd08457941a6")

    def test_sp800_38a_ctr_aes256(self):
        self.assertEqual(C.aes_ctr(self.KEY, self.CTR, self.PT, C.kCCEncrypt), self.CT)  # F.5.5
        self.assertEqual(C.aes_ctr(self.KEY, self.CTR, self.CT, C.kCCDecrypt), self.PT)  # F.5.6
        # a part block and a cut in the middle of the stream: the same keystream, byte for byte
        self.assertEqual(C.aes_ctr(self.KEY, self.CTR, self.PT[:37]), self.CT[:37])

    def test_rfc4231_hmac_sha256(self):
        cases = [  # test cases 1-4 of RFC 4231 (HMAC-SHA-256)
            (b"\x0b" * 20, b"Hi There", "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"),
            (b"Jefe", b"what do ya want for nothing?", "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"),
            (b"\xaa" * 20, b"\xdd" * 50, "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe"),
            (bytes(range(1, 26)), b"\xcd" * 50, "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b"),
        ]
        for key, data, want in cases:
            self.assertEqual(hmac.new(key, data, hashlib.sha256).hexdigest(), want)

    def test_subkeys_are_hmacs_of_the_key(self):
        k = bytes(range(32))
        enc, mac = C.subkeys(k)
        self.assertEqual(enc, hmac.new(k, b"hpv1 enc", hashlib.sha256).digest())
        self.assertEqual(mac, hmac.new(k, b"hpv1 mac", hashlib.sha256).digest())
        self.assertEqual(len({k, enc, mac}), 3)

    def test_the_envelope_byte_for_byte(self):
        """A blob rebuilt by hand from the spec (magic, nonce, AES-256-CTR under Kenc, the MAC under Kmac over magic,
        nonce, the AD's length, the length-prefixed AD and the ciphertext) opens; open_ agrees with the spec."""
        k = bytes(range(32, 64))
        enc, mac = C.subkeys(k)
        nonce = bytes(16)
        ad = ["c0ffee", "message", "42", "content"]
        a = b"".join(len(p).to_bytes(4, "big") + p.encode() for p in ad)
        ct = C.aes_ctr(enc, nonce, b"hello private")
        tag = hmac.new(mac, b"hpv1" + nonce + len(a).to_bytes(4, "big") + a + ct, hashlib.sha256).digest()
        self.assertEqual(C.open_(k, ad, b"hpv1" + nonce + ct + tag), b"hello private")


class Envelope(unittest.TestCase):
    def setUp(self):
        self.k = C.new_key()
        self.ad = ["0123456789abcdef", "message", "17", "content"]

    def test_round_trip_and_sizes(self):
        for pt in (b"", b"x", os.urandom(1 << 20)):
            blob = C.seal(self.k, self.ad, pt)
            self.assertEqual(len(blob), 4 + 16 + len(pt) + 32)
            self.assertEqual(C.open_(self.k, self.ad, blob), pt)
        self.assertEqual(C.open_text(self.k, self.ad, C.seal_text(self.k, self.ad, "naïve ✓")), "naïve ✓")

    def test_a_fresh_nonce_every_write(self):
        blobs = {C.seal(self.k, self.ad, b"same words") for _ in range(50)}
        self.assertEqual(len(blobs), 50)
        self.assertEqual(len({b[4:20] for b in blobs}), 50)

    def test_changed_moved_cut_or_foreign_blobs_are_refused(self):
        blob = C.seal(self.k, self.ad, b"secret words")
        bad = [
            bytes([blob[0] ^ 1]) + blob[1:],                       # the magic
            blob[:6] + bytes([blob[6] ^ 1]) + blob[7:],            # the nonce
            blob[:22] + bytes([blob[22] ^ 1]) + blob[23:],         # the ciphertext
            blob[:-1] + bytes([blob[-1] ^ 1]),                     # the tag
            blob[:-1],                                             # cut short
            blob + b"\0",                                          # grown
            b"hpv1",                                               # nothing at all
        ]
        for b in bad:
            with self.assertRaises(C.Tampered):
                C.open_(self.k, self.ad, b)
        for ad in (["0123456789abcdef", "message", "18", "content"],     # another record
                   ["0123456789abcdef", "message", "17", "meta"],        # another field
                   ["fedcba9876543210", "message", "17", "content"],     # another chat
                   ["0123456789abcdef", "message", "17"]):               # a part missing
            with self.assertRaises(C.Tampered):
                C.open_(self.k, ad, blob)
        with self.assertRaises(C.Tampered):
            C.open_(C.new_key(), self.ad, blob)                          # another key

    def test_the_parts_of_the_ad_cant_be_shifted(self):
        blob = C.seal(self.k, ["ab", "c"], b"x")
        with self.assertRaises(C.Tampered):
            C.open_(self.k, ["a", "bc"], blob)

    def test_the_tag_is_checked_before_anything_is_decrypted(self):
        blob = C.seal(self.k, self.ad, b"secret")
        calls = []
        real = C.aes_ctr
        C.aes_ctr = lambda *a, **k: calls.append(a) or real(*a, **k)
        self.addCleanup(setattr, C, "aes_ctr", real)
        with self.assertRaises(C.Tampered):
            C.open_(self.k, self.ad, blob[:-1] + bytes([blob[-1] ^ 1]))
        self.assertEqual(calls, [])
        C.open_(self.k, self.ad, blob)
        self.assertEqual(len(calls), 1)

    def test_the_master_key_wrapped_under_the_passcode(self):
        salt = C.random_bytes(16)
        kek = C.passcode_key("correct horse battery", salt)
        self.assertEqual(kek, hashlib.pbkdf2_hmac("sha256", b"correct horse battery", salt, 1_000_000, 32))
        pmk = C.new_key()
        w = C.wrap_key(kek, pmk, ["pmk-wrap"])
        self.assertEqual(C.unwrap_key(kek, ["pmk-wrap"], w), pmk)
        with self.assertRaises(C.Tampered):
            C.unwrap_key(C.passcode_key("wrong", salt, rounds=1000), ["pmk-wrap"], w)
        with self.assertRaises(C.Tampered):
            C.unwrap_key(kek, ["chat-key", "x"], w)  # the same key under another label
        self.assertNotIn(pmk, w)
        self.assertEqual(C.PMK_AD, ("pmk-wrap",))  # the store wraps under this label, and only this one
        with self.assertRaisesRegex(ValueError, "at least 16 bytes"):
            C.passcode_key("correct horse battery", salt[:15], rounds=1000)

    def test_the_library_loads_once_under_threads(self):
        import threading
        old = C._cc
        self.addCleanup(setattr, C, "_cc", old)
        C._cc = None
        got = []
        ts = [threading.Thread(target=lambda: got.append(C._lib())) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len({id(x) for x in got}), 1)

    def test_random_bytes(self):
        a, b = C.random_bytes(32), C.random_bytes(32)
        self.assertEqual((len(a), len(b)), (32, 32))
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
