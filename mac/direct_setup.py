"""Settings' "Computers for Direct access": add, test and remove the computers a chat's Direct access runs commands on
(chat_direct.TARGETS), kept in config.json's direct_targets. On this Mac only (the routes refuse the phone and the home
network). A computer is reached as ssh knows it: a Host name from ~/.ssh/config or user@address, with ssh's own key and
known host, never a password; the connection test runs one harmless command there and says in words what to do when it
can't get in. Python 3.9 standard library only."""
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading

import chat_direct

# an ssh name, or user@address: never an option ("-o..."), and nothing ssh refuses after the @
HOST = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}@)?(?![-.])[A-Za-z0-9_.-]{1,190}")
SSH = "ssh"
# the run's own (chat_direct.SSH_SAFE), and no shared connection: a hand login's open master never passes for a key
SSH_OPTS = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            *chat_direct.SSH_SAFE]
SHELLS = ("bash", "PowerShell")
MU = threading.Lock()  # one add or remove at a time: config.json and the run's list together
HERE = ("/tmp/strixfold-probe-", "~/.strixfold-probe-")  # files only this Mac has (its /tmp, and its home folder, which
# a VM here may share): either read back, and the address is this Mac


def probe(shell, name):
    """The one command the test runs there: it says it can run Direct's commands, what it is, and (bash) whether the
    files only this Mac has are there. It names the files, never what is in them, so a host that prints the command back
    can't pass for this Mac."""
    if shell == "bash":
        return (f"command -v bash base64 setsid >/dev/null && echo strixfold-ok || echo strixfold-lacks; uname -sr; "
                f"cat {HERE[0]}{name} {HERE[1]}{name} 2>/dev/null")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -Command \"'strixfold-ok'; [Environment]::OSVersion.VersionString\""
KEY_HELP = """It needs a key of its own for this computer. In Terminal on this Mac:

  ssh-keygen -t ed25519 -N '' -C strixfold-{name} -f ~/.ssh/strixfold-{name}
{copy}
then give it a name at the end of ~/.ssh/config (or, if it has a Host entry already, add the IdentityFile and
IdentitiesOnly lines to it), and add the computer here by that name ({name}):

  Host {name}
      HostName {address}
      User {user}{port}
      IdentityFile ~/.ssh/strixfold-{name}
      IdentitiesOnly yes

-N '' leaves the key without a passphrase, so Direct can use it with nobody there to type one. Anything running as you
on this Mac can use it too, so it opens this one computer only."""
COPY_UNIX = """  ssh-copy-id -i ~/.ssh/strixfold-{name}.pub -o PreferredAuthentications=keyboard-interactive,password {login}

(that line asks for the computer's password once)"""
COPY_WINDOWS = """and copy the one line in ~/.ssh/strixfold-{name}.pub into that PC's C:\\Users\\{user}\\.ssh\\authorized_keys, by hand
or by any copy you trust. For an administrator account it goes in C:\\ProgramData\\ssh\\administrators_authorized_keys
instead, which the PC's ssh reads only when Administrators and SYSTEM alone can (in PowerShell, as administrator):

  icacls C:\\ProgramData\\ssh\\administrators_authorized_keys /inheritance:r /grant Administrators:F /grant SYSTEM:F
"""


def plain(x, n=200):
    return re.sub(r"[^ -~]+", " ", str(x)).strip()[:n]


def conf_path():
    import halo_control
    return os.path.realpath(os.path.join(halo_control.CONF_DIR, "config.json"))


def computers():
    """The computers besides this Mac, as Settings lists them (no ssh options or keys)."""
    saved = _saved()
    return [{"id": k, "name": v.get("name"), "host": v.get("host"), "shell": v.get("shell"), "note": v.get("note") or "",
             "mine": k in saved} for k, v in list(chat_direct.TARGETS.items()) if k != "mac"]


def _saved():
    try:
        with open(conf_path()) as f:
            return set((json.load(f).get("direct_targets") or {}))
    except (OSError, ValueError, AttributeError):
        return set()


def check(name, host, shell, note=""):
    """(name, host, shell, note) as given, cleaned; ValueError in words."""
    name, note = plain(name, 40), plain(note, 300)
    host = str(host or "").strip()
    if not name:
        raise ValueError("Give the computer a name.")
    if not HOST.fullmatch(host) or host.count("@") > 1 or host.endswith("@"):
        raise ValueError("Its address is a name from ~/.ssh/config, or user@address.")
    if shell not in SHELLS:
        raise ValueError("Its shell is bash or PowerShell.")
    return name, host, shell, note


def test(host, shell, timeout=25, ssh_options=()):
    """{"ok", "words", "help"?}: one harmless command there over ssh, with the run's own options (and a listed computer's
    own ssh_options), as Direct access will run them."""
    _, host, shell, _ = check("x", host, shell)
    name, nonce = secrets.token_hex(8), secrets.token_hex(12)  # the files' name, and what is in them
    marks = [os.path.expanduser(x) + name for x in HERE]
    for mark in marks:
        fd = os.open(mark, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)  # readable by any account here
        with os.fdopen(fd, "w") as f:
            f.write(nonce + "\n")
    try:
        p = subprocess.run([SSH, *SSH_OPTS, *ssh_options, host, probe(shell, name)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "words": f"{host} didn't answer within {timeout} s. Check the name or the address, and that it is on."}
    except OSError as e:
        return {"ok": False, "words": f"ssh couldn't run here ({plain(e, 80)})."}
    finally:
        for mark in marks:
            os.unlink(mark)
    out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    lines = [x.strip() for x in out.splitlines()]
    last = plain((err.strip().splitlines() or ["no answer"])[-1], 160)
    if nonce in out:
        return {"ok": False, "words": f"{host} is this Mac, or a machine that shares its folders (a VM here). Direct access on "
                                      "this Mac is off: give it another computer."}
    if p.returncode == 0 and lines[:1] == ["strixfold-ok"]:
        system = plain(lines[1], 80) if len(lines) > 1 else ""
        return {"ok": True, "words": f"Connected to {host}" + (f": {system}." if system else ".")}
    if p.returncode == 0 and lines[:1] == ["strixfold-lacks"]:
        return {"ok": False, "words": f"{host} answered, but it lacks bash, base64 or setsid, which Direct's commands need "
                                      "(a Mac, or a very small system)."}
    if "ermission denied" in err or "Too many authentication failures" in err:
        g = _ssh_g(host, ssh_options)
        address = plain(g.get("hostname") or host.rpartition("@")[2], 120)
        user = plain(g.get("user") or host.rpartition("@")[0] or "USER", 60)
        port = g.get("port") or "22"
        name = re.sub(r"[^a-z0-9]+", "-", (host.rpartition("@")[2] if "@" in host else host).lower()).strip("-")[:20] or "lab"
        login = (f"-p {plain(port, 6)} " if port != "22" else "") + f"{user}@{address}"
        copy = (COPY_WINDOWS if shell == "PowerShell" else COPY_UNIX).format(name=name, login=login, user=user)
        return {"ok": False, "words": f"{host} is there, but ssh can't log in to it by itself ({last}).",
                "help": KEY_HELP.format(name=name, address=address, user=user, copy=copy,
                                        port=f"\n      Port {plain(port, 6)}" if port != "22" else "")}
    if "ost key" in err:
        return {"ok": False, "words": f"This Mac doesn't know {host}'s key yet, or it changed ({last}). Log in once by hand in "
                                      f"Terminal and look at the key it shows: ssh {host}"}
    if p.returncode == 0 or p.returncode == 127 or "not recognized" in out + err:
        return {"ok": False, "words": f"{host} answered, but not as {shell}. Choose the other shell."}
    return {"ok": False, "words": f"ssh can't reach {host} ({last}). Check the name or the address, and that it is on."}


def test_listed(cid):
    """A listed computer's test, with its own ssh options (a pinned key or known hosts)."""
    spec = chat_direct.TARGETS.get(cid)
    if not spec or cid == "mac":
        raise ValueError("No such computer.")
    return test(spec["host"], spec.get("shell") or "bash", ssh_options=tuple(spec.get("ssh_options") or ()))


def _ssh_g(host, ssh_options=()):
    try:
        out = subprocess.run([SSH, *ssh_options, "-G", host], stdin=subprocess.DEVNULL, capture_output=True, timeout=10).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(x.split(" ", 1) for x in out.splitlines() if " " in x)


def _write(update):
    """config.json with update(cfg) applied; the rest of it, its place (a link) and its mode stay."""
    p = conf_path()
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    cfg, mode = {}, 0o600
    if os.path.exists(p):
        with open(p) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError(f"{p} can't be read as settings: mend it first")
        mode = os.stat(p).st_mode & 0o777
    if cfg.get("direct_targets") is not None and not isinstance(cfg.get("direct_targets"), dict):
        raise ValueError(f"{p}'s direct_targets isn't a list of computers: mend it first")
    update(cfg)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".config-")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(tmp, mode)
    os.replace(tmp, p)


def add(name, host, shell, note=""):
    """A computer, after a passing connection test: in config.json and in this run's list at once. (its id, the test)
    An id is never used twice, so a chat set to a removed computer never runs on a new one of the same name."""
    name, host, shell, note = check(name, host, shell, note)
    t = test(host, shell)
    if not t["ok"]:
        return None, t
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "computer"
    spec = {"name": name, "host": host, "shell": shell, **({"note": note} if note else {})}
    with MU:
        taken = set(chat_direct.TARGETS) | _saved() | {"mac"}
        cid = f"{base}-{secrets.token_hex(2)}"
        while cid in taken:
            cid = f"{base}-{secrets.token_hex(2)}"
        try:
            _write(lambda cfg: cfg.__setitem__("direct_targets", dict(cfg.get("direct_targets") or {}, **{cid: spec})))
        except OSError as e:
            raise ValueError(f"config.json couldn't be written ({type(e).__name__})")
        chat_direct.TARGETS[cid] = spec
    return cid, t


def remove(cid):
    """A computer added here (config.json's); this Mac and computers named in code stay. A command running there stops."""
    with MU:
        if cid == "mac" or cid not in _saved():
            raise ValueError("Only a computer added in Settings can be removed here.")
        try:
            _write(lambda cfg: (cfg.get("direct_targets") or {}).pop(cid, None))
        except OSError as e:
            raise ValueError(f"config.json couldn't be written ({type(e).__name__})")
        chat_direct.TARGETS.pop(cid, None)
