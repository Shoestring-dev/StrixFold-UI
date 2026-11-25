"""Lab chat and agent sessions into Studio's private store.

Once, at the app's start: Lab chat's SQLite moves from ~/.config/halo-control/chats.sqlite to <store>/lab/chats.sqlite
and each agent session's folder from ~/.config/halo-control/agents/<id> to <store>/agents/<id>. Each is copied first
(SQLite's own backup, so rows still in the write-ahead log come along), checked (row counts and a sha256 of every
table's rows in key order; every file's sha256), switched to, and only then is the old copy removed. A copy that does
not check out is discarded and the old place stays in use (said in the log, with counts only: no prompt, title or
output is ever logged). Python 3.9 standard library only.
"""
import hashlib
import os
import shutil
import sqlite3

TABLES = {"chats": "id", "messages": "id"}


def table_digest(db, table, key):
    """(rows, sha256 of every row in key order) of one table."""
    h = hashlib.sha256()
    n = 0
    for row in db.execute(f"SELECT * FROM {table} ORDER BY {key}"):  # noqa: S608 - fixed table and key names
        h.update(repr(tuple(row)).encode())
        n += 1
    return n, h.hexdigest()


def digests(path):
    db = sqlite3.connect(path)
    try:
        have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: table_digest(db, t, k) for t, k in TABLES.items() if t in have}
    finally:
        db.close()


def remove_db(path):
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        if os.path.exists(p):
            os.unlink(p)


def counts(d):
    return ", ".join(f"{v[0]} {k}" for k, v in sorted(d.items()))


def move_lab(old, new, log=lambda m: None):
    """Lab chat's database from `old` to `new` (700 dir, 600 file). The path to use: `new` once it is there and checks
    out, else `old`. A move cut short after the switch (both there): the old copy goes when it holds the same rows,
    else both stay and the log says so (counts only)."""
    if not os.path.exists(old):
        return new
    if os.path.exists(new):  # an earlier move stopped between the switch and the removal
        a, b = digests(old), digests(new)
        if a == b:
            remove_db(old)
            log(f"lab chat: removed the old copy an interrupted move left ({counts(a)}, the same as the private store's)")
        else:
            log(f"lab chat: an old copy outside the private store differs from it ({counts(a)} there, {counts(b)} in the "
                "store); both kept, the store's in use")
        return new
    os.makedirs(os.path.dirname(new), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(new), 0o700)
    part = new + ".part"
    for p in (part, part + "-wal", part + "-shm"):
        if os.path.exists(p):
            os.unlink(p)
    src = sqlite3.connect(old)
    dst = sqlite3.connect(part)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")  # one file: no -wal or -shm left beside the copy
    finally:
        dst.close()
        src.close()
    for p in (part + "-wal", part + "-shm"):
        if os.path.exists(p):
            os.unlink(p)
    os.chmod(part, 0o600)
    a, b = digests(old), digests(part)
    if a != b:
        remove_db(part)
        log(f"lab chat: the copy into the private store did not check out ({counts(a)}); kept where it was")
        return old
    os.replace(part, new)
    remove_db(old)
    log(f"lab chat: moved into the private store ({counts(a)})")
    return new


def file_digests(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            if os.path.islink(p):
                out[os.path.relpath(p, root)] = "link:" + os.readlink(p)
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out[os.path.relpath(p, root)] = h.hexdigest()
    return out


def move_agents(old, new, log=lambda m: None):
    """Each agent session's folder from `old`/<id> to `new`/<id>: copied, checked file by file, then the old one
    removed. A session whose copy does not check out stays where it was (its id is listed in the log). The ids moved."""
    os.makedirs(new, mode=0o700, exist_ok=True)
    os.chmod(new, 0o700)
    if not os.path.isdir(old):
        return []
    moved, kept, cleaned = [], [], []
    for sid in sorted(os.listdir(old)):
        src, dst = os.path.join(old, sid), os.path.join(new, sid)
        if not os.path.isdir(src) or os.path.islink(src):
            continue
        if os.path.exists(dst):  # an earlier move stopped between the switch and the removal
            if file_digests(src) == file_digests(dst):
                shutil.rmtree(src)
                cleaned.append(sid)
            else:
                kept.append(sid)
            continue
        part = dst + ".part"
        shutil.rmtree(part, ignore_errors=True)
        shutil.copytree(src, part, symlinks=True)
        if file_digests(src) != file_digests(part):
            shutil.rmtree(part, ignore_errors=True)
            kept.append(sid)
            continue
        for d, dirs, files in os.walk(part):
            os.chmod(d, 0o700)
        os.replace(part, dst)
        shutil.rmtree(src)
        moved.append(sid)
    if moved or kept or cleaned:
        log(f"agent sessions: {len(moved)} moved into the private store"
            + (f"; {len(cleaned)} old copies an interrupted move left removed (the same as the store's)" if cleaned else "")
            + (f"; {len(kept)} differ from the store's copy, both kept (the store's in use): {', '.join(kept)}" if kept else ""))
    try:
        os.rmdir(old)  # only when empty
    except OSError:
        pass
    return moved
