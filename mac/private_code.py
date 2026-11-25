"""The running code, checked before Private unlocks. Agents run as the same account and can edit ~/Code/halo-control;
this catches an edit made after the app started, and a careless one. It can't stop a deliberate change to the start
path before a restart (the launcher, the interpreter's own startup files, this check itself).

CodeCheck (made at app start):
  - HEAD and its commit time; every tracked file under mac/ and box/ with its blob id in HEAD's tree;
  - findings(): HEAD moved; a tracked file whose bytes (hashed in Python as git does: sha1 of "blob <size>\\0<bytes>")
    aren't its blob; `git ls-files -v` showing S (skip-worktree) or a lowercase tag (assume-unchanged); an untracked
    file under mac/ or box/ (not .DS_Store, not another Python's bytecode, and not what DEPLOY_COPY names); this
    app's own bytecode (cpython-39 .pyc) anywhere under them. Git runs with core.fsmonitor=false,
    core.untrackedCache=false, core.hooksPath=/dev/null, no user config;
  - github(): main's commit on GitHub (`git ls-remote` against GITHUB, outside the repo, with no user or repo config,
    so no url.insteadOf can redirect it); None when it can't be read ("not checked on GitHub").

The page's files (mac/ui) are read once at start (ui_snapshot) and the app serves only those bytes, so an edit after
start never reaches the page, where the user types the passcode and the private token lives. findings() also holds each
of them against HEAD's blob: a file changed before start and put back after still refuses Unlock.

Guard (installed at app start, first in sys.meta_path): a module is looked for in the standard library and the
system's folders first, so a planted mac/array.py never shadows `array`; from a user-writable folder (mac/, box/, the
user site-packages) only a file recorded at start with its sha256 loads, and a .py is compiled from the very bytes that
were hashed (no .pyc is read). Anything else is an ImportError.
"""
import hashlib
import importlib.abc
import importlib.machinery
import os
import subprocess
import sys
import time

GITHUB = None  # the repo whose main the running code should be; none: "not checked on GitHub"
try:
    from site_defaults import CONFIG as _SITE  # this setup's own repo (the open-source export leaves it out)
    GITHUB = _SITE.get("github_repo")
except ImportError:
    pass
GIT = "/usr/bin/git"
SAFE = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=/dev/null")
DIRS = ("mac", "box")
DEPLOY_COPY = ()  # folders whose files aren't the app's code
OWN_PYC = f"cpython-{sys.version_info[0]}{sys.version_info[1]}"  # this app's own bytecode tag (cpython-39)
# what the app process may load from the user's site-packages after start: nothing. numpy, pypdf and xlrd run only in
# child processes with their own venvs (transcribe_worker, agents_web/parse_child, xls_reader), so the guard refuses
# every user-site import after start and no compiled module (.so) there is ever loaded late (the .so gap). The
# user site's .pth files (setuptools' _distutils_hack) run at interpreter start: the start path.
USER_SITE_PACKAGES = ()


def _env(no_user_config=True):
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "GIT_TERMINAL_PROMPT": "0", "LANG": "C", "LC_ALL": "C"}
    if no_user_config:
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"  # ~/.gitconfig is the user's to change; the system's (root's) stays
    return env


def git(repo, *args, timeout=30, cwd=None):
    r = subprocess.run([GIT, *SAFE, *(("-C", repo) if repo else ()), *args], capture_output=True, timeout=timeout,
                       env=_env(), cwd=cwd or "/", stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return r.stdout


def blob_id(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class CodeCheck:
    def __init__(self, repo, github=None):
        self.repo = os.path.realpath(repo)
        self.head = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.landed = int(git(self.repo, "show", "-s", "--format=%ct", "HEAD").decode().strip() or 0)
        self.tree = {}
        for rec in git(self.repo, "ls-tree", "-r", "-z", "HEAD", "--", *DIRS).split(b"\0"):
            if rec:
                meta, path = rec.split(b"\t", 1)
                mode, kind, oid = meta.decode().split()
                if kind == "blob":
                    self.tree[path.decode()] = oid
        self._github = github  # tests pass a stand-in; None: ask GitHub
        self.started = time.time()
        self.ui = {}  # the page's files as served (ui_snapshot), relative to mac/ui

    def findings(self):
        """What makes the running code not the landed one, in plain words ([] when clean)."""
        out = []
        try:
            if git(self.repo, "rev-parse", "HEAD").decode().strip() != self.head:
                out.append("the checkout moved to another commit since StrixFold UI started")
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            out.append(f"git couldn't read the checkout ({e})")
        for rel, oid in self.tree.items():
            p = os.path.join(self.repo, rel)
            try:
                with open(p, "rb") as f:
                    if blob_id(f.read()) != oid:
                        out.append(f"{rel} was changed")
            except OSError:
                out.append(f"{rel} is missing")
        for rel, data in sorted(self.ui.items()):  # what the page is served: the bytes read at start
            path = f"mac/ui/{rel}"
            if self.tree.get(path) is None:
                out.append(f"{path} wasn't in git when StrixFold UI started")
            elif blob_id(data) != self.tree[path]:
                out.append(f"{path} was changed when StrixFold UI started")
        try:
            for rec in git(self.repo, "ls-files", "-v", "-z", "--", *DIRS).split(b"\0"):
                if rec:
                    tag, path = rec[:1].decode(), rec[2:].decode()
                    if tag == "S" or tag.islower():
                        out.append(f"{path} is marked for git to look past")
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            out.append(f"git couldn't list the files ({e})")
        for d in DIRS:
            for dp, dn, fn in os.walk(os.path.join(self.repo, d)):
                for f in fn:
                    rel = os.path.relpath(os.path.join(dp, f), self.repo)
                    if rel in self.tree or f == ".DS_Store" or rel.startswith(DEPLOY_COPY):
                        continue
                    if f.endswith(".pyc") and os.path.basename(dp) == "__pycache__" and f".{OWN_PYC}." not in f:
                        continue  # another Python's bytecode: not this app's
                    out.append(f"{rel} is there but not in git" + (" (Python's own cache: delete mac's __pycache__ folders)"
                                                                   if f.endswith(".pyc") else ""))
        return out

    def github(self):
        """main's commit on GitHub, or None when it can't be read."""
        if self._github is not None:
            return self._github()
        if not GITHUB:
            return None
        try:
            out = git(None, "ls-remote", GITHUB, "refs/heads/main", timeout=15).decode().split()
            return out[0] if out else None
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return None

    def view(self):
        """For the Unlock sheet: "Running code: <commit>, <landed time>, clean" and whether Private may unlock."""
        problems = self.findings()
        gh = self.github()
        gh_words = "not checked on GitHub" if gh is None else "on GitHub's main" if gh == self.head else "not GitHub's main"
        if gh is not None and gh != self.head:
            problems = problems + ["the running commit isn't GitHub's main"]
        when = time.strftime("%d %b %H:%M", time.localtime(self.landed)) if self.landed else "unknown"
        return {"commit": self.head[:12], "landed": when, "clean": not problems, "github": gh_words, "problems": problems[:12],
                "line": f"Running code: {self.head[:12]}, landed {when}, {'clean' if not problems else 'NOT clean'} ({gh_words})"}


# ---------------------------------------------------------------- the import guard
def _writable(p):
    p = p or os.getcwd()
    try:
        return os.access(p if os.path.isdir(p) else os.path.dirname(p), os.W_OK)
    except OSError:
        return True


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _HashedLoader(importlib.machinery.SourceFileLoader):
    """A .py from a user-writable folder: compiled from the bytes whose sha256 matched the start's, never from a .pyc."""

    def __init__(self, fullname, path, want):
        super().__init__(fullname, path)
        self.want = want

    def get_code(self, fullname):
        data = self.get_data(self.path)
        if hashlib.sha256(data).hexdigest() != self.want:
            raise ImportError(f"{fullname}: {self.path} changed since StrixFold UI started")
        return compile(data, self.path, "exec", dont_inherit=True)


class Guard(importlib.abc.MetaPathFinder):
    def __init__(self, allowed):
        self.allowed = dict(allowed)  # realpath -> sha256, files from user-writable folders recorded at start
        self.refused = []

    def find_spec(self, name, path=None, target=None):
        search = list(path) if path is not None else list(sys.path)
        safe = [p for p in search if not _writable(p)]
        spec = importlib.machinery.PathFinder.find_spec(name, safe) if safe else None
        if spec is not None:
            return spec  # the standard library and the system's folders first
        risky = [p for p in search if _writable(p)]
        spec = importlib.machinery.PathFinder.find_spec(name, risky) if risky else None
        if spec is None:
            return None
        origin = spec.origin if spec.origin not in (None, "namespace") else None
        if origin is None:
            self.refused.append(name)
            raise ImportError(f"{name}: a folder package from a user-writable folder isn't loaded after start")
        real = os.path.realpath(origin)
        want = self.allowed.get(real)
        if want is None:
            self.refused.append(name)
            raise ImportError(f"{name}: {origin} wasn't part of StrixFold UI when it started")
        if real.endswith(".py"):
            spec.loader = _HashedLoader(name, origin, want)
            spec.cached = None
        elif _sha(real) != want:
            self.refused.append(name)
            raise ImportError(f"{name}: {origin} changed since StrixFold UI started")
        return spec


def manifest(repo, check=None, packages=USER_SITE_PACKAGES):
    """realpath -> sha256 of every file a user-writable folder may still load after start: the tracked .py files under
    mac/ and box/, and the files of the user site-packages the app uses (imported here first)."""
    out = {}
    tree = check.tree if check is not None else {}
    for rel in tree:
        if rel.endswith((".py", ".so")):
            p = os.path.realpath(os.path.join(repo, rel))
            if os.path.exists(p):
                out[p] = _sha(p)
    for name in packages:
        try:
            mod = __import__(name)
        except ImportError:
            continue
        top = os.path.dirname(os.path.realpath(getattr(mod, "__file__", "") or ""))
        if not top or not _writable(top):
            continue
        for dp, dn, fn in os.walk(top):
            for f in fn:
                if f.endswith((".py", ".so", ".dylib")):
                    p = os.path.realpath(os.path.join(dp, f))
                    out[p] = _sha(p)
    return out


CHECK = None
GUARD = None
UI_FILES = None  # the page's files, read at start: {path relative to mac/ui: bytes}


def ui_snapshot(ui):
    """Every file under the page's folder, read once (.DS_Store aside)."""
    out = {}
    for dp, dn, fn in os.walk(ui):
        for f in fn:
            if f != ".DS_Store":
                with open(os.path.join(dp, f), "rb") as fh:
                    out[os.path.relpath(os.path.join(dp, f), ui)] = fh.read()
    return out


def install(repo, fake=False):
    """At app start, after the start's own imports: the code check, the page's files read once, and the guard first in
    sys.meta_path. The fake app never asks GitHub (its checkout is a test branch): "not checked on GitHub"; the local
    checks still hold."""
    global CHECK, GUARD, UI_FILES
    try:
        CHECK = CodeCheck(repo, github=(lambda: None) if fake else None)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        CHECK = None  # no git: Private can't unlock (the route says why)
    UI_FILES = ui_snapshot(os.path.join(repo, "mac", "ui"))
    if CHECK is not None:
        CHECK.ui = UI_FILES
    # Private's own photo and PDF reader: the Swift source as read now, usable only if it is HEAD's (private_reader)
    import private_reader
    try:
        with open(os.path.join(repo, "mac", "halo_extract.swift"), "rb") as f:
            src = f.read()
    except OSError:
        src = b""
    private_reader.READER = private_reader.Reader(src, CHECK is not None and CHECK.tree.get("mac/halo_extract.swift") == blob_id(src))
    GUARD = Guard(manifest(repo, CHECK))
    sys.meta_path.insert(0, GUARD)
    sys.dont_write_bytecode = True
    return CHECK
