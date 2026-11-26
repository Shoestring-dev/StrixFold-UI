"""The private store on the Mac, and who may open it: the passcode and paired devices.

The store is one folder (default ~/Pictures/StrixFoldUI; config.json "studio_root" or HALO_STUDIO_ROOT moves it, and
the tests and screenshots use ~/Pictures/StrixFoldUI-test or a temp dir): studio.sqlite (chats, messages, uploads,
devices, settings) and refs/ (attached files). Directories 700, files 600, re-checked at every start. Nothing here goes
to a log: the server logs no message, file name or thumbnail.

Access: the chats' pages, API and files need a device session, checked on every request (also on the LAN, where the
rest of the dashboard has no login). A session comes from the passcode (PBKDF2-SHA256, 200,000 rounds; set once, on
this Mac only); "Remember this device" makes it a paired device for 90 days, otherwise it ends after 12 hours idle.
Wrong passcodes are slowed per client (5 free, then 30 s doubling to 15 min) and, past 20 in an hour, refused from
anywhere but this Mac until it unlocks. File URLs carry random 128-bit ids and are served with no-store.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "box"))
import mediainfo  # noqa: E402
import filetext  # noqa: E402

TMUTIL = "/usr/bin/tmutil"


def exclude_from_backup(folder, tmutil=None):
    """A private folder is never backed up: it is kept out of Time Machine (a sticky exclusion that travels with the
    folder). The check is quick; adding the exclusion takes tmutil about ten seconds, so it runs beside the caller.
    Returns the thread doing it, or None when the folder is already excluded or tmutil is missing."""
    tmutil = tmutil or TMUTIL
    try:
        r = subprocess.run([tmutil, "isexcluded", folder], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "[Excluded]" in r.stdout:
        return None

    def add():
        try:
            subprocess.run([tmutil, "addexclusion", folder], capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            pass
    t = threading.Thread(target=add, name="tmutil", daemon=True)
    t.start()
    return t
import officetext  # noqa: E402
import private_store as _private  # noqa: E402  (Private chats: keys, the grant, Locked)

DEFAULT_ROOT = os.path.expanduser("~/Pictures/StrixFoldUI")
COOKIE = "halo_studio"
PBKDF2_ROUNDS = 200_000
SESSION_IDLE_S = 12 * 3600
PAIRED_S = 90 * 86400
UPLOAD_MAX = {"image": 40 << 20, "audio": 60 << 20, "video": 300 << 20, "text": 1 << 20}
PRIVATE_LEVELS = ("none", "box", "box_web")  # a Private chat's tools: its container folder is sealed on Toolbox
PRIVATE_NO_FULL = "Full access isn't offered in a Private chat: its files would sit unsealed on this Mac."
KINDS = ("chat",)  # a chat with one of the text models (halo_chat.py)
KIND = KINDS[0]  # a chat's kind when its row or its caller names none
EFFORTS = ("low", "high", "extra")
IDLE_LOCK_MINUTES = (0, 1, 5, 15, 30)

SETTINGS = {  # the app's own defaults
    "initials": "", "notify_mac": True,
    # on this Mac only: after this many minutes with no input in any StrixFold UI tab, the app locks and the window
    # blurs until the passcode; 0 = off. The phone and the tailnet page never lock this way.
    "idle_lock_min": 5,
}


def now():
    return time.time()


def as_text(data, name=""):
    """The bytes as text when they are a text file (filetext.decode_text: UTF-8/16/32 by their mark, UTF-8, then
    Windows-1252 or Mac Roman), else None."""
    return filetext.decode_text(data, name)


def new_id(prefix, n=16):
    return prefix + secrets.token_hex(n)


UPLOAD_ID = re.compile(r"u[0-9a-f]{32}")
UPLOAD_ID_IN = re.compile(r'"(u[0-9a-f]{32})"')


class OutboxGone(Exception):
    """A waiting message's row was gone when its turn came to write it (cancelled meanwhile): nothing was written."""


SCHEMA = """
            PRAGMA journal_mode=WAL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL, settings TEXT DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat_id TEXT, role TEXT, content TEXT DEFAULT '',
                created REAL, meta TEXT DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, chat_id TEXT, kind TEXT, type TEXT, mime TEXT, bytes INT,
                sha256 TEXT, width INT, height INT, duration_s REAL, name TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, hash TEXT UNIQUE, label TEXT, created REAL, last_seen REAL,
                remember INT, client TEXT);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, chat_id TEXT, content TEXT, meta TEXT DEFAULT '{}',
                created REAL);
            CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT, instructions TEXT DEFAULT '', created REAL,
                updated REAL);
"""
# added after the first version: (table, column and type)
COLUMNS = [("messages", "tgroup INT"), ("messages", "tversion INT DEFAULT 0"),
           ("messages", "hidden INT DEFAULT 0"), ("chats", f"kind TEXT DEFAULT '{KIND}'"), ("chats", "project_id TEXT"),
           ("chats", "model TEXT"), ("chats", "effort TEXT"), ("uploads", "chars INT"),
           ("chats", "cousteau TEXT"),  # a project's orchestrator thread (mac/cousteau.py): the project's id
           ("chats", "perm TEXT"), ("chats", "perm_opts TEXT"),  # a chat's permissions (mac/chat_tools.py)
           ("chats", "auto_approve INT DEFAULT 0"),
           ("chats", "private INT DEFAULT 0"), ("chats", "pkey TEXT"),  # a Private chat and its wrapped key (private_store)
           ("chats", "pwork TEXT"),  # a Private chat's sealed Toolbox folder: its version, sealed (private_workspace)
           ("uploads", "detail TEXT"),
           ("uploads", "orig TEXT"),  # a document's original kept beside its text (docs.KINDS: "pdf", "docx"...)
           ("uploads", "made INT DEFAULT 0")]  # a file a chat's model made (chat_docs), not one the user attached


class Store:
    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.mu = threading.RLock()
        for d in (self.root,
                  self.path("refs")):
            os.makedirs(d, mode=0o700, exist_ok=True)
        self.c = sqlite3.connect(self.path("studio.sqlite"), check_same_thread=False)
        self.c.executescript(SCHEMA)
        for table, col in COLUMNS:
            try:
                self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.c.commit()
        self.fix_modes()
        self.vault = _private.Vault(self)

    # ---------------------------------------------------------------- Private chats (private_store)
    def _ck(self, cid):
        """(chat key, True) for a Private chat (Locked without the grant), (None, False) for any other. KeyError: no chat."""
        r = self.one("SELECT private, pkey FROM chats WHERE id=?", (cid,))
        if not r:
            raise KeyError(cid)
        if not r.get("private"):
            return None, False
        return self.vault.chat_key(cid, r.get("pkey")), True

    def check_attachment(self, cid, uid):
        """A message's file stays on its side of Private: a Private chat's upload goes only in that chat's
        messages, and a Private chat takes only its own uploads. Read from the plain columns, nothing is opened;
        ValueError in words. An unknown id is left to the caller's own check."""
        r = self.one("SELECT chat_id FROM uploads WHERE id=?", (uid,))
        if not r:
            return
        owner = r.get("chat_id")
        if self.is_private(cid):
            if owner != cid:
                raise ValueError("A Private chat's message can carry only files sent in that chat.")
        elif owner and self.is_private(owner):
            raise ValueError("A file from a Private chat can't go in another chat.")

    def is_private(self, cid):
        r = self.one("SELECT private FROM chats WHERE id=?", (cid,))
        return bool(r and r.get("private"))

    def _upload_ck(self, u):
        """An upload's chat key when it belongs to a Private chat (Locked without the grant), else None."""
        if not u or not u.get("chat_id"):
            return None
        try:
            ck, priv = self._ck(u["chat_id"])
        except KeyError:
            return None
        return ck if priv else None

    def path(self, *p):
        return os.path.join(self.root, *p)

    def fix_modes(self):
        """Directories 700, files 600, everywhere in the store (also after a copy or a restore). An agent session's
        work folder keeps its files' own modes (a project's scripts stay runnable): only the folder is closed."""
        for d, dirs, files in os.walk(self.root):
            os.chmod(d, 0o700)
            rel = os.path.relpath(d, self.root).split(os.sep)
            if len(rel) == 2 and rel[0] == "agents":
                if "work" in dirs:
                    os.chmod(os.path.join(d, "work"), 0o700)
                    dirs.remove("work")
            for f in files:
                p = os.path.join(d, f)
                if not os.path.islink(p):
                    os.chmod(p, 0o600)

    def q(self, sql, args=()):
        with self.mu:
            cur = self.c.execute(sql, args)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            self.c.commit()
            return rows

    def x(self, sql, args=()):
        with self.mu:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur.lastrowid

    def changed(self, sql, args=()):
        """Rows an UPDATE or DELETE changed (a compare-and-set: 0 means another process got there first)."""
        with self.mu:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur.rowcount

    def one(self, sql, args=()):
        r = self.q(sql, args)
        return r[0] if r else None

    def kv(self, k, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set_kv(self, k, v):
        self.x("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))

    def set_kvs(self, d):
        """Several keys in one transaction (all or none)."""
        with self.mu:
            try:
                for k, v in d.items():
                    self.c.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))
                self.c.commit()
            except BaseException:
                self.c.rollback()
                raise

    def write_file(self, rel, data):
        """A file in the store, written whole (tmp + rename) with mode 600."""
        p = self.path(rel)
        tmp = p + ".tmp-" + secrets.token_hex(4)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        return p

    def thumb(self, src, rel):
        """A small JPEG (768 px at most) of an image, made by macOS's sips; 600 like the rest. False without sips."""
        out = self.path(rel)
        try:
            r = subprocess.run(["sips", "-Z", "768", "-s", "format", "jpeg", "-s", "formatOptions", "80", src, "--out", out],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode != 0 or not os.path.exists(out):
            return False
        os.chmod(out, 0o600)
        return True

    def to_jpeg(self, data):
        """HEIC/AVIF to JPEG with macOS's sips, through files in the private store (600) that are removed at once."""
        tmp = new_id("t")
        src = self.write_file(f"refs/{tmp}.in", data)
        out = self.path("refs", f"{tmp}.jpg")
        try:
            r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "90", src, "--out", out], capture_output=True, timeout=120)
            if r.returncode != 0 or not os.path.exists(out):
                raise ValueError("this photo format could not be read; export it as JPEG or PNG")
            with open(out, "rb") as f:
                return f.read()
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("this photo format could not be read; export it as JPEG or PNG")
        finally:
            for p in (src, out):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def remove_file(self, rel):
        try:
            os.unlink(self.path(rel))
        except OSError:
            pass

    # ---------------------------------------------------------------- settings
    def settings(self):
        s = dict(SETTINGS)
        s.update(self.kv("settings", {}))
        return s

    def update_settings(self, new):
        s = self.settings()
        s.update(check_settings(new, full=True))
        self.set_kv("settings", {k: v for k, v in s.items() if k in SETTINGS})
        return s

    # ---------------------------------------------------------------- chats
    def create_chat(self, title="", kind=KIND, project_id=None, model=None, effort=None,
                    cousteau=None, private=False):
        """A chat. private: a Private chat: its key wrapped under the PMK, its title
        sealed; needs the grant and Private unlocked (Locked otherwise)."""
        if kind not in KINDS:
            raise ValueError("kind: " + " or ".join(KINDS))
        if private and cousteau:
            raise ValueError("a Private chat is a chat")
        if project_id:
            self.project(project_id)
        cid = secrets.token_hex(8)
        t = now()
        title = (title or "New chat")[:200]
        pkey = None
        if private:
            pkey = self.vault.new_chat_key(cid)
            title = self.vault.enc(self.vault.chat_key(cid, pkey), cid, "chat", cid, "title", title)
        self.x("INSERT INTO chats (id, title, created, updated, kind, project_id, model, effort, cousteau, private, pkey) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (cid, title, t, t, kind, project_id or None, model, effort, cousteau or None, 1 if private else 0, pkey))
        return self.chat(cid)

    def chats(self, q="", kind=KIND, project=None, cousteau=None):
        """kind: one of KINDS, or all (the sidebar's Recents). A project's threads live on its page, not in
        Recents: they are listed only when asked for by the project's id (cousteau)."""
        where, args = [], []
        if kind in KINDS:
            where.append(f"COALESCE(c.kind, '{KIND}')=?")
            args.append(kind)
        if project:
            where.append("c.project_id=?")
            args.append(project)
        if cousteau:
            where.append("c.cousteau=?")
            args.append(cousteau)
        else:
            where.append("c.cousteau IS NULL")
        cols = (f"c.id, c.title, c.updated, COALESCE(c.kind, '{KIND}') AS kind, c.project_id, c.perm_opts, "  # perm_opts: the delete
                "COALESCE(c.private, 0) AS private")  # confirm's folder line (blanked for a Private chat: _titles)
        if q:
            like = "%" + q.replace("%", "").replace("_", "")[:100] + "%"
            where.append("(c.title LIKE ? OR m.content LIKE ?)")
            where.append("COALESCE(c.private, 0)=0")  # a Private chat is never found by search
            args += [like, like]
            return self._titles(self.q(f"SELECT DISTINCT {cols} FROM chats c LEFT JOIN messages m ON m.chat_id = c.id WHERE "
                                       + " AND ".join(where) + " ORDER BY c.updated DESC LIMIT 500", tuple(args)))
        return self._titles(self.q(f"SELECT {cols} FROM chats c" + (" WHERE " + " AND ".join(where) if where else "")
                                   + " ORDER BY c.updated DESC LIMIT 500", tuple(args)))

    def _titles(self, rows):
        """A Private chat's title in a list: its own with the grant, else "Private chat"."""
        for r in rows:
            r["private"] = bool(r.get("private"))
            if r["private"]:
                r["perm_opts"] = None  # a Private chat has no tools and no folder; its stored value is never listed
                try:
                    ck, _ = self._ck(r["id"])
                    r["title"] = self.vault.dec(ck, r["id"], "chat", r["id"], "title", r["title"])
                except (_private.Locked, KeyError):
                    r["title"] = "Private chat"
        return rows

    def update_chat(self, cid, title=None, project_id=False, model=None,
                    effort=None):
        """A chat's title, project (None takes it out), model (a chat profile) or effort; False/None leaves one as is."""
        self.chat(cid)
        if title is not None:
            self.rename_chat(cid, title)
        if project_id is not False:
            if project_id:
                self.project(project_id)
            self.x("UPDATE chats SET project_id=? WHERE id=?", (project_id or None, cid))
        if model is not None:
            self.x("UPDATE chats SET model=? WHERE id=?", (str(model)[:60], cid))
        if effort is not None:
            if effort not in EFFORTS:
                raise ValueError("effort: low, high or extra")
            self.x("UPDATE chats SET effort=? WHERE id=?", (effort, cid))
        return self.chat(cid)

    def set_perm(self, cid, perm, opts=None):
        """A chat's permissions (chat_tools.LEVELS, checked by chat_tools.check_perm) and their options (JSON). A
        Private chat takes No tools or the sealed container levels, never Full access: its
        files would sit unsealed on this Mac (P2)."""
        self.chat(cid)
        if self.is_private(cid) and (perm or "none") not in PRIVATE_LEVELS:
            raise ValueError(PRIVATE_NO_FULL)
        self.x("UPDATE chats SET perm=?, perm_opts=?, auto_approve=0 WHERE id=?", (perm, json.dumps(opts or {}), cid))
        return self.chat(cid)

    def set_auto_approve(self, cid, enabled):
        self.chat(cid)  # includes the private-chat access check
        self.x("UPDATE chats SET auto_approve=? WHERE id=?", (int(bool(enabled)), cid))


    def private_workspace(self, cid):
        """(key, version) of a Private chat's sealed folder on Toolbox. The key is made from the chat's own
        key, so it is never stored and goes when the chat is deleted; the version (sealed in the row) is the newest
        Toolbox has shown, and an older folder is refused. Locked without the grant."""
        ck, priv = self._ck(cid)
        if not priv:
            raise ValueError("not a Private chat")
        wk = hmac.new(ck, b"halo private workspace v1 " + cid.encode(), hashlib.sha256).digest()
        r = self.one("SELECT pwork FROM chats WHERE id=?", (cid,))
        d = self.vault.dec_json(ck, cid, "chat", cid, "pwork", r.get("pwork")) if r and r.get("pwork") else {}
        if d.get("unreadable"):
            raise ValueError("this Private chat's folder record can't be read (it was changed on disk)")
        return wk, int(d.get("ver") or 0)

    def set_private_workspace_ver(self, cid, ver):
        """The newest version Toolbox's sealed folder has shown for this chat (it only goes up)."""
        ck, priv = self._ck(cid)
        if not priv:
            raise ValueError("not a Private chat")
        if int(ver) <= self.private_workspace(cid)[1]:
            return
        self.x("UPDATE chats SET pwork=? WHERE id=?", (self.vault.enc_json(ck, cid, "chat", cid, "pwork", {"ver": int(ver)}), cid))

    # ---------------------------------------------------------------- projects (folders of chats with shared instructions)
    def projects(self):
        return self.q("SELECT p.id, p.name, p.instructions, p.created, p.updated, (SELECT COUNT(*) FROM chats c WHERE c.project_id=p.id) AS chats "
                      "FROM projects p ORDER BY p.updated DESC")

    def project(self, pid):
        p = self.one("SELECT * FROM projects WHERE id=?", (pid,))
        if not p:
            raise KeyError(pid)
        return p

    def save_project(self, pid=None, name=None, instructions=None):
        t = now()
        if pid is None:
            name = str(name or "").strip()[:120]
            if not name:
                raise ValueError("name: 1 to 120 characters")
            pid = secrets.token_hex(8)
            self.x("INSERT INTO projects (id, name, instructions, created, updated) VALUES (?,?,?,?,?)",
                   (pid, name, str(instructions or "")[:20000], t, t))
            return self.project(pid)
        self.project(pid)
        if name is not None:
            name = str(name).strip()[:120]
            if not name:
                raise ValueError("name: 1 to 120 characters")
            self.x("UPDATE projects SET name=?, updated=? WHERE id=?", (name, t, pid))
        if instructions is not None:
            self.x("UPDATE projects SET instructions=?, updated=? WHERE id=?", (str(instructions)[:20000], t, pid))
        return self.project(pid)

    def delete_project(self, pid):
        """The project goes; its chats stay, out of any project."""
        self.project(pid)
        self.x("UPDATE chats SET project_id=NULL WHERE project_id=?", (pid,))
        self.x("DELETE FROM projects WHERE id=?", (pid,))

    def chat_rev(self, cid):
        """A fingerprint of what the page shows for a chat (messages and their growth, versions, waiting messages, the
        title), so an open page reloads it when it changes."""
        c = self.one("SELECT updated, title FROM chats WHERE id=?", (cid,))
        if not c:
            return None
        m = self.one("SELECT COUNT(*) AS n, MAX(id) AS mx, COALESCE(SUM(hidden), 0) AS h, "
                     "COALESCE(SUM(LENGTH(content) + LENGTH(meta)), 0) AS l FROM messages WHERE chat_id=?", (cid,))
        seen = [c, m]
        seen.append(self.one("SELECT COUNT(*) AS n, MAX(id) AS mx FROM outbox WHERE chat_id=?", (cid,)))
        g = (self.kv("goals") or {}).get(cid)  # its goal (chat_goal) changes without a message
        return hashlib.sha256(json.dumps(seen + ([g] if g else []), sort_keys=True).encode()).hexdigest()[:16]

    def list_rev(self):
        """The chat list's fingerprint (a chat added, renamed, removed or updated)."""
        r = self.one("SELECT COUNT(*) AS n, MAX(updated) AS u, COALESCE(SUM(LENGTH(title)), 0) AS t FROM chats")
        return hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()[:16]

    def chat(self, cid):
        c = self.one("SELECT * FROM chats WHERE id=?", (cid,))
        if not c:
            raise KeyError(cid)
        wrapped = c.pop("pkey", None)
        c.pop("pwork", None)
        c["private"] = bool(c.get("private"))
        if c["private"]:  # Locked without the grant and Private unlocked
            ck = self.vault.chat_key(cid, wrapped)
            c["title"] = self.vault.dec(ck, cid, "chat", cid, "title", c["title"])
        c["settings"] = json.loads(c["settings"] or "{}")
        c["messages"] = self.messages(cid)
        return c

    def messages(self, cid, visible=False):
        ck, priv = self._ck(cid) if self.one("SELECT 1 AS x FROM chats WHERE id=?", (cid,)) else (None, False)
        out = self.q("SELECT id, role, content, created, meta, tgroup, tversion, hidden FROM messages WHERE chat_id=?"
                     + (" AND hidden=0" if visible else "") + " ORDER BY id", (cid,))
        for m in out:
            if priv:
                m["content"] = self.vault.dec(ck, cid, "message", m["id"], "content", m["content"])
                m["meta"] = self.vault.dec_json(ck, cid, "message", m["id"], "meta", m["meta"])
            else:
                m["meta"] = json.loads(m["meta"] or "{}")
            m["hidden"] = bool(m["hidden"])
        return out

    def rename_chat(self, cid, title):
        self.chat(cid)
        title = str(title or "").strip()[:200]
        if not title:
            raise ValueError("title: 1 to 200 characters")
        ck, priv = self._ck(cid)
        self.x("UPDATE chats SET title=? WHERE id=?", (self.vault.enc(ck, cid, "chat", cid, "title", title) if priv else title, cid))
        return self.chat(cid)

    def delete_chat(self, cid):
        """The chat, its messages and its uploads. A Private chat needs the grant; its key goes first (crypto-shred,
        without opening it, so a damaged key deletes too), then every file, then a WAL checkpoint so no old page holds the
        key (secure_delete zeroes freed space in the file)."""
        r = self.one("SELECT private FROM chats WHERE id=?", (cid,))
        if not r:
            raise KeyError(cid)
        priv = bool(r.get("private"))
        if priv:
            self.vault.require()
            self.x("UPDATE chats SET pkey=NULL, title='' WHERE id=?", (cid,))
            self.vault.forget(cid)
        for u in self.q("SELECT id, type FROM uploads WHERE chat_id=?", (cid,)):
            self.remove_upload_files(u["id"], u["type"])
        self.x("DELETE FROM uploads WHERE chat_id=?", (cid,))
        self.x("DELETE FROM messages WHERE chat_id=?", (cid,))
        self.x("DELETE FROM outbox WHERE chat_id=?", (cid,))
        self.x("DELETE FROM chats WHERE id=?", (cid,))
        if priv:
            with self.mu:
                self.c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def add_message(self, cid, role, content, meta=None, version=None, from_outbox=None):
        """A message; version = (turn group, version index) when it belongs to a turn with versions. One transaction:
        a Private chat's row is written sealed (a placeholder insert gives the record id the seal is bound to, then the
        sealed fields, committed together). from_outbox: the waiting message it is (outbox.Outbox), deleted in the
        same transaction, so a waiting message is never lost (a Lock or a crash before this leaves it queued) and never
        written twice; OutboxGone, with nothing written, when that row is gone (cancelled meanwhile)."""
        t = now()
        g, v = version or (None, 0)
        ck, priv = self._ck(cid)
        ins = "INSERT INTO messages (chat_id, role, content, created, meta, tgroup, tversion) VALUES (?,?,?,?,?,?,?)"
        with self.mu:
            try:
                if from_outbox is not None and self.c.execute("DELETE FROM outbox WHERE id=?", (from_outbox,)).rowcount != 1:
                    raise OutboxGone("the waiting message was cancelled")
                if priv:
                    mid = self.c.execute(ins, (cid, role, "", t, "{}", g, v)).lastrowid
                    self.c.execute("UPDATE messages SET content=?, meta=? WHERE id=?",
                                   (self.vault.enc(ck, cid, "message", mid, "content", content or ""),
                                    self.vault.enc_json(ck, cid, "message", mid, "meta", meta), mid))
                else:
                    mid = self.c.execute(ins, (cid, role, content or "", t, json.dumps(meta or {}), g, v)).lastrowid
                self.c.execute("UPDATE chats SET updated=? WHERE id=?", (t, cid))
                self.c.commit()
            except BaseException:
                self.c.rollback()
                raise
        if not priv:  # a photo picked before its chat existed joins it (a Private chat only ever holds its own sealed files)
            self.claim_uploads(cid, meta)
        return mid

    def set_message(self, mid, content=None, meta=None):
        r = self.one("SELECT chat_id FROM messages WHERE id=?", (mid,))
        ck, priv = self._ck(r["chat_id"]) if r else (None, False)
        cid = r["chat_id"] if r else None
        if content is not None:
            self.x("UPDATE messages SET content=? WHERE id=?",
                   (self.vault.enc(ck, cid, "message", mid, "content", content) if priv else content, mid))
        if meta is not None:
            self.x("UPDATE messages SET meta=? WHERE id=?",
                   (self.vault.enc_json(ck, cid, "message", mid, "meta", meta) if priv else json.dumps(meta), mid))

    def message(self, mid):
        m = self.one("SELECT * FROM messages WHERE id=?", (mid,))
        if m:
            ck, priv = self._ck(m["chat_id"]) if m.get("chat_id") else (None, False)
            if priv:
                m["content"] = self.vault.dec(ck, m["chat_id"], "message", mid, "content", m["content"])
                m["meta"] = self.vault.dec_json(ck, m["chat_id"], "message", mid, "meta", m["meta"])
            else:
                m["meta"] = json.loads(m["meta"] or "{}")
        return m

    def drop_from(self, cid, mid):
        self.x("DELETE FROM messages WHERE chat_id=? AND id>=?", (cid, mid))

    # ---------------------------------------------------------------- versions of the last turn (edit, regenerate)
    def new_version(self, cid, user_mid):
        """Keep the turn that starts at user_mid as a version and hide it; (group, next index) for the new version."""
        rows = self.q("SELECT id, tgroup, tversion FROM messages WHERE chat_id=? AND id>=? AND hidden=0 AND role!='summary' ORDER BY id",
                      (cid, user_mid))  # a summary made during the turn covers older turns: it stays
        g = (rows[0]["tgroup"] or rows[0]["id"]) if rows else user_mid
        top = self.one("SELECT MAX(tversion) AS v FROM messages WHERE chat_id=? AND tgroup=?", (cid, g))["v"] or 0
        for r in rows:  # this turn's visible messages become a version of group g
            self.x("UPDATE messages SET tgroup=?, tversion=COALESCE(tversion, 0), hidden=1 WHERE id=?", (g, r["id"]))
            if r["tgroup"] is None:
                self.x("UPDATE messages SET tversion=? WHERE id=?", (top, r["id"]))
        return g, top + 1

    def show_version(self, cid, g, v):
        """Show version v of turn group g (the last turn only), hide its others."""
        if not self.one("SELECT 1 AS x FROM messages WHERE chat_id=? AND tgroup=? AND tversion=?", (cid, g, v)):
            raise KeyError(f"{g}/{v}")
        later = self.one("SELECT 1 AS x FROM messages WHERE chat_id=? AND tgroup IS NOT ? AND id > (SELECT MAX(id) FROM messages "
                         "WHERE chat_id=? AND tgroup=?) AND hidden=0 AND role='user'", (cid, g, cid, g))
        if later:
            raise ValueError("only the last turn's versions can be switched")
        self.x("UPDATE messages SET hidden=CASE WHEN tversion=? THEN 0 ELSE 1 END WHERE chat_id=? AND tgroup=?", (v, cid, g))

    # ---------------------------------------------------------------- messages waiting for the model (the outbox)
    def enqueue(self, cid, content, meta):
        self.chat(cid)
        for a in (meta or {}).get("attachments") or []:
            if isinstance(a, dict) and str(a.get("id") or "").startswith("u"):
                self.check_attachment(cid, a["id"])
        ck, priv = self._ck(cid)
        if priv:  # sealed like a message (the outbox never opens one without the grant)
            oid = self.x("INSERT INTO outbox (chat_id, content, meta, created) VALUES (?,?,?,?)", (cid, "", "{}", now()))
            self.x("UPDATE outbox SET content=?, meta=? WHERE id=?", (self.vault.enc(ck, cid, "outbox", oid, "content", content or ""),
                                                                      self.vault.enc_json(ck, cid, "outbox", oid, "meta", meta), oid))
        else:
            oid = self.x("INSERT INTO outbox (chat_id, content, meta, created) VALUES (?,?,?,?)", (cid, content or "", json.dumps(meta), now()))
        self.x("UPDATE chats SET updated=? WHERE id=?", (now(), cid))
        if not priv:
            self.claim_uploads(cid, meta)
        return oid

    def waiting(self, cid=None):
        """Messages waiting for the model. A Private chat's, without the grant: content "" and meta {"locked_private":
        true} (the outbox leaves it waiting; it is never opened)."""
        rows = self.q("SELECT * FROM outbox" + (" WHERE chat_id=?" if cid else "") + " ORDER BY id", (cid,) if cid else ())
        for r in rows:
            try:
                ck, priv = self._ck(r["chat_id"])
            except KeyError:
                ck, priv = None, False
            except _private.Locked:
                r["content"], r["meta"] = "", {"locked_private": True, "attachments": []}
                continue
            if priv:
                r["content"] = self.vault.dec(ck, r["chat_id"], "outbox", r["id"], "content", r["content"])
                r["meta"] = self.vault.dec_json(ck, r["chat_id"], "outbox", r["id"], "meta", r["meta"])
            else:
                r["meta"] = json.loads(r["meta"] or "{}")
        return rows

    def rewrite_waiting(self, oid, content, meta):
        """A waiting message's text and meta, replaced: a chat's team update grows while its lead is busy
        (chat_team). Never a Private chat's sealed row. Returns whether the row was still there."""
        r = self.one("SELECT chat_id FROM outbox WHERE id=?", (oid,))
        if not r:
            return False
        if self._ck(r["chat_id"])[1]:
            raise ValueError("a Private chat's waiting message is sealed")
        return bool(self.changed("UPDATE outbox SET content=?, meta=? WHERE id=?", (content or "", json.dumps(meta), oid)))

    def unqueue(self, oid):
        return self.changed("DELETE FROM outbox WHERE id=?", (oid,))

    # ---------------------------------------------------------------- uploads (attached files)
    def add_upload(self, data, name="", chat_id=None, text_ok=False, text_limit=None):
        """An image, audio or video file (the bytes decide the type). With text_ok (a chat's attachment) any file: read as
        text on this Mac, best effort (filetext.py: text, Office, PDF, archives; anything else by its details), at most
        text_limit characters; an image stays an image (a model that sees looks at it) with the text OCR finds in it
        beside it."""
        limit = min(text_limit or officetext.MAX_CHARS, officetext.MAX_CHARS)
        if chat_id and self.is_private(chat_id):  # sealed, read in memory only
            return self._add_private_upload(data, name, chat_id, limit)
        if not text_ok:
            return self.add_media(data, name, chat_id)
        if mediainfo.sniff(data) is None or data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 text's mark reads as an MP3 frame
            doc = filetext.read(data, name, limit)
            return self.keep_original(self.save_text(doc.text, name, chat_id, doc.detail), data)
        try:
            u = self.add_media(data, name, chat_id)
        except ValueError:  # not media after all (UTF-16 text can look like MP3 to a sniffer): read as any file
            doc = filetext.read(data, name, limit)
            return self.keep_original(self.save_text(doc.text, name, chat_id, doc.detail), data)
        if u["kind"] == "image":
            doc = filetext.read(data, name, limit, media_kind="image")
            self.write_file(f"refs/{u['id']}.txt", doc.text.encode("utf-8"))
            self.x("UPDATE uploads SET chars=?, detail=? WHERE id=?", (len(doc.text), doc.detail, u["id"]))
            return self.upload(u["id"])
        return u

    def add_media(self, data, name="", chat_id=None):
        """An image, audio or video file (the bytes decide), its identifying metadata stripped."""
        if mediainfo.sniff(data) in ("heic", "avif"):
            data = self.to_jpeg(data)  # a phone photo: as a JPEG, its metadata stripped below
        data = mediainfo.strip_metadata(data)  # location, camera and names out (EXIF, XMP, IPTC, text, udta, ID3)
        inf = mediainfo.info(data)
        kind = inf.get("kind")
        if kind not in UPLOAD_MAX:
            raise ValueError("an image (PNG, JPEG, WEBP, GIF), audio (MP3, WAV, M4A, OGG, FLAC) or video (MP4, MOV, WEBM) file")
        if len(data) > UPLOAD_MAX[kind]:
            raise ValueError(f"{kind} files up to {UPLOAD_MAX[kind] >> 20} MB")
        if inf.get("error"):
            raise ValueError("the file could not be read")
        uid = new_id("u")
        src = self.write_file(f"refs/{uid}.{inf['type']}", data)
        if kind == "image":
            self.thumb(src, f"refs/{uid}.thumb.jpg")
        self.x("INSERT INTO uploads (id, chat_id, kind, type, mime, bytes, sha256, width, height, duration_s, name, created) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (uid, chat_id, kind, inf["type"], inf["mime"], len(data), hashlib.sha256(data).hexdigest(), inf.get("width"),
                inf.get("height"), inf.get("duration_s"), os.path.basename(str(name or ""))[:120], now()))
        return self.upload(uid)

    def add_text_upload(self, data, name="", chat_id=None):
        """A text or code file for a chat (the models read text only): kept as UTF-8, its name for the model to see."""
        text = as_text(data, name)
        if text is None:
            raise ValueError("a text file (UTF-8)")
        if len(data) > UPLOAD_MAX["text"]:
            raise ValueError(f"text files up to {UPLOAD_MAX['text'] >> 20} MB")
        detail = None
        if officetext.ext(name) in (".csv", ".tsv"):
            rows = sum(1 for line in text.splitlines() if line.strip())
            detail = f"{rows:,} row{'' if rows == 1 else 's'}"
        return self.save_text(text, name, chat_id, detail)

    def save_text(self, text, name, chat_id, detail=None):
        """A chat attachment's text, as the model will read it (UTF-8), with a few words about it for its chip."""
        uid = new_id("u")
        raw = text.encode("utf-8")
        self.write_file(f"refs/{uid}.txt", raw)
        self.x("INSERT INTO uploads (id, chat_id, kind, type, mime, bytes, sha256, name, created, chars, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (uid, chat_id, "text", "txt", "text/plain; charset=utf-8", len(raw), hashlib.sha256(raw).hexdigest(),
                os.path.basename(str(name or ""))[:120] or "file.txt", now(), len(text), detail))
        return self.upload(uid)

    def keep_original(self, u, data):
        """A document's own file beside the text read from it (the chat's document tools fill it in,
        look at its pages, hand it to the container): refs/<id>.orig, 600, for a PDF or an Office file of at most
        docs.DOC_MAX. A PDF's page 1 becomes the chip's thumbnail and its chip says when it is a form. Best effort:
        the text upload stands whatever happens here."""
        import docs
        kind = docs.kind_of(data, u.get("name"))
        if not kind or len(data) > docs.DOC_MAX:
            return u
        uid = u["id"]
        try:
            self.write_file(f"refs/{uid}.orig", data)
        except OSError:
            return u
        detail = u.get("detail") or ""
        if kind == "pdf":
            more = docs.summary(data)
            if more:
                detail = f"{detail} · {more}" if detail else more
            jpg = docs.thumbnail(data)
            if jpg:
                self.write_file(f"refs/{uid}.thumb.jpg", jpg)
        self.x("UPDATE uploads SET orig=?, detail=? WHERE id=?", (kind, detail or None, uid))
        return self.upload(uid)

    def upload_orig(self, uid):
        """A document's original bytes (a Private chat's opened in memory: Locked without the grant). ValueError when
        it has none."""
        u = self.upload(uid)
        if not u.get("orig"):
            raise ValueError(f"{uid} has no original")
        ck = self._upload_ck(u)
        if ck is not None:
            with open(self.path("refs", f"{uid}.orig.hpv"), "rb") as f:
                return self.vault.dec_bytes(ck, u["chat_id"], uid, "orig", f.read())
        with open(self.path("refs", f"{uid}.orig"), "rb") as f:
            return f.read()

    def add_document(self, chat_id, data, name, made=True):
        """A file a chat's tools made (a PDF; an Office file from the container), kept as an attachment is: its text for
        the model, its original, and a thumbnail of a PDF's page 1 (sealed in a Private chat, no thumbnail there)."""
        import docs
        kind = docs.kind_of(data, name)
        if not kind:
            raise ValueError("a PDF or an Office file")
        if len(data) > docs.DOC_MAX:
            raise ValueError(f"files up to {docs.DOC_MAX >> 20} MB")
        nm = os.path.basename(str(name or ""))[:120] or f"document.{kind}"
        limit = officetext.MAX_CHARS
        if chat_id and self.is_private(chat_id):
            if kind != "pdf":
                raise ValueError("a Private chat keeps PDFs only")
            import private_reader
            ck, _ = self._ck(chat_id)

            def run(args, d, suffix, timeout):
                return private_reader.reader().run(args[0], d, *args[1:], timeout=timeout)
            try:
                doc = filetext.pdf(data, limit, run=run)
            except private_reader.ReaderError as e:
                raise ValueError(str(e)) from None
            text = doc.text if doc else ""
            detail = ((doc.detail if doc else "PDF") + " · " + (docs.summary(data, private=True) or "")).rstrip(" ·")
            uid = new_id("u")
            self.write_file(f"refs/{uid}.txt.hpv", self.vault.enc_bytes(ck, chat_id, uid, "text", text.encode("utf-8")))
            self.write_file(f"refs/{uid}.orig.hpv", self.vault.enc_bytes(ck, chat_id, uid, "orig", data))
            self.x("INSERT INTO uploads (id, chat_id, kind, type, mime, bytes, name, created, chars, detail, orig, made) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (uid, chat_id, "text", "txt", "text/plain; charset=utf-8", len(text.encode("utf-8")),
                    self.vault.enc(ck, chat_id, "upload", uid, "name", nm), now(), len(text),
                    self.vault.enc(ck, chat_id, "upload", uid, "detail", detail), kind, 1 if made else 0))
            return self.upload(uid)
        doc = filetext.read(data, nm, limit)
        u = self.keep_original(self.save_text(doc.text, nm, chat_id, doc.detail), data)
        if made:
            self.x("UPDATE uploads SET made=1 WHERE id=?", (u["id"],))
        return self.upload(u["id"])

    def drop_made(self, uid):
        """A file a chat's tools made, gone (remade under the same name in the same answer: chat_docs). Only a made one."""
        u = self.one("SELECT id, type FROM uploads WHERE id=? AND made=1", (uid,))
        if u:
            self.remove_upload_files(uid, u["type"])
            self.x("DELETE FROM uploads WHERE id=?", (uid,))

    def documents(self, chat_id, messages=None):
        """The chat's documents with an original (the user's attachments and what its tools made), oldest first: the uploads
        it holds and the ones its messages carry (messages: the chat's, when the caller has them already)."""
        ids = [r["id"] for r in self.q("SELECT id FROM uploads WHERE chat_id=? AND orig IS NOT NULL ORDER BY created", (chat_id,))]
        for m in self.messages(chat_id) if messages is None else messages:
            for a in (m.get("meta") or {}).get("attachments") or []:
                if isinstance(a, dict) and a.get("id") not in ids and a.get("doc"):
                    ids.append(a["id"])
        out = []
        for uid in ids:
            try:
                u = self.upload(uid)
            except KeyError:
                continue
            if u.get("orig") and (u.get("chat_id") in (chat_id, None) or not self.is_private(u.get("chat_id") or "")):
                out.append(u)
        return out

    def set_upload_text(self, uid, text, detail):
        """What was read from a media upload (an audio or video file's transcript) for the chat's text models."""
        u = self.one("SELECT * FROM uploads WHERE id=?", (uid,))
        ck = self._upload_ck(u)
        if ck is not None:
            self.write_file(f"refs/{uid}.txt.hpv", self.vault.enc_bytes(ck, u["chat_id"], uid, "text", text.encode("utf-8")))
            self.x("UPDATE uploads SET chars=?, detail=? WHERE id=?",
                   (len(text), self.vault.enc(ck, u["chat_id"], "upload", uid, "detail", detail), uid))
            return self.upload(uid)
        self.write_file(f"refs/{uid}.txt", text.encode("utf-8"))
        self.x("UPDATE uploads SET chars=?, detail=? WHERE id=?", (len(text), detail, uid))
        return self.upload(uid)

    def _add_private_upload(self, data, name, chat_id, limit):
        """A Private chat's attachment (P3), read in memory and written only sealed (refs/<id>.hpv, its text as
        .txt.hpv); no sha256, size in pixels or temp file is kept:
          - a text file, as text;
          - a PDF, its text (PDFKit, OCR for scanned pages) through Private's own reader (private_reader: the file on
            its stdin, never on disk), and the PDF itself sealed beside it for the document tools (refs/<id>.orig.hpv);
          - a photo (JPEG, PNG, WebP, or a phone's HEIC made a JPEG in memory), its location and camera data stripped,
            and the text Vision finds in it, through the same reader.
        Never in Private v1: audio, video, Office files."""
        import private_reader
        ck, _ = self._ck(chat_id)
        kind = mediainfo.sniff(data)
        uid = new_id("u")
        nm = os.path.basename(str(name or ""))[:120] or "file"

        def run(args, d, suffix, timeout):  # filetext's helper call, through the reader, in memory
            return private_reader.reader().run(args[0], d, *args[1:], timeout=timeout)

        def keep_text(text, kind_, typ, mime, size, detail):
            self.write_file(f"refs/{uid}.txt.hpv", self.vault.enc_bytes(ck, chat_id, uid, "text", text.encode("utf-8")))
            self.x("INSERT INTO uploads (id, chat_id, kind, type, mime, bytes, name, created, chars, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (uid, chat_id, kind_, typ, mime, size, self.vault.enc(ck, chat_id, "upload", uid, "name", nm), now(), len(text),
                    self.vault.enc(ck, chat_id, "upload", uid, "detail", detail)))
        if kind is None or data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            if data[:5] == b"%PDF-":
                try:
                    doc = filetext.pdf(data, limit, run=run)
                except private_reader.ReaderError as e:
                    raise ValueError(str(e)) from None
                if doc is None:
                    raise ValueError("This PDF couldn't be read.")
                import docs
                more = docs.summary(data, private=True) if len(data) <= docs.DOC_MAX else ""
                keep_text(doc.text, "text", "txt", "text/plain; charset=utf-8", len(doc.text.encode("utf-8")),
                          f"{doc.detail} · {more}" if more else doc.detail)
                if len(data) <= docs.DOC_MAX:  # the PDF itself, sealed, for the chat's document tools (no thumbnail)
                    self.write_file(f"refs/{uid}.orig.hpv", self.vault.enc_bytes(ck, chat_id, uid, "orig", data))
                    self.x("UPDATE uploads SET orig='pdf' WHERE id=?", (uid,))
                return self.upload(uid)
            if officetext.ext(nm) in (".pdf", ".docx", ".xlsx", ".pptx", ".xls", ".doc", ".key", ".pages", ".numbers"):
                raise ValueError("Office files can't go in a Private chat yet: paste the text instead")
            text = as_text(data, nm)
            if text is None:
                raise ValueError("a Private chat takes text files, PDFs and photos (JPEG, PNG, WebP, HEIC)")
            text = text[:limit]
            keep_text(text, "text", "txt", "text/plain; charset=utf-8", len(text.encode("utf-8")), f"{len(text):,} characters")
            return self.upload(uid)
        if kind not in ("jpeg", "png", "webp", "heic"):
            raise ValueError("a Private chat takes photos as JPEG, PNG, WebP or HEIC; audio, video and other files can't go in one")
        try:
            if kind == "heic":  # a phone photo: a JPEG made in memory, upright, without its metadata
                got = private_reader.reader().see(data, 4096 * 4096, 0.9)
                if not got:
                    raise ValueError("This photo couldn't be read.")
                data, kind = got[0], "jpeg"
            data = mediainfo.strip_metadata(data)
            if len(data) > UPLOAD_MAX["image"]:
                raise ValueError(f"photos up to {UPLOAD_MAX['image'] >> 20} MB")
            doc = filetext.image(data, limit, run=run)
        except private_reader.ReaderError as e:
            raise ValueError(str(e)) from None
        self.write_file(f"refs/{uid}.hpv", self.vault.enc_bytes(ck, chat_id, uid, "data", data))
        text = doc.text if doc else ""
        detail = (doc.detail.split(" · ", 1)[-1] if doc and " · " in doc.detail else "no text found") if doc else "Photo"
        keep_text(text, "image", kind, f"image/{kind}", len(data), "Photo · " + detail)
        return self.upload(uid)

    def upload_bytes(self, uid):
        """An upload's own bytes: a Private chat's opened in memory (Locked without the grant), any other read as it is."""
        u = self.upload(uid)
        ck = self._upload_ck(u)
        if ck is not None:
            with open(self.path("refs", f"{uid}.hpv"), "rb") as f:
                return self.vault.dec_bytes(ck, u["chat_id"], uid, "data", f.read())
        with open(self.path("refs", f"{uid}.{u['type']}"), "rb") as f:
            return f.read()

    def upload_text(self, uid):
        """A chat attachment's text: a text upload's, or what was read from an image beside it."""
        u = self.upload(uid)
        ck = self._upload_ck(u)
        if ck is not None:
            p = self.path("refs", f"{uid}.txt.hpv")
            if not os.path.exists(p):
                raise ValueError(f"{uid} has no text")
            with open(p, "rb") as f:
                return self.vault.dec_bytes(ck, u["chat_id"], uid, "text", f.read()).decode("utf-8")
        p = self.path("refs", f"{uid}.txt")
        if u["kind"] != "text" and not os.path.exists(p):
            raise ValueError(f"{uid} has no text")
        with open(p, encoding="utf-8") as f:
            return f.read()

    def upload(self, uid):
        u = self.one("SELECT * FROM uploads WHERE id=?", (uid,))
        if not u:
            raise KeyError(uid)
        ck = self._upload_ck(u)  # a Private chat's: Locked without the grant
        if ck is not None:
            u["name"] = self.vault.dec(ck, u["chat_id"], "upload", uid, "name", u["name"])
            u["detail"] = self.vault.dec(ck, u["chat_id"], "upload", uid, "detail", u["detail"])
            u["private"] = True
        return u

    def remove_upload_files(self, uid, typ):
        """Every file an upload keeps under refs/: the file, its thumbnail, the text read from it, the copy a vision model
        was shown (halo_chat.picture) and its description by the Mac's vision model (look.py)."""
        for name in (f"{uid}.{typ}", f"{uid}.thumb.jpg", f"{uid}.txt", f"{uid}.see.jpg", f"{uid}.look.json",
                     f"{uid}.hpv", f"{uid}.txt.hpv", f"{uid}.thumb.hpv",
                     f"{uid}.see.hpv", f"{uid}.look.hpv", f"{uid}.orig", f"{uid}.orig.hpv"):  # (.hpv: a Private chat's, sealed)
            self.remove_file(f"refs/{name}")

    def claim_uploads(self, cid, meta):
        """The uploads a message (or a waiting message) lists belong to its chat from now on: a photo picked before its
        chat existed has no chat, and deleting the chat would leave it behind. One another chat holds is never moved."""
        ids = sorted({a["id"] for a in (meta or {}).get("attachments") or []
                      if isinstance(a, dict) and isinstance(a.get("id"), str) and UPLOAD_ID.fullmatch(a["id"])})
        if ids:
            self.x(f"UPDATE uploads SET chat_id=? WHERE chat_id IS NULL AND id IN ({','.join('?' * len(ids))})", (cid, *ids))

    def link_orphans(self):
        """Uploads with no chat (from before claim_uploads) go to the chat that lists them: the earliest message first,
        then a waiting message. It only links: nothing is deleted here, an upload nothing lists stays as it is, and a row
        that has a chat is never touched. Runs once at start, off the start path."""
        orphans = {r["id"] for r in self.q("SELECT id FROM uploads WHERE chat_id IS NULL")}
        owner = {}
        listed = ["SELECT chat_id, meta AS j FROM messages WHERE meta LIKE '%\"u%' ORDER BY id",
                  "SELECT chat_id, meta AS j FROM outbox WHERE meta LIKE '%\"u%' ORDER BY id"]
        for sql in listed:
            if len(owner) == len(orphans):
                break
            for r in self.q(sql):
                for uid in UPLOAD_ID_IN.findall(r["j"] or ""):
                    if uid in orphans and r["chat_id"]:
                        owner.setdefault(uid, r["chat_id"])
        return sum(self.changed("UPDATE uploads SET chat_id=? WHERE id=? AND chat_id IS NULL AND EXISTS (SELECT 1 FROM chats WHERE id=?)",
                                (cid, uid, cid)) for uid, cid in sorted(owner.items()))


def check_settings(s, full=False):
    """Settings a page sent, checked: the ones it may set, the rest left out."""
    out = {}
    if "initials" in s:
        v = str(s["initials"] or "").strip()
        if v and not re.fullmatch(r"[^\W\d_]{1,3}", v):
            raise ValueError("initials: one to three letters")
        out["initials"] = v.upper()
    if not full:
        return out
    if "notify_mac" in s:
        out["notify_mac"] = bool(s["notify_mac"])
    if "idle_lock_min" in s:
        if s["idle_lock_min"] not in IDLE_LOCK_MINUTES or isinstance(s["idle_lock_min"], bool):
            raise ValueError("lock when idle: off, or after 1, 5, 15 or 30 minutes")
        out["idle_lock_min"] = int(s["idle_lock_min"])
    return out


# ---------------------------------------------------------------- access: passcode and devices
class Auth:
    def __init__(self, store):
        self.s = store
        self.mu = threading.Lock()
        self.fails = {}       # client ip -> [count, blocked_until]
        self.recent = []      # times of wrong passcodes (the hour's count)
        self.lan_locked = False

    def is_set(self):
        return bool(self.s.kv("passcode"))

    def skipped(self):
        """No passcode, by the user's choice on the first screen: this Mac's browsers open the app without one."""
        return not self.is_set() and bool(self.s.kv("passcode_skipped"))

    def skip(self, local, key_ok, label, client, old_token=None):
        """Open without a passcode: on this Mac only, and only for a page opened with this run's key, which only this Mac
        account can read (halo_control.write_open_key). Private chats, the phone and the idle lock still need one. A
        device session for it, made under the same lock as setup's, so the two can't cross."""
        with self.mu:
            if self.is_set():
                raise PermissionError("a passcode is set already")
            if not local:
                raise PermissionError("skipping the passcode is for this Mac only (http://127.0.0.1:8711/)")
            if not key_ok:
                raise PermissionError("open StrixFold UI with bin/halo-control on this Mac to skip the passcode")
            if not self.s.kv("passcode_skipped"):
                self.s.set_kv("passcode_skipped", {"t": now()})
            return self.new_session(True, label, client, old_token)

    @staticmethod
    def _hash(passcode, salt, rounds=PBKDF2_ROUNDS):
        return hashlib.pbkdf2_hmac("sha256", passcode.encode(), bytes.fromhex(salt), rounds).hex()

    def setup(self, passcode, local, signed_in=False, key_ok=True):
        """The first passcode: from a page opened with this run's key (key_ok, from the routes), or after a Skip from a
        page that is signed in (Settings), so another account or another page can't set one and lock the user out;
        every session made before it ends."""
        with self.mu:
            if self.is_set():
                raise PermissionError("a passcode is set already; change it in Settings")
            if not local:
                raise PermissionError("set the passcode on this Mac (http://127.0.0.1:8711/) first")
            if self.s.kv("passcode_skipped") and not signed_in:
                raise PermissionError("set the passcode in Settings, on a page that is open")
            if not signed_in and not key_ok:
                raise PermissionError("open StrixFold UI with bin/halo-control on this Mac to set the passcode")
            self._store(passcode)
            self.s.x("DELETE FROM kv WHERE k=?", ("passcode_skipped",))
            self.s.x("DELETE FROM devices")

    def strengthen(self, passcode, rounds=1_000_000):
        """The passcode's check at `rounds` (Private's first unlock: a copied store can't test guesses against a cheaper
        check than the key's own). The caller has just verified the passcode."""
        rec = self.s.kv("passcode")
        if rec and int(rec.get("rounds", PBKDF2_ROUNDS)) < rounds:
            salt = secrets.token_hex(16)
            self.s.set_kv("passcode", dict(rec, salt=salt, hash=self._hash(str(passcode), salt, rounds), rounds=rounds, t=now()))

    @staticmethod
    def _check_new(passcode):
        passcode = str(passcode or "")
        if len(passcode) < _private.MIN_PASSCODE or len(passcode) > 128:
            raise ValueError(f"the passcode: {_private.MIN_PASSCODE} to 128 characters")
        return passcode

    def _record(self, passcode, rounds=PBKDF2_ROUNDS):
        salt = secrets.token_hex(16)
        return {"salt": salt, "hash": self._hash(passcode, salt, rounds), "rounds": rounds, "t": now()}

    def _store(self, passcode):
        self.s.set_kv("passcode", self._record(self._check_new(passcode)))

    def verify(self, passcode, ip, local):
        """True for the right passcode; PermissionError while this client (or the LAN) has to wait."""
        with self.mu:
            t = now()
            if self.lan_locked and not local:
                raise PermissionError("too many wrong passcodes: unlock the app on the Mac first")
            n, until = self.fails.get(ip, [0, 0])
            if until > t:
                raise PermissionError(f"too many wrong passcodes: try again in {int(until - t) + 1} s")
        rec = self.s.kv("passcode")
        if not rec:
            raise PermissionError("no passcode is set yet: set one in Settings first")
        ok = hmac.compare_digest(self._hash(str(passcode or ""), rec["salt"], rec.get("rounds", PBKDF2_ROUNDS)), rec["hash"])
        with self.mu:
            if ok:
                self.fails.pop(ip, None)
                if local:
                    self.lan_locked = False
                return True
            n += 1
            wait = 0 if n < 5 else min(900, 30 * 2 ** (n - 5))
            self.fails[ip] = [n, now() + wait]
            self.recent = [x for x in self.recent if now() - x < 3600] + [now()]
            if len(self.recent) > 20:
                self.lan_locked = True
            return False

    def change(self, old, new, ip, local, before_lock=None):
        """A new passcode. With Private set up: the new one is checked first (6 to 128 characters), the
        Private master key is wrapped under it, and both records are written in one transaction, so a refused or failed
        change leaves both as they were (a change must never lock the user out of Private). Then Private locks."""
        private = self.s.vault.is_set()
        new = self._check_new(new)
        if not self.verify(old, ip, local):
            raise PermissionError("the current passcode is wrong")
        recs = {"passcode": self._record(new, 1_000_000 if private else PBKDF2_ROUNDS)}
        if private:
            recs["private_wrap"] = self.s.vault.rewrapped(old, new)
            if before_lock:  # the routes: Private's running answers end first, kept with their words (keys still there)
                before_lock()
        self.s.set_kvs(recs)
        if private:
            self.s.vault.lock()
        # every other device signs in again with the new passcode
        self.s.x("DELETE FROM devices")

    def new_session(self, remember, label, client, old_token=None):
        """A device session; the browser's previous one (old_token) is replaced, so one device is one row."""
        if old_token:
            self.s.x("DELETE FROM devices WHERE hash=?", (hashlib.sha256(old_token.encode()).hexdigest(),))
        token = secrets.token_urlsafe(32)
        did = secrets.token_hex(6)
        self.s.x("INSERT INTO devices (id, hash, label, created, last_seen, remember, client) VALUES (?,?,?,?,?,?,?)",
                 (did, hashlib.sha256(token.encode()).hexdigest(), label[:80], now(), now(), 1 if remember else 0, client))
        return token, did

    def device(self, token):
        """The device row for a session token, or None (unknown, revoked or expired)."""
        if not token or len(token) > 100:
            return None
        h = hashlib.sha256(token.encode()).hexdigest()
        d = self.s.one("SELECT * FROM devices WHERE hash=?", (h,))
        if not d:
            return None
        age = now() - d["last_seen"]
        if (d["remember"] and now() - d["created"] > PAIRED_S) or (not d["remember"] and age > SESSION_IDLE_S):
            self.s.x("DELETE FROM devices WHERE id=?", (d["id"],))
            return None
        if age > 60:
            self.s.x("UPDATE devices SET last_seen=? WHERE id=?", (now(), d["id"]))
        return d

    def devices(self):
        """One row per device (label and address), newest use first; expired sessions are dropped first."""
        t = now()
        self.s.x("DELETE FROM devices WHERE (remember=1 AND ? - created > ?) OR (remember=0 AND ? - last_seen > ?)",
                 (t, PAIRED_S, t, SESSION_IDLE_S))
        out = {}
        for d in self.s.q("SELECT id, label, created, last_seen, remember, client FROM devices ORDER BY last_seen DESC"):
            key = (d["label"], d["client"])
            if key in out:
                out[key]["ids"].append(d["id"])
                out[key]["sessions"] += 1
            else:
                out[key] = dict(d, ids=[d["id"]], sessions=1)
        return list(out.values())

    def revoke(self, did):
        """Sign out a device: every session it has (the same label and address)."""
        d = self.s.one("SELECT label, client FROM devices WHERE id=?", (did,))
        if d:
            self.s.x("DELETE FROM devices WHERE label=? AND client=?", (d["label"], d["client"]))
        return d


def device_label(ua):
    ua = ua or ""
    for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android phone"), ("Macintosh", "Mac"), ("Windows", "Windows PC")):
        if key in ua:
            for b, bn in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari")):
                if b in ua:
                    return f"{bn} on {name}"
            return name
    return "Browser"


def cookie_value(header):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return None


def parse_range(header, size):
    """(start, end) inclusive for a single bytes=a-b range, None for none, False for an unsatisfiable one."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[6:].split(",")[0].strip()
    a, _, b = spec.partition("-")
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                return False
            return max(0, size - n), size - 1
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return None
    if start >= size or end < start:
        return False
    return start, min(end, size - 1)
