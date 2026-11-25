"""The deny-default Seatbelt (sandbox-exec) profile every sandboxed program here runs under: the chat's Full tools
(chat_full), the photo worker (look), and an add-on's own when one is present. It starts from
(deny default): reads of the system and the listed folders only, writes only to the writable ones, the secret folders
denied again, no LaunchServices, Keychain or Apple Events, and the network lines the caller gives (none by default)."""
import os

HOME = os.path.expanduser("~")
# read-only for the tests, as lead_tools: the language toolchains the user installs under the user's home
TOOLCHAINS = tuple(os.path.join(HOME, d) for d in (".pyenv", os.path.join(".local", "share", "uv", "python"), ".cargo", ".rustup", ".nvm",
                                                   ".bun", ".deno", ".volta", ".rbenv", "go"))
# denied again after the allows (the home folder as a whole is denied first): keys and logins, even inside an allowed folder
SECRET_DIRS = (".ssh", ".aws", ".config", ".gnupg", ".netrc", ".git-credentials", ".cousteau", ".claude", ".codex", ".docker",
               ".kube", ".npmrc", ".pypirc", "Library/Keychains")


def _ancestors(path):
    out, d = [], os.path.dirname(os.path.realpath(path))
    while d and d != "/":
        out.append(d)
        d = os.path.dirname(d)
    return out


# A sandboxed process asking LaunchServices to open an app gets that app started by launchd, outside the sandbox
# (found: `open -a` from the tests' sandbox started an app; Terminal with a script would run anything as the user).
# Every profile here denies the services an open goes through. The Apple Events path is already refused.
LAUNCH_DENY = ('(deny mach-lookup (global-name-regex #"^com[.]apple[.](coreservices[.]launchservicesd|lsd[.].*|'
               'coreservices[.]quarantine-resolver|backgroundtaskmanagement.*|xpc[.]smd|ScriptEditor.*)$"))')


def _ci(path):
    """A Seatbelt regex for one path, letters matched in either case (APFS is case-insensitive)."""
    out = []
    for ch in os.path.realpath(path):
        if ch.isalpha():
            out.append(f"[{ch.lower()}{ch.upper()}]")
        elif ch in ".^$*+?()[]{}|\\":
            out.append(f"[{ch}]" if ch not in "^]\\" else "\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# What a deny-default profile lets every sandboxed program read (start from (deny default), like the
# others', so a route nobody thought of stays shut): the system, the developer tools, Homebrew, and the few files
# the shell and xcrun read to find them. Never the home folder, /private/tmp or the user's temp folders.
SYSTEM_READS = ("/usr/lib", "/usr/libexec", "/usr/share", "/usr/bin", "/usr/sbin", "/bin", "/sbin", "/System", "/Library/Apple",
                "/Library/Developer/CommandLineTools", "/Library/Frameworks", "/Library/Preferences/com.apple.dt.Xcode.plist",
                "/Applications/Xcode.app", "/private/etc", "/private/var/select", "/private/var/db/xcode_select_link",
                "/private/var/db/dyld", "/private/var/db/timezone", "/opt/homebrew", "/usr/local")
# the only services a sandboxed program may look up: user, group and /etc/hosts answers. Nothing that starts a program,
# reads the Keychain or sends Apple Events.
SAFE_MACH = ("com.apple.system.opendirectoryd.libinfo",)


def strict_profile(writable, readable=(), guard=(), net=("(deny network*)",)):
    """A deny-default sandbox-exec profile:
    - programs may run (their children stay in this sandbox);
    - reads: SYSTEM_READS plus the given folders and the toolchains, with only a stat of the folders above them;
    - writes: only the writable folders, never a guarded folder's .git (any case);
    - the secret folders and any auth.json are denied again;
    - mach services: SAFE_MACH only (so no LaunchServices, launchd jobs, Keychain or Apple Events);
    - the network: the given lines (none by default)."""
    q = lambda p: '"' + os.path.realpath(p).replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
    sub = lambda ps: " ".join(f"(subpath {q(x)})" for x in ps if os.path.lexists(x))  # noqa: E731
    reads = list(writable) + list(readable) + [t for t in TOOLCHAINS if os.path.isdir(t)]
    above = sorted({a for x in reads for a in _ancestors(x)} | {a for x in SYSTEM_READS for a in _ancestors(x)} |
                   {"/private", "/var", "/etc", "/tmp", "/usr", "/opt", "/Applications", "/Library", "/Users"})  # (CLT python3's realpath)
    home = os.path.realpath(HOME)
    dev = '(literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (regex #"^/dev/fd/")'  # never another terminal's /dev/ttys*
    return "\n".join([
        "(version 1)", "(deny default)",
        "(allow process-exec process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)", "(allow ipc-posix-sem ipc-posix-shm)", "(allow file-ioctl)",
        f'(allow file-read* file-test-existence {sub(SYSTEM_READS)} {sub(reads)} (literal "/") (literal "/etc") (literal "/var") (literal "/tmp") '
        f'{dev} (literal "/dev/urandom") (literal "/dev/random"))',
        "(allow file-read-metadata file-test-existence " + " ".join(f"(literal {q(x)})" for x in above if os.path.lexists(x)) + ")",
        f"(allow file-write* {sub(writable)} {dev})",
        *[f'(deny file-write* (regex #"^{_ci(g)}/[.][gG][iI][tT](/|$)"))' for g in guard],  # the worktree's .git
        f'(deny file-read* file-write* {sub([os.path.join(home, d) for d in SECRET_DIRS])} (regex #"/auth\\.json$"))',
        "(allow mach-lookup " + " ".join(f'(global-name "{m}")' for m in SAFE_MACH) + ")",
        LAUNCH_DENY,  # kept, though (deny default) already refuses it
        *net]) + "\n"
