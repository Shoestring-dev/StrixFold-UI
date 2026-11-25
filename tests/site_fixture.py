"""A setup's own machines as its config.json would give them: two pairs and four Direct access computers (the tests'
own copy, so they run the same with or without this setup's mac/site_defaults.py)."""
import os

PAIRS = [
    {"name": "box-b", "label": "box5 + box4", "halod_host": "box4", "halod_local_port": 8716, "halod_port": 8710,
     "endpoint_host": "box5", "endpoint_local_port": 18081, "endpoint_port": 18080},
    {"name": "box-c", "label": "box3 + box6", "halod_host": "box6", "halod_local_port": 8717, "halod_port": 8710,
     "endpoint_host": "box3", "endpoint_local_port": 18082, "endpoint_port": 18080},
]

DIRECT_TARGETS = {
    "toolbox": {"name": "Toolbox", "host": "gpu-pc", "shell": "PowerShell"},
    "desk": {"name": "Desk", "host": "desk-pc", "shell": "PowerShell"},
    "vps": {"name": "VPS", "host": "vps-desktop", "shell": "bash",
               "note": "A small Ubuntu VPS: keep jobs light. Its live desktop is X display :10 (DISPLAY=:10 for a program "
                       "the user should see). This model is also reachable on the VPS at http://127.0.0.1:18080/v1 (model "
                       "dsv41) while the tunnel is up."},
    "vps2": {"name": "VPS 2", "host": "ubuntu@192.0.2.10", "shell": "bash",  # no ~/.ssh/config entry: its own ssh options
             "ssh_options": ["-o", "IdentityFile=/tmp/id_test", "-o", "UserKnownHostsFile=/tmp/known_hosts_test"]},
}


def use_pairs(t):
    """halo_control.DEFAULTS carries the two pairs for test t, so an App built from them routes over them."""
    import halo_control
    t.addCleanup(halo_control.DEFAULTS.__setitem__, "pairs", halo_control.DEFAULTS["pairs"])
    halo_control.DEFAULTS["pairs"] = PAIRS


def use_targets(t):
    """chat_direct.TARGETS has the four computers for test t."""
    import chat_direct
    t.addCleanup(setattr, chat_direct, "TARGETS", chat_direct.TARGETS)
    chat_direct.TARGETS = dict(chat_direct.TARGETS, **DIRECT_TARGETS)

# Private chats' boxes for the private tunnel's tests (made up: no real address or host key)
PRIVATE_BOXES = {"key": "~/.ssh/id_test", "hostkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKeyNotARealOneFixtureHostKeyNotARea",
                 "boxes": {"box1": ("100.64.0.11", "user1"), "box2": ("100.64.0.12", "user2")}}


def use_private(t):
    """private_engines pins the fixture's boxes and host key for test t."""
    import private_engines as E
    for k in ("KEY", "HOSTKEY", "BOXES"):
        t.addCleanup(setattr, E, k, getattr(E, k))
    E.KEY, E.HOSTKEY, E.BOXES = os.path.expanduser(PRIVATE_BOXES["key"]), PRIVATE_BOXES["hostkey"], dict(PRIVATE_BOXES["boxes"])
