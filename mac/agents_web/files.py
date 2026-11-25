"""The files tool: one run's workspace folder, and nothing outside it.

Paths are relative to the workspace. Refused: absolute paths, "~", "..", empty or NUL paths, any symlink on the
way, and key or credential file names (.env, id_*, *.pem, *.key, auth.json, a .cousteau folder and the like: the user's
security baseline, item 1). Every call walks from the workspace's own descriptor one component at a time with O_NOFOLLOW (openat-style
dir_fd calls), so a symlink planted or swapped mid-call cannot lead outside either. Folders are 700, files 600.
Caps: 20 MB a file, 200 MB a workspace.
"""
import errno
import functools
import os
import secrets
import stat

from .decision import FilesDenied

import re  # noqa: E402

KEY_NAME = re.compile(r"^\.env(\..*)?$|^id_[A-Za-z0-9_-]+(\.pub)?$|\.(pem|key|p12|pfx)$|^\.netrc$|^credentials(\.json)?$|"
                      r"^auth\.json$|^\.cousteau$", re.I)  # auth.json: Codex's live sign-in, kept in ~/.cousteau/accounts
MAX_FILE = 20 << 20
MAX_TOTAL = 200 << 20
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _link_error(e):
    return e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK)


def _refusing(fn):
    """Any other OS error (a folder where a file was expected, a full disk) is a FilesDenied too."""
    @functools.wraps(fn)
    def run(*a, **kw):
        try:
            return fn(*a, **kw)
        except FilesDenied:
            raise
        except OSError as e:
            raise FilesDenied("The file operation failed (%s)." % (e.strerror or type(e).__name__))
    return run


class Workspace:
    def __init__(self, root):
        root = os.path.abspath(root)
        os.makedirs(root, mode=0o700, exist_ok=True)
        st = os.lstat(root)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise FilesDenied("The workspace is not a plain folder.")
        os.chmod(root, 0o700)
        self.root = root

    # ---------------------------------------------------------------- the walk
    def _parts(self, rel, allow_root=False):
        if not isinstance(rel, str) or "\x00" in rel or len(rel) > 1024:
            raise FilesDenied("The path is not valid.")
        if rel.startswith(("/", "~", "\\")) or (len(rel) > 1 and rel[1] == ":"):
            raise FilesDenied("Absolute paths are not allowed; use a path inside the workspace.")
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise FilesDenied("'..' is not allowed in a path.")
        if any(len(p.encode()) > 255 for p in parts):
            raise FilesDenied("A name in the path is too long.")
        if any(KEY_NAME.search(p) for p in parts):  # security baseline item 1: never a key file, even one inside
            raise FilesDenied("Key and credential files (.env, id_*, *.pem, *.key, auth.json, .cousteau) are never read or written.")
        if not parts and not allow_root:
            raise FilesDenied("The path is empty.")
        return parts

    def _walk(self, parts, create=False):
        """A descriptor of the folder parts names, opened from the root with O_NOFOLLOW at every step."""
        try:
            fd = os.open(self.root, DIR_FLAGS)
        except OSError:
            raise FilesDenied("The workspace is missing or not a plain folder.")
        try:
            for p in parts:
                try:
                    nfd = os.open(p, DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise FilesDenied("No such folder in the workspace.")
                    os.mkdir(p, 0o700, dir_fd=fd)
                    nfd = os.open(p, DIR_FLAGS, dir_fd=fd)
                except OSError as e:
                    if _link_error(e):
                        raise FilesDenied("The path goes through a link or a file.")
                    raise
                os.close(fd)
                fd = nfd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_file(self, rel, create_dirs=False):
        parts = self._parts(rel)
        dfd = self._walk(parts[:-1], create=create_dirs)
        return dfd, parts[-1]

    def usage(self):
        total = 0
        for d, _, names in os.walk(self.root, followlinks=False):
            for n in names:
                try:
                    total += os.lstat(os.path.join(d, n)).st_size
                except OSError:
                    pass
        return total

    # ---------------------------------------------------------------- the calls
    @_refusing
    def read_bytes(self, rel, max_bytes=MAX_FILE):
        dfd, name = self._open_file(rel)
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            except FileNotFoundError:
                raise FilesDenied("No such file in the workspace.")
            except OSError as e:
                if _link_error(e):
                    raise FilesDenied("The path is a link.")
                raise
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise FilesDenied("The path is not a file.")
                return f.read(max_bytes)
        finally:
            os.close(dfd)

    def read_text(self, rel, max_bytes=2 << 20):
        return self.read_bytes(rel, max_bytes).decode("utf-8", "replace")

    @_refusing
    def write_bytes(self, rel, data):
        data = bytes(data)
        if len(data) > MAX_FILE:
            raise FilesDenied("A file may be at most 20 MB.")
        if self.usage() + len(data) > MAX_TOTAL:
            raise FilesDenied("The workspace is full (200 MB).")
        dfd, name = self._open_file(rel, create_dirs=True)
        tmp = ".tmp-" + secrets.token_hex(6)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    raise FilesDenied("The path is a folder.")
            except FileNotFoundError:
                pass
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)  # replaces a link itself, never its target
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
        finally:
            os.close(dfd)

    def write_text(self, rel, text):
        self.write_bytes(rel, str(text).encode("utf-8"))

    @_refusing
    def append_text(self, rel, text):
        data = str(text).encode("utf-8")
        dfd, name = self._open_file(rel, create_dirs=True)
        try:
            try:
                fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            except OSError as e:
                if _link_error(e):
                    raise FilesDenied("The path is a link.")
                raise
            with os.fdopen(fd, "ab") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise FilesDenied("The path is not a file.")
                if st.st_size + len(data) > MAX_FILE:
                    raise FilesDenied("A file may be at most 20 MB.")
                if self.usage() + len(data) > MAX_TOTAL:
                    raise FilesDenied("The workspace is full (200 MB).")
                f.write(data)
        finally:
            os.close(dfd)

    @_refusing
    def list(self, rel="."):
        fd = self._walk(self._parts(rel, allow_root=True))
        try:
            out = []
            for e in sorted(os.scandir(fd), key=lambda e: e.name):
                st = e.stat(follow_symlinks=False)
                kind = "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
                if e.name.startswith(".tmp-"):
                    continue
                out.append({"name": e.name, "type": kind, "size": st.st_size if kind == "file" else 0})
            return out
        finally:
            os.close(fd)

    @_refusing
    def exists(self, rel):
        parts = self._parts(rel)
        try:
            dfd = self._walk(parts[:-1])
        except FilesDenied as e:
            if "No such folder" in e.reason:
                return False
            raise
        try:
            os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        finally:
            os.close(dfd)

    @_refusing
    def delete(self, rel):
        dfd, name = self._open_file(rel)
        try:
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                raise FilesDenied("No such file in the workspace.")
            if stat.S_ISDIR(st.st_mode):
                raise FilesDenied("Only files can be deleted.")
            os.unlink(name, dir_fd=dfd)
        finally:
            os.close(dfd)
