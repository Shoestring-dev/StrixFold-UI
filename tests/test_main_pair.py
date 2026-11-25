"""A main pair that is not the main boxes (box5 + box4, say): halod runs the
built-in profiles' box1 / box2 on its own boxes by rank, the running app's config names the pair, Private chats pin
each box's own host key, and the pages and Studio take the pair's names."""
try:
    import _tmp  # noqa: F401
except ImportError:
    from tests import _tmp  # noqa: F401
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mac"))
sys.path.insert(0, os.path.join(ROOT, "box"))

import halod  # noqa: E402
import halo_control as HC  # noqa: E402
import private_engines as E  # noqa: E402
import profiles  # noqa: E402


BOS = {"halod_host": "box4", "endpoint_host": "box5", "endpoint2_host": "box5", "endpoint3_host": "box4", "images_host": "box5",
       "halos_boxes": ["box5", "box4"], "halos_label": "box5 + box4"}


def pair(*names):
    """What on_boxes needs of halod: its boxes, listed here rank 1 first (the ranks decide, not the order)."""
    H = types.SimpleNamespace(cfg={}, boxes=[types.SimpleNamespace(name=n, rank=r) for r, n in reversed(list(enumerate(names)))])
    H.on_boxes = lambda spec: halod.Halod.on_boxes(H, spec)
    return H


class Halod(unittest.TestCase):
    def test_the_built_in_names_run_on_this_pairs_boxes(self):
        H = pair("box5", "box4")
        ext = halod.Halod.on_boxes(H, profiles.model("qwen38-halogen")["external"])
        self.assertEqual((ext["box"], ext["boxes"]), ("box5", ["box5", "box4"]))
        self.assertEqual(profiles.model("qwen38-halogen")["external"]["boxes"], ["box1", "box2"])  # the profile itself unchanged

    def test_the_halos_and_other_names_are_left_as_they_are(self):
        self.assertEqual(halod.Halod.on_boxes(pair("box1", "box2"), {"box": "box1", "boxes": ["box1", "box2"]}),
                         {"box": "box1", "boxes": ["box1", "box2"]})
        self.assertEqual(halod.Halod.on_boxes(pair("box5", "box4"), {"box": "box4"}), {"box": "box4"})
        self.assertIsNone(halod.Halod.on_boxes(pair("box5", "box4"), None))


class Private(unittest.TestCase):
    def setUp(self):
        for k in ("KEY", "HOSTKEY", "BOXES"):
            self.addCleanup(setattr, E, k, getattr(E, k))
        E.KEY, E.HOSTKEY = "/k", None
        E.BOXES = {"box5": ("100.64.0.21", "user5", "ssh-ed25519 AAAAbox5"), "box4": ("100.64.0.22", "user4", "ssh-ed25519 AAAAbox4")}

    def test_the_engines_take_the_pinned_boxes_by_rank(self):
        self.assertEqual(E.engine_boxes("dsv41-unc"), ("box5", "box4"))
        self.assertEqual(E.engine_boxes("qwen38-kyojin"), ("box5",))

    def test_each_box_pins_its_own_host_key(self):
        a3, a2 = " ".join(E.ssh_argv("box5", "true")), " ".join(E.ssh_argv("box4", "true"))
        self.assertIn("KnownHostsCommand=/bin/echo 100.64.0.21 ssh-ed25519 AAAAbox5", a3)
        self.assertIn("KnownHostsCommand=/bin/echo 100.64.0.22 ssh-ed25519 AAAAbox4", a2)
        self.assertTrue(a3.endswith("-l user5 true 100.64.0.21"))

    def test_the_enclave_key_is_usable_without_the_ssh_config(self):
        """KEY points at a Secure Enclave key: without its provider, every Private check failed to sign in."""
        a = " ".join(E.ssh_argv("box5", "true"))
        self.assertIn("-F /dev/null", a)
        if os.path.exists(E.SK_PROVIDER):
            self.assertIn("SecurityKeyProvider=/usr/lib/ssh-keychain.dylib", a)

    def test_a_box_without_a_key_has_no_private_turn(self):
        E.BOXES = {"box5": ("100.64.0.21", "user5"), "box4": ("100.64.0.22", "user4")}
        with self.assertRaises(RuntimeError):
            E.ssh_argv("box5", "true")
        E.HOSTKEY = "ssh-ed25519 AAAAshared"  # the main boxes' way: one key for both
        self.assertIn("ssh-ed25519 AAAAshared", " ".join(E.ssh_argv("box5", "true")))


class App(unittest.TestCase):
    def test_the_running_apps_config_names_the_main_pair(self):
        tmp = tempfile.mkdtemp(prefix="hc-main-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for k, v in (("CONF_DIR", HC.CONF_DIR), ("SITE_MAIN", HC.SITE_MAIN)):
            self.addCleanup(setattr, HC, k, v)
        HC.CONF_DIR, HC.SITE_MAIN = tmp, dict(BOS)
        cfg = HC.load_config()
        self.assertEqual({k: cfg[k] for k in BOS}, BOS)
        self.assertEqual((HC.DEFAULTS["halod_host"], HC.DEFAULTS["halos_boxes"]), ("box2", ["box1", "box2"]))
        with open(os.path.join(tmp, "config.json"), "w") as f:
            json.dump({"lan": False}, f)
        self.assertEqual((HC.load_config()["endpoint_host"], HC.load_config()["lan"]), ("box5", False))

    def test_qwens_two_copies_by_the_pairs_names(self):
        A = types.SimpleNamespace(cfg=dict(HC.DEFAULTS, **BOS), fake=False, local_endpoint=lambda st: "one")
        A._copy_urls = lambda st: HC.App._copy_urls(A, st)
        st = {"profile": {"model_id": "qwen38-halogen"}, "endpoint": "http://127.0.0.1:18090", "copies": ["box5", "box4"]}
        self.assertEqual(HC.App._copy_urls(A, st), {"box5": "http://127.0.0.1:18090/v1", "box4": "http://127.0.0.1:18095/v1"})
        self.assertEqual(HC.App.replicas(A, st), ["http://127.0.0.1:18090/v1", "http://127.0.0.1:18095/v1"])
        self.assertEqual(HC.App.replicas(A, dict(st, copies=["box4"])), ["http://127.0.0.1:18095/v1"])
        self.assertEqual(HC.App.replicas(A, dict(st, endpoint="http://127.0.0.1:18080")), ["one"])  # one server: not copies


if __name__ == "__main__":
    unittest.main()
