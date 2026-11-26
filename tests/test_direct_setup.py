"""Settings' computers for Direct access (direct_setup.py): added only after a connection test passes, on this Mac only,
never with an address that ssh would read as an option; kept in config.json and in this run's list at once; the test
says in words what to do when ssh can't log in. A stand-in ssh on PATH (no network); test_studio's Http harness for
the routes."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
sys.path.insert(0, os.path.join(ROOT, "mac"))
import chat_direct  # noqa: E402
import direct_setup as D  # noqa: E402
import halo_control as HC  # noqa: E402

FAKE_SSH = r"""#!/bin/sh
# a stand-in ssh: records its arguments, answers as the host's name says
printf '%s\n' "$@" > "$SSH_LOG"
for a; do last="$a"; done
case "$*" in
  *-G*) echo "hostname 192.168.1.20"; echo "user agent"; exit 0;;
esac
case "$*" in
  *" here "*) cat "$(printf '%s' "$last" | sed -n 's/.*cat \(\/tmp\/strixfold-probe-[0-9a-f]*\).*/\1/p')"; exit 0;;
  *" vm "*) f=$(printf '%s' "$last" | sed -n 's/.* \(~\/\.strixfold-probe-[0-9a-f]*\).*/\1/p'); cat "$HOME/${f#\~/}"; exit 0;;
  *" parrot "*) echo strixfold-lacks; printf '%s\n' "$last"; exit 0;;
  *" lacks "*) echo strixfold-lacks; echo "Darwin 25.5.0"; exit 0;;
  *" good "*|*" good"*) echo strixfold-ok; echo "Linux 6.8.0"; exit 0;;
  *" nokey "*|*" nokey"*) echo "agent@192.168.1.20: Permission denied (publickey,password)." >&2; exit 255;;
  *" newhost "*|*" newhost"*) echo "Host key verification failed." >&2; exit 255;;
esac
echo "ssh: Could not resolve hostname x" >&2; exit 255
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="direct-")
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        ssh = os.path.join(bin_, "ssh")
        open(ssh, "w").write(FAKE_SSH)
        os.chmod(ssh, 0o755)
        self.log = os.path.join(self.tmp, "ssh.log")
        old_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old_env)))
        os.environ["PATH"] = bin_ + os.pathsep + os.environ["PATH"]
        os.environ["SSH_LOG"] = self.log
        old_conf, old_targets = HC.CONF_DIR, dict(chat_direct.TARGETS)
        self.addCleanup(setattr, HC, "CONF_DIR", old_conf)
        self.addCleanup(lambda: (chat_direct.TARGETS.clear(), chat_direct.TARGETS.update(old_targets)))
        HC.CONF_DIR = os.path.join(self.tmp, "conf")
        os.makedirs(HC.CONF_DIR)
        self.conf = os.path.join(HC.CONF_DIR, "config.json")
        json.dump({"ui_port": 8711}, open(self.conf, "w"))
        os.chmod(self.conf, 0o600)

    def args(self):
        return open(self.log).read().split("\n")


class Module(Base):
    def test_an_address_never_reaches_ssh_as_an_option(self):
        for bad in ("-oProxyCommand=touch /tmp/x", "-v", ".hidden", "a b", "$(id)", "a;b", "@host", "user@", "a@b@c", "", "x" * 201):
            with self.assertRaises(ValueError, msg=bad):
                D.test(bad, "bash")
        self.assertFalse(os.path.exists(self.log))  # ssh never ran

    def test_the_test_runs_one_command_with_no_password_agent_or_forwarding(self):
        r = D.test("good", "bash")
        self.assertTrue(r["ok"], r)
        self.assertIn("Linux 6.8.0", r["words"])
        a = self.args()
        for opt in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ForwardAgent=no", "ClearAllForwardings=yes", "PermitLocalCommand=no"):
            self.assertIn(opt, a)
        self.assertTrue(a[a.index("good") + 1].startswith("command -v bash base64 setsid"))
        for opt in ("ControlMaster=no", "ControlPath=none"):  # a hand login's open connection never passes for a key
            self.assertIn(opt, a)

    def test_an_address_that_is_this_mac_is_refused(self):
        """Direct access on this Mac is off; an address back to it (localhost, its own name) would undo that."""
        r = D.test("here", "bash")
        self.assertFalse(r["ok"])
        self.assertIn("is this Mac", r["words"])
        self.assertEqual([f for f in os.listdir("/tmp") if f.startswith("strixfold-probe-") and os.path.getmtime(os.path.join("/tmp", f)) > 0
                          and f[len("strixfold-probe-"):] in open(self.log).read()], [])  # its file is gone again
        self.assertIsNone(D.add("Me", "here", "bash")[0])

    def test_a_vm_that_shares_the_home_folder_is_this_mac_too(self):
        os.environ["HOME"] = self.tmp  # the stand-in VM sees this Mac's home folder, not its /tmp
        r = D.test("vm", "bash")
        self.assertFalse(r["ok"])
        self.assertIn("shares its folders", r["words"])
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith(".strixfold-probe-")], [])

    def test_a_host_that_prints_the_command_back_is_not_called_this_mac(self):
        r = D.test("parrot", "bash")
        self.assertFalse(r["ok"])
        self.assertNotIn("this Mac", r["words"])
        self.assertIn("lacks", r["words"])

    def test_a_computer_without_what_direct_needs_is_refused(self):
        r = D.test("lacks", "bash")
        self.assertFalse(r["ok"])
        self.assertIn("lacks bash, base64 or setsid", r["words"])

    def test_a_listed_computer_is_tested_with_its_own_options(self):
        chat_direct.TARGETS["pinned"] = {"name": "Pinned", "host": "good", "shell": "bash",
                                         "ssh_options": ["-o", "IdentityFile=~/.ssh/pinned", "-o", "UserKnownHostsFile=~/.ssh/pinned_hosts"]}
        self.assertTrue(D.test_listed("pinned")["ok"])
        a = self.args()
        self.assertIn("IdentityFile=~/.ssh/pinned", a)
        self.assertIn("UserKnownHostsFile=~/.ssh/pinned_hosts", a)
        for cid in ("mac", "nope"):
            with self.assertRaises(ValueError):
                D.test_listed(cid)

    def test_the_run_never_forwards_this_macs_agent(self):
        chat_direct.TARGETS["lab-x"] = {"name": "Lab", "host": "lab", "shell": "bash"}
        argv = chat_direct.argv_for("lab-x", "true", 5)
        for opt in ("ForwardAgent=no", "ClearAllForwardings=yes", "PermitLocalCommand=no", "StrictHostKeyChecking=yes"):
            self.assertIn(opt, argv)
        self.assertLess(argv.index("ForwardAgent=no"), argv.index("lab"))

    def test_it_says_what_to_do_in_words(self):
        r = D.test("nokey", "bash")
        self.assertFalse(r["ok"])
        self.assertIn("can't log in to it by itself", r["words"])
        self.assertIn("ssh-keygen -t ed25519 -N '' -C strixfold-nokey -f ~/.ssh/strixfold-nokey", r["help"])  # a key per computer
        self.assertIn("ssh-copy-id -i ~/.ssh/strixfold-nokey.pub -o PreferredAuthentications=keyboard-interactive,password agent@192.168.1.20", r["help"])
        self.assertIn("without a passphrase, so Direct can use it", r["help"])
        w = D.test("nokey", "PowerShell")["help"]  # Windows has its own steps
        self.assertIn("administrators_authorized_keys", w)
        self.assertIn("/inheritance:r /grant Administrators:F /grant SYSTEM:F", w)
        self.assertIn("IdentitiesOnly yes", r["help"])
        self.assertIn("Log in once by hand", D.test("newhost", "bash")["words"])
        self.assertIn("can't reach", D.test("nowhere", "bash")["words"])

    def test_added_only_after_a_passing_test_kept_and_removed(self):
        cid, t = D.add("Lab PC", "nokey", "bash")
        self.assertIsNone(cid)
        self.assertNotIn("direct_targets", json.load(open(self.conf)))
        cid, t = D.add("Lab PC", "good", "bash", "Ubuntu, 4 GB")
        self.assertRegex(cid, r"^lab-pc-[0-9a-f]{4}$")
        cfg = json.load(open(self.conf))
        self.assertEqual(cfg["ui_port"], 8711)  # the rest of it stays
        self.assertEqual(cfg["direct_targets"][cid], {"name": "Lab PC", "host": "good", "shell": "bash", "note": "Ubuntu, 4 GB"})
        self.assertEqual(stat.S_IMODE(os.stat(self.conf).st_mode), 0o600)
        self.assertIn(cid, chat_direct.TARGETS)  # this run's list too: no restart
        self.assertTrue(chat_direct.available(cid))
        D.remove(cid)
        self.assertNotIn(cid, json.load(open(self.conf))["direct_targets"])
        self.assertNotIn(cid, chat_direct.TARGETS)
        self.assertNotEqual(D.add("Lab PC", "good", "bash")[0], cid)  # an id is never used twice
        for cid in ("mac", "nope"):
            with self.assertRaises(ValueError):
                D.remove(cid)
        self.assertIn("mac", chat_direct.TARGETS)


class Together(Base):
    def test_adds_at_once_each_get_their_own_id_and_all_are_kept(self):
        import threading
        got = []
        ts = [threading.Thread(target=lambda: got.append(D.add("Box", "good", "bash")[0])) for _ in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(set(got)), 12)
        self.assertEqual(set(json.load(open(self.conf))["direct_targets"]), set(got))

    def test_a_config_whose_list_is_not_a_list_is_left_alone(self):
        json.dump({"direct_targets": None}, open(self.conf, "w"))
        self.assertIsNotNone(D.add("Box", "good", "bash")[0])  # null is an empty list
        json.dump({"direct_targets": ["x"]}, open(self.conf, "w"))
        with self.assertRaises(ValueError):
            D.add("Box", "good", "bash")
        self.assertEqual(json.load(open(self.conf)), {"direct_targets": ["x"]})


class Routes(Base):
    def setUp(self):
        super().setUp()
        loader = importlib.machinery.SourceFileLoader("hc_test_studio_direct", os.path.join(ROOT, "tests", "test_studio.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.ts = importlib.util.module_from_spec(spec)
        loader.exec_module(self.ts)
        self.ts.Http.setUp(self)
        self.ts.Http.unlock(self)

    def req(self, *a, **k):
        return self.ts.Http.req(self, *a, **k)

    def test_on_this_mac_with_a_session_and_the_passcode(self):
        code, body, _ = self.req("POST", "/studio/api/direct", {"name": "Lab", "host": "good", "shell": "bash", "passcode": "wrong one"})
        self.assertEqual(code, 403, body)  # a shell on that computer: the passcode again
        code, body, _ = self.req("POST", "/studio/api/direct", {"name": "Lab", "host": "good", "shell": "bash", "passcode": "correct horse"})
        self.assertEqual(code, 200, body)
        lab = json.loads(body)["id"]
        cs = json.loads(self.req("GET", "/studio/api/direct")[1])["computers"]
        self.assertEqual([c["id"] for c in cs if c["mine"]], [lab])  # (computers named in code are listed, not removable)
        self.assertNotIn("mac", [c["id"] for c in cs])
        code, body, _ = self.req("POST", "/studio/api/direct", {"name": "Lab", "host": "nokey", "shell": "bash", "passcode": "correct horse"})
        self.assertEqual(code, 409)
        self.assertIn("help", json.loads(body))
        self.assertIn("can't log in", json.loads(body)["error"])
        self.assertEqual(self.req("POST", "/studio/api/direct/test", {"id": lab})[0], 200)
        self.assertEqual(self.req("DELETE", f"/studio/api/direct/{lab}")[0], 200)
        self.assertEqual(self.req("DELETE", "/studio/api/direct/mac")[0], 400)
        self.assertEqual(self.req("GET", "/studio/api/direct", cookie=False)[0], 401)
        self.assertEqual(self.req("POST", "/studio/api/direct", {"name": "x", "host": "good", "shell": "bash"}, Origin="http://evil.example")[0], 403)

    def test_never_from_the_phone(self):
        old = HC.Handler.channel
        self.addCleanup(setattr, HC.Handler, "channel", old)
        HC.Handler.channel = lambda h: "tailnet"
        code, body, _ = self.req("POST", "/studio/api/direct", {"name": "Lab", "host": "good", "shell": "bash"})
        self.assertEqual(code, 403, body)
        self.assertNotIn("direct_targets", json.load(open(self.conf)))
        self.assertEqual(self.req("GET", "/studio/api/direct")[0], 403)


if __name__ == "__main__":
    unittest.main()
