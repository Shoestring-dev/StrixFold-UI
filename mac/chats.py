"""Chats on this Mac: the store (SQLite, ~/.config/halo-control/chats.sqlite) and the streaming relay to the model.

A chat has a title, an optional system prompt and settings (thinking on/off, temperature; top_k whenever the
temperature is above 0, because the live DeepSeek build answers HTTP 500 to a sampled request without it).
relay() appends the user's message (or replaces the last one: edit and resend; or drops the last answer:
regenerate), sends the whole conversation to the serving model's OpenAI-compatible endpoint with stream=true and an
explicit max_tokens, and yields simple events for the page while it writes the answer to the store, also when the
reader goes away or the stop route cancels it (the partial answer is kept, marked stopped). Model output is stored
and sent as text; the page renders it without ever inserting it as HTML. Python 3.9 standard library only.
"""
import http.client
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlparse

MAX_TOKENS = 4096
TITLE_LEN = 60


class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.mu = threading.Lock()
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL,
                system TEXT DEFAULT '', settings TEXT DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat_id TEXT, role TEXT, content TEXT,
                reasoning TEXT DEFAULT '', created REAL, meta TEXT DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
        """)
        self.c.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _q(self, sql, args=()):
        with self.mu:
            cur = self.c.execute(sql, args)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            self.c.commit()
            return rows

    def create(self, title="", system="", settings=None):
        cid = secrets.token_hex(6)
        t = time.time()
        self._q("INSERT INTO chats (id, title, created, updated, system, settings) VALUES (?,?,?,?,?,?)",
                (cid, (title or "New chat")[:200], t, t, system or "", json.dumps(check_settings(settings or {}))))
        return self.get(cid)

    def list(self, q=""):
        if q:
            like = "%" + q.replace("%", "").replace("_", "")[:100] + "%"
            rows = self._q("SELECT DISTINCT c.id, c.title, c.updated FROM chats c LEFT JOIN messages m ON m.chat_id = c.id "
                           "WHERE c.title LIKE ? OR m.content LIKE ? ORDER BY c.updated DESC LIMIT 500", (like, like))
        else:
            rows = self._q("SELECT id, title, updated FROM chats ORDER BY updated DESC LIMIT 500")
        return rows

    def get(self, cid):
        rows = self._q("SELECT * FROM chats WHERE id=?", (cid,))
        if not rows:
            raise KeyError(cid)
        c = rows[0]
        c["settings"] = json.loads(c["settings"] or "{}")
        c["messages"] = self.messages(cid)
        return c

    def messages(self, cid):
        out = self._q("SELECT id, role, content, reasoning, created, meta FROM messages WHERE chat_id=? ORDER BY id", (cid,))
        for m in out:
            m["meta"] = json.loads(m["meta"] or "{}")
        return out

    def update(self, cid, title=None, system=None, settings=None):
        c = self.get(cid)
        if title is not None:
            title = str(title).strip()[:200]
            if not title:
                raise ValueError("title: 1 to 200 characters")
            self._q("UPDATE chats SET title=? WHERE id=?", (title, cid))
        if system is not None:
            if len(str(system)) > 20000:
                raise ValueError("system prompt: at most 20,000 characters")
            self._q("UPDATE chats SET system=? WHERE id=?", (str(system), cid))
        if settings is not None:
            merged = dict(c["settings"])
            merged.update(settings)
            self._q("UPDATE chats SET settings=? WHERE id=?", (json.dumps(check_settings(merged)), cid))
        return self.get(cid)

    def delete(self, cid):
        self.get(cid)
        self._q("DELETE FROM messages WHERE chat_id=?", (cid,))
        self._q("DELETE FROM chats WHERE id=?", (cid,))

    def add(self, cid, role, content, reasoning="", meta=None):
        t = time.time()
        with self.mu:
            cur = self.c.execute("INSERT INTO messages (chat_id, role, content, reasoning, created, meta) VALUES (?,?,?,?,?,?)",
                                 (cid, role, content, reasoning, t, json.dumps(meta or {})))
            self.c.execute("UPDATE chats SET updated=? WHERE id=?", (t, cid))
            self.c.commit()
            return cur.lastrowid

    def set_message(self, mid, content, reasoning, meta):
        self._q("UPDATE messages SET content=?, reasoning=?, meta=? WHERE id=?", (content, reasoning, json.dumps(meta), mid))

    def drop_from(self, cid, mid):
        self._q("DELETE FROM messages WHERE chat_id=? AND id>=?", (cid, mid))

    def export_md(self, cid):
        c = self.get(cid)
        lines = [f"# {c['title']}", "", f"Exported from StrixFold UI, {time.strftime('%Y-%m-%d %H:%M')}.", ""]
        if c["system"]:
            lines += ["## System", "", c["system"], ""]
        for m in c["messages"]:
            who = "You" if m["role"] == "user" else (m["meta"].get("model") or "Model")
            lines += [f"## {who}", ""]
            if m["reasoning"]:
                lines += ["<details><summary>Thinking</summary>", "", m["reasoning"], "", "</details>", ""]
            lines += [m["content"], ""]
        return "\n".join(lines)


def check_settings(s):
    out = {}
    if s.get("profile"):
        if not re.fullmatch(r"[a-z0-9-]{1,80}", str(s["profile"])):
            raise ValueError("profile: a profile id")
        out["profile"] = str(s["profile"])
    if "thinking" in s:
        out["thinking"] = bool(s["thinking"])
    if "temperature" in s:
        try:
            t = float(s["temperature"])
        except (TypeError, ValueError):
            raise ValueError("temperature: a number from 0 to 2")
        if not 0 <= t <= 2:
            raise ValueError("temperature: a number from 0 to 2")
        out["temperature"] = t
    if "top_k" in s:
        try:
            k = int(s["top_k"])
        except (TypeError, ValueError):
            raise ValueError("top_k: a whole number from 1 to 56")
        if not 1 <= k <= 56:
            raise ValueError("top_k: a whole number from 1 to 56")
        out["top_k"] = k
    if "max_tokens" in s:
        try:
            m = int(s["max_tokens"])
        except (TypeError, ValueError):
            raise ValueError("max_tokens: a whole number from 16 to 32,768")
        if not 16 <= m <= 32768:
            raise ValueError("max_tokens: a whole number from 16 to 32,768")
        out["max_tokens"] = m
    return out


def title_of(text):
    t = re.sub(r"\s+", " ", text).strip()
    return (t[:TITLE_LEN - 1] + "…") if len(t) > TITLE_LEN else (t or "New chat")


class Active(dict):
    """The answers being streamed, chat id -> {"conn", "stop"}; last_end is when one last ended (Agents waits
    60 s after an answer to the user before its next request)."""
    last_end = 0.0

    def pop(self, k, *default):
        if k in self:
            self.last_end = time.time()
        return super().pop(k, *default)

    def __delitem__(self, k):
        self.last_end = time.time()
        super().__delitem__(k)


class Relay:
    """One answer being streamed: the upstream connection, so the stop route can close it."""

    def __init__(self):
        self.active = Active()  # chat id -> {"conn": HTTPConnection, "stop": Event}
        self.mu = threading.Lock()
        # waiting messages the user pressed Send now on, id -> their created time: no step takes them in
        # (halo_chat._take_steers) and the outbox sends them first. The time keeps the mark on that message: an outbox
        # id comes back once the table empties (review: a later message with the same id would never have been taken in
        # at a step)
        self.now = {}

    def send_now(self, oid, row_of):
        """Send now on a waiting message (Claude's): marked so the answer's next step leaves it, then the answer stops
        and the outbox sends it once the chat is free. row_of(oid): its outbox row ({"chat_id", "created"}), or None
        when a step took it in already (the answer goes on, about to answer it, so nothing stops). Checked and marked
        under the lock _take_steers takes it under: never a stop after the user's message went in. (chat id or None,
        stopped)."""
        with self.mu:
            row = row_of(oid)
            if row is None:
                return None, False
            self.now[oid] = row.get("created")
        return row["chat_id"], self.stop(row["chat_id"])

    def is_now(self, w):
        """Whether Send now was pressed on waiting message w (an outbox row): its id and its created time."""
        return w["id"] in self.now and self.now[w["id"]] == w.get("created")

    def claim(self, cid, compacting=False):
        """The chat for one answer or compaction, taken under the lock before anything else is looked at (two sends, or
        a send and a Compact now, both got through a check-then-register gap). The entry, or None when the chat is
        taken."""
        with self.mu:
            if cid in self.active:
                return None
            entry = self.active[cid] = {"conn": None, "stop": threading.Event(), "compacting": compacting}
            return entry

    def release(self, cid, entry):
        """Give the chat back: only its own entry (a later claim is never removed by an earlier one's cleanup)."""
        with self.mu:
            if self.active.get(cid) is entry:
                del self.active[cid]

    def stop(self, cid):
        with self.mu:
            a = self.active.get(cid)
        if not a:
            return False
        a["stop"].set()
        c = a.get("conn")
        if c is not None and c.sock is not None:
            try:
                c.sock.shutdown(2)
            except OSError:
                pass
        return True

    def busy(self, cid):
        with self.mu:
            return cid in self.active


def relay(store, rl, cid, endpoint, model, content=None, mode="send", attachments=None, model_label=None):
    """Yield page events for one answer. mode: send (a new user message), edit (replace the last user message and its
    answer), regenerate (drop the last answer). Events: {"type": "user", ...}, {"type": "start"}, {"type": "delta",
    "text"}, {"type": "reasoning", "text"}, {"type": "done", "finish", "usage", "stopped"}, {"type": "error"}."""
    chat = store.get(cid)
    msgs = chat["messages"]
    if mode in ("edit", "regenerate"):
        last_user = next((m for m in reversed(msgs) if m["role"] == "user"), None)
        if last_user is None:
            raise ValueError("there is no message to resend")
        if mode == "edit":
            if not (content or "").strip():
                raise ValueError("the message is empty")
            store.drop_from(cid, last_user["id"])
        else:
            store.drop_from(cid, last_user["id"] + 1)
    if mode in ("send", "edit"):
        text = (content or "").strip()
        for a in attachments or []:
            name = str(a.get("name") or "file")[:200]
            body = str(a.get("text") or "")
            if len(body) > 400_000:
                raise ValueError(f"{name}: larger than 400,000 characters")
            fence = "````" if "```" in body else "```"
            text += f"\n\n{fence} {name}\n{body}\n{fence}"
        if not text:
            raise ValueError("the message is empty")
        if len(text) > 1_000_000:
            raise ValueError("the message is larger than 1,000,000 characters")
        uid = store.add(cid, "user", text, meta={"attachments": [str(a.get("name"))[:200] for a in attachments or []]})
        yield {"type": "user", "id": uid, "content": text}
        if chat["title"] == "New chat" and not [m for m in msgs if m["role"] == "user"]:
            store.update(cid, title=title_of(content or (attachments or [{}])[0].get("name") or "New chat"))
    chat = store.get(cid)
    s = chat["settings"]
    body = {"model": model, "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": int(s.get("max_tokens") or MAX_TOKENS),
            "messages": ([{"role": "system", "content": chat["system"]}] if chat["system"] else []) +
                        [{"role": m["role"], "content": m["content"]} for m in chat["messages"] if m["role"] in ("user", "assistant") and (m["content"] or m["role"] == "user")]}
    if float(s.get("temperature") or 0) > 0:
        body["temperature"], body["top_k"] = float(s["temperature"]), int(s.get("top_k") or 40)  # never without top_k
    else:
        body["temperature"] = 0
    if "thinking" in s:
        body["chat_template_kwargs"] = {"enable_thinking": bool(s["thinking"])}
    meta = {"model": model_label or model, "served_model": model, "started": time.time()}
    mid = store.add(cid, "assistant", "", meta=meta)
    yield {"type": "start", "id": mid, "model": model_label or model}
    stop = threading.Event()
    u = urlparse(endpoint)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=900)
    with rl.mu:
        rl.active[cid] = {"conn": conn, "stop": stop}
    text, reasoning, usage, finish, err = [], [], None, None, None
    first = None
    last_save = time.time()
    try:
        conn.request("POST", u.path.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                     {"Content-Type": "application/json", "Accept": "text/event-stream", "Authorization": "Bearer none"})
        resp = conn.getresponse()
        if resp.status != 200:
            raw = resp.read().decode("utf-8", "replace")[:600]
            try:
                err = json.loads(raw).get("error")
                err = err.get("message") if isinstance(err, dict) else err
            except ValueError:
                err = raw
            err = f"the model answered HTTP {resp.status}: {err or raw}"
        else:
            buf = b""
            while not stop.is_set():
                chunk = resp.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("error"):
                    e = obj["error"]
                    err = e.get("message") if isinstance(e, dict) else str(e)
                    break
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    if d.get("reasoning_content"):
                        reasoning.append(d["reasoning_content"])
                        first = first or time.time()
                        yield {"type": "reasoning", "text": d["reasoning_content"]}
                    if d.get("content"):
                        text.append(d["content"])
                        first = first or time.time()
                        yield {"type": "delta", "text": d["content"]}
                if time.time() - last_save > 2:  # a crash or a closed tab keeps what came so far
                    store.set_message(mid, "".join(text), "".join(reasoning), dict(meta, partial=True))
                    last_save = time.time()
    except (OSError, http.client.HTTPException) as e:
        if not stop.is_set():
            err = f"the model endpoint did not answer: {e}"
    finally:
        with rl.mu:
            rl.active.pop(cid, None)
        try:
            conn.close()
        except OSError:
            pass
        t = time.time()
        meta.update(finish=finish or ("stopped" if stop.is_set() else None), usage=usage, error=err,
                    ttft_s=round(first - meta["started"], 3) if first else None, total_s=round(t - meta["started"], 3),
                    stopped=stop.is_set())
        if usage and first and (usage.get("completion_tokens") or 0) > 1 and t > first:
            meta["tok_s"] = round((usage["completion_tokens"] - 1) / (t - first), 1)
        store.set_message(mid, "".join(text), "".join(reasoning), meta)
    if err:
        yield {"type": "error", "error": err, "id": mid}
    yield {"type": "done", "id": mid, "finish": meta["finish"], "usage": usage, "stopped": stop.is_set(),
           "tok_s": meta.get("tok_s"), "ttft_s": meta.get("ttft_s")}
