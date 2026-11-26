"""The private app's routes in the Mac server (mac/halo_control.py hands every /studio/ path here).

Every route except /studio/api/auth (what the lock screen needs) checks the device session cookie first, on each
request; writes also pass the dashboard's own guards (this Mac's Host names, a same-origin Origin, the page token).
Files are served from the private store by random id with no-store, a sandbox CSP and byte ranges (Safari plays a clip
only with ranges). Nothing a request carries is logged.
"""
import contextvars
import gzip
import hmac
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

import chat_tools
import halo_chat
import look
import private_store as _private
import outbox
import studio
import studio_transcribe

REF_RE = re.compile(r"^/studio/ref/(u[0-9a-f]{8,40})(\.thumb)?$")
DOC_RE = re.compile(r"^/studio/doc/(u[0-9a-f]{8,40})$")  # a chat document's own file: a PDF the user attached or the chat made
DOC_VIEW_RE = re.compile(r"^/studio/doc/(u[0-9a-f]{8,40})/(?:info|page/([1-9][0-9]{0,3}))$")  # the viewer's pages
UPLOAD_TYPES = ("image/", "audio/", "video/", "application/octet-stream")


def perm_model_id(chat, profile=None):
    """The model id a chat's permissions are checked against: the chat's model (or a new chat's chosen profile)."""
    if chat is not None and profile is None:
        return halo_chat.need(chat)[1]
    pid = profile if profile in halo_chat.CHAT_PROFILES else halo_chat.DEFAULT_MODEL
    return halo_chat.need({"model": pid})[1]


def set_level(st, cid, perm, opts):
    """Store a chat's level; a change leaves a divider in the thread ("Permissions: Container + web"). Leaving Full keeps
    its folder's name as last_folder (the folder stays on this Mac: the delete dialog says so, and Full reuses it)."""
    row = st.chat(cid)
    old, prev = chat_tools.level_of(row), chat_tools.opts_of(row)
    folder = prev.get("folder") or prev.get("last_folder")
    if folder and perm != "full":
        opts = dict(opts or {}, last_folder=folder)
    c = st.set_perm(cid, perm, opts)
    if old != perm:
        st.add_message(cid, "note", "", {"perm_from": old, "perm_to": perm})
        c = st.chat(cid)
    return c


def private_endpoint(S, model_id, hs):
    """(endpoint, None) for a Private turn on model_id, or (None, words) when it can't take one. None
    from the fake app's gate: the ordinary endpoint."""
    gate = S.get("private_gate")
    name = halo_chat.MODEL_NAMES.get(model_id) or model_id or "this model"
    if gate is None:
        return None, f"Private chats can't run on {name}: the check isn't set up"
    probs = gate.check(model_id, (hs or {}).get("run_id"), (hs or {}).get("served_model"))
    if probs:
        return None, f"Private chats can't run on {name} right now: " + "; ".join(probs[:3]) + "."
    try:
        ep = gate.endpoint(model_id)
    except RuntimeError as e:
        return None, f"Private chats can't run on {name} right now: {e}."
    return (ep, None) if ep else None


def chat_endpoint(A, S, hs, cid):
    """The copy of the serving model a chat uses: with a copy on each box (Kyojin, Gufo v0.21), the one fewer
    answering chats are on when this chat is first seen in the run, then kept (each server's prompt cache is its own)."""
    pick = getattr(A, "endpoint_for", None)
    if pick is None:
        return A.local_endpoint(hs)
    with S["relay"].mu:
        busy = {("chat", c) for c in S["relay"].active if c != cid}
    return pick(hs, ("chat", cid), busy)


def client_local(h):
    return h.channel() == "local"


def client(h):
    """(address, tailnet login) of the device asking. On the tailnet listener every request comes from tailscaled on
    127.0.0.1, so the device is what `tailscale serve` forwards (X-Forwarded-For, Tailscale-User-Login); those headers
    are trusted there only. It keys the wrong-passcode throttle and names the device rows."""
    if not getattr(h, "tailnet", False):
        return h.client_address[0], ""
    import ipaddress
    ip = (h.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()  # the last hop: the one tailscale serve adds
    try:
        ip = str(ipaddress.ip_address(ip))
    except ValueError:
        ip = "tailnet"
    login = re.sub(r"[^A-Za-z0-9@._+-]", "", h.headers.get("Tailscale-User-Login") or "")[:80]
    return ip, login


def label_of(h):
    ip, login = client(h)
    return studio.device_label(h.headers.get("User-Agent")) + (f" ({login})" if login else "")


def https_page(h):
    """Plain HTTP from the LAN: the private pages do not open here; where they do (the tailnet over HTTPS), if set up."""
    import halo_control
    name, ready = halo_control.tailnet_name(h.A.cfg)
    return {"error": "https_required", "url": f"https://{name}/#/talk" if name else None, "tailnet_ready": ready}


_you = {}


def you(st=None):
    """The sidebar's initials: the settings' own, else the Mac owner's name (macOS's full name for this user)."""
    mine = (st.settings().get("initials") or "").strip() if st else ""
    if mine:
        return {"initials": mine.upper()}
    if "initials" not in _you:
        name = ""
        try:
            import subprocess
            name = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        words = re.findall(r"[^\W\d_]+", name)
        _you["initials"] = ("".join(w[0] for w in words[:2]) or "?").upper()
    return {"initials": _you["initials"]}


def links(h):
    """Plain addresses for the page's "Open elsewhere" block, never a token. On the Mac: the dashboard and the app's
    tailnet address for the phone. On the tailnet page: the dashboard on the home network (it has no login, so it is
    not on the tailnet), by address and Bonjour name."""
    import halo_control
    cfg = h.A.cfg
    if h.channel() == "tailnet":
        names = sorted(halo_control.lan_names(), key=lambda n: (not n.replace(".", "").isdigit(), n)) if cfg.get("lan") else []
        return {"here": "tailnet", "home": [f"http://{n}:{cfg['ui_port']}/" for n in names]}
    name, ready = halo_control.tailnet_name(cfg)
    return {"here": "mac", "dashboard": f"http://127.0.0.1:{cfg['ui_port']}/", "phone": f"https://{name}/#/talk" if name and ready else None}


def device(h):
    S = h.A.studio
    return S["auth"].device(studio.cookie_value(h.headers.get("Cookie")))


def send_json(h, code, obj, cookie=None):
    """gzipped when the browser takes it (the app was slow on a slower computer; a long chat's JSON is
    about a fifth of its size gzipped). The tailnet page gets it through `tailscale serve` as it is."""
    body = json.dumps(obj).encode()
    gz = len(body) > 1024 and "gzip" in (h.headers.get("Accept-Encoding") or "")
    if gz:
        body = gzip.compress(body, 5)
    h.send_response(code)
    h.headers_common("application/json", len(body))
    h.send_header("Vary", "Accept-Encoding")
    if gz:
        h.send_header("Content-Encoding", "gzip")
    if cookie is not None:
        h.send_header("Set-Cookie", cookie)
    h.end_headers()
    h.wfile.write(body)


def light(c):
    """A chat as the chat page first draws it (on a slower computer: the longest chat was 8.8 MB, mostly its steps'
    output and the model's thinking, fetched and drawn whole on every open and redraw). Each step's output and each
    answer's thinking stay here until the user opens one (GET chats/<id>/messages/<mid>); out_n and reasoning_n say how
    much there is. The answer being written keeps both, as the page shows them while they come in."""
    msgs = c.get("messages") or []
    # the newest answer, not the newest row: a permission change while it answers adds a note after it
    live = next((m["id"] for m in reversed(msgs) if m["role"] == "assistant"), None) if c.get("streaming") else None
    for m in msgs:
        meta = m.get("meta") or {}
        if m["id"] == live or not (meta.get("reasoning") or meta.get("parts")):
            continue
        meta = m["meta"] = dict(meta, light=True)
        if meta.get("reasoning"):
            meta["reasoning_n"] = len(meta.pop("reasoning"))
        meta["parts"] = [dict(p, tool=_light_step(p["tool"])) if isinstance(p.get("tool"), dict) else p
                         for p in meta.get("parts") or []]
    return c


def _light_step(t):
    t = dict(t)
    out = t.pop("out", None) or t.pop("live", None) or ""
    t.pop("live", None)
    t["out_n"] = len(out)
    return t


DIRECT_HERE = "Computers for Direct access are added on the Mac itself."
PRIVATE_MIN = _private.MIN_PASSCODE  # P4: the app's own minimum (6); a shorter entry is simply a wrong passcode
LOCK_WAIT_S = 5  # how long Lock waits for a running Private answer to stop and keep its words


def stop_private(S, why="Private was locked"):
    """Every running Private answer: stopped, and given LOCK_WAIT_S to keep its words, sealed,
    while its key is still there; its thread says "Stopped: Private was locked." The caller locks after."""
    st, relay = S["store"], S["relay"]
    with relay.mu:
        running = list(relay.active.items())
    mine = [(cid, e) for cid, e in running if st.is_private(cid)]
    for cid, e in mine:
        e["why"] = why
        relay.stop(cid)
    end = time.time() + LOCK_WAIT_S
    while time.time() < end and any(relay.active.get(cid) is e for cid, e in mine):
        time.sleep(0.05)


def warm_private(A, S):
    """On Unlock: the serving model's Private check starts in the background (private_engines.Gate.warm)."""
    gate = S.get("private_gate")
    if gate is None:
        return

    def run():
        try:
            hs = A.halod_state() or {}
        except Exception:  # noqa: BLE001 - nothing serving, or halod away: a turn checks itself
            return
        mid = ((hs.get("profile") or {}).get("model_id")) or ""
        if hs.get("state") == "ready" and mid:
            gate.warm(mid, hs.get("run_id"), hs.get("served_model"))
    threading.Thread(target=run, name="private-warm", daemon=True).start()


def lock_private(S):
    """Lock Private: its running answers stop first (stop_private), then the keys and tokens go, then the private
    tunnels."""
    stop_private(S)
    S["store"].vault.lock()
    if S.get("private_gate"):
        S["private_gate"].close()


def private_grant(h):
    """A request may read Private chats when it carries this device session's private token (X-Halo-Private) and
    Private is unlocked."""
    S = getattr(h.A, "studio", None)
    tok = h.headers.get("X-Halo-Private")
    if not S or not tok:
        return False
    d = device(h)
    return bool(d) and S["store"].vault.check(d["id"], tok)


PRIVATE_NO = "Private chats have no Full access."
UNREADABLE_FILE = "This file can't be read: it was changed or damaged on this Mac."


def get(h):
    """The GET routes, with the request's Private grant; a Private chat without it answers 404 locked."""
    tok = _private.GRANT.set(private_grant(h))
    try:
        return _get(h)
    except _private.Unreadable as e:  # only past the grant: the chat's key is damaged (it can still be deleted)
        return send_json(h, 409, {"error": str(e), "why": "unreadable"})
    except _private.Locked:
        return send_json(h, 404, {"error": "locked"})
    except _private.C.Tampered:  # a Private file that doesn't open
        return send_json(h, 409, {"error": UNREADABLE_FILE, "why": "unreadable"})
    finally:
        _private.GRANT.reset(tok)


def write(h, method):
    tok = _private.GRANT.set(private_grant(h))
    try:
        return _write(h, method)
    except _private.Unreadable as e:
        return send_json(h, 409, {"error": str(e), "why": "unreadable"})
    except _private.Locked:
        return send_json(h, 404, {"error": "locked"})
    except _private.C.Tampered:
        return send_json(h, 409, {"error": UNREADABLE_FILE, "why": "unreadable"})
    finally:
        _private.GRANT.reset(tok)


def private_view(S):
    v = S["store"].vault
    return {"set": v.is_set(), "unlocked": v.unlocked(), "granted": bool(_private.GRANT.get()), "min": PRIVATE_MIN}


def _get(h):
    u = urlparse(h.path)
    S = getattr(h.A, "studio", None)
    if not S:
        return send_json(h, 503, {"error": "The private store is not set up on this dashboard"})
    if u.path == "/studio/api/auth":
        if h.channel() == "lan":
            return send_json(h, 200, dict(https_page(h), set=S["auth"].is_set(), unlocked=False, local=False, https_only=True))
        d = device(h)
        local = client_local(h)
        return send_json(h, 200, {"set": S["auth"].is_set(), "skipped": local and S["auth"].skipped(), "unlocked": bool(d), "local": local,
                                  "device": d and d["label"], "remember": bool(d and d["remember"]),
                                  # the Mac's idle lock (studio.js): minutes, 0 = off; never on the phone or tailnet page
                                  "idle_lock_min": S["store"].settings().get("idle_lock_min", 0) if local else 0})
    if h.channel() == "lan":
        return send_json(h, 403, https_page(h))
    d = device(h)
    if not d:
        return send_json(h, 401, {"error": "locked", "set": S["auth"].is_set()})
    m = REF_RE.match(u.path)
    if m:
        return ref(h, m.group(1), bool(m.group(2)))
    m = DOC_RE.match(u.path)
    if m:
        return doc_file(h, m.group(1), parse_qs(u.query).get("dl") == ["1"])
    m = DOC_VIEW_RE.match(u.path)
    if m:
        return doc_view(h, m.group(1), int(m.group(2) or 0))
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    st = S["store"]
    parts = u.path.strip("/").split("/")[2:]  # after studio/api
    if parts[:1] == ["agents"]:  # Agents (agents_http.py), behind the same session
        import agents_http
        return agents_http.get(h, S, parts[1:], q)
    try:
        if parts == ["state"]:
            cid = q.get("chat") or ""
            rev = {"list": st.list_rev(), "chat": st.chat_rev(cid) if cid else None}  # the page reloads what changed
            out = dict(state(h.A, S), links=links(h), rev=rev, you=you(st))
            return send_json(h, 200, out)
        if parts == ["chats"]:  # kind: one of studio.KINDS, or all (the sidebar's Recents)
            return send_json(h, 200, {"chats": st.chats(q.get("q", ""), q.get("kind") or studio.KIND, q.get("project") or None)})
        if parts == ["projects"]:
            return send_json(h, 200, {"projects": st.projects()})
        if len(parts) == 2 and parts[0] == "projects":
            p = st.project(parts[1])
            return send_json(h, 200, dict(p, chats=st.chats(kind="all", project=parts[1])))
        if len(parts) == 3 and parts[0] == "uploads" and parts[2] == "look":  # a photo's chip: is its description ready?
            u = st.upload(parts[1])
            return send_json(h, 200, {"id": parts[1], "look": look_state(st, parts[1], u, S.get("looker"))})
        if parts == ["chat", "models"]:  # the model sheet: the chat models
            out = dict(halo_chat.models(h.A.halod_state()))
            return send_json(h, 200, out)
        if parts == ["private"]:  # Private's state for the page (set, unlocked, this request granted)
            return send_json(h, 200, private_view(S))
        if parts == ["chat", "perms"]:  # the Permissions chip's sheet (chat_tools.view) and Toolbox's state for the container
            c = st.chat(q["chat"]) if q.get("chat") else None
            v = chat_tools.view(c, perm_model_id(c, q.get("profile")))
            if any(r["id"] in ("box", "box_web") and r["offered"] for r in v["levels"]):
                import chat_box
                ok, info = chat_box.health(bool(getattr(h.A, "fake", False)), fresh=q.get("fresh") == "1", store=st)
                v["toolbox"] = {"ok": ok, "words": None if ok else chat_box.NO_DISK if info.get("chats_fs") is False else chat_box.OFFLINE}
            return send_json(h, 200, v)
        if parts == ["speed"]:  # every pair's recent decode and prompt speeds (the Speed sheet)
            return send_json(h, 200, speed_view(h.A, minutes=max(5, min(24 * 60, int(q.get("minutes") or 60)))))
        if parts == ["chat", "switch"]:  # what loading this profile would stop, or what stands in its way
            return send_json(h, 200, switch_check(h.A, S, q.get("profile") or ""))
        if len(parts) in (3, 4) and parts[0] == "chats" and parts[2] == "team":  # team mode (chat_team): the team, an agent
            c = st.chat(parts[1])
            team = S.get("team")
            if team is None or c.get("private"):
                return send_json(h, 200, {"cap": 0, "agents": [], "running": 0, "slots": []})
            try:
                return send_json(h, 200, team.view(parts[1]) if len(parts) == 3 else
                                 team.detail(parts[1], parts[3], after=int(q.get("after") or 0)))
            except KeyError:
                return send_json(h, 404, {"error": "no such agent"})
        if len(parts) == 2 and parts[0] == "chats":
            rev = st.chat_rev(parts[1])  # read before the chat: a change while it is read shows as a newer rev next poll
            c = st.chat(parts[1])
            c["streaming"] = S["relay"].busy(parts[1])
            c["ask"] = chat_tools.ASKS.open(parts[1])  # a tap the chat's turn waits on
            c["waiting"] = st.waiting(parts[1])
            # waiting messages too: their attachments were shown as "removed" (an iPhone)
            refs = {a["id"] for m in c["messages"] + c["waiting"] for a in (m["meta"].get("attachments") or [])}
            c["refs"] = {r: ref_view(st, r) for r in refs}
            c["rev"] = rev
            if (c.get("kind") or studio.KIND) == "chat":
                c["window"] = window_view(h.A, S, c)
                if S.get("team") is not None and not c.get("private"):
                    c["team"] = S["team"].view(parts[1])
                if S["outbox"].goals is not None and not c.get("private"):
                    c["goal"] = S["outbox"].goals.get(parts[1])
            if q.get("light") == "1":
                light(c)
            return send_json(h, 200, c)
        if len(parts) == 4 and parts[0] == "chats" and parts[2] == "messages":  # what light() left out of one message
            m = st.message(int(parts[3])) if parts[3].isdigit() else None
            if not m or m.get("chat_id") != parts[1]:
                return send_json(h, 404, {"error": "no such message"})
            return send_json(h, 200, {"id": m["id"], "meta": m["meta"]})
        if parts == ["direct"]:  # Settings' computers for Direct access (direct_setup.py): this Mac only
            if not client_local(h):
                return send_json(h, 403, {"error": DIRECT_HERE})
            import direct_setup
            return send_json(h, 200, {"computers": direct_setup.computers()})
        if parts == ["settings"]:
            return send_json(h, 200, {"settings": st.settings(), "root": st.root,
                                      "devices": S["auth"].devices(), "this_device": d["id"]})
    except KeyError:
        return send_json(h, 404, {"error": "not found"})
    except ValueError as e:
        return send_json(h, 400, {"error": str(e)})
    return send_json(h, 404, {"error": "not found"})


def _write(h, method):
    u = urlparse(h.path)
    S = getattr(h.A, "studio", None)
    if not S:
        return send_json(h, 503, {"error": "The private store is not set up on this dashboard"})
    if h.channel() == "lan":
        return send_json(h, 403, https_page(h))
    if u.path == "/studio/api/uploads" and method == "POST":
        return upload(h, S)
    if u.path == "/studio/api/transcribe" and method == "POST":
        return transcribe(h, S)
    why = h.write_ok()
    if why:
        return send_json(h, 403, {"error": why})
    n = int(h.headers.get("Content-Length") or 0)
    if n > 1 << 20:
        return send_json(h, 413, {"error": "too large"})
    try:
        b = json.loads(h.rfile.read(n) or b"{}") if n else {}
    except ValueError:
        return send_json(h, 400, {"error": "JSON only"})
    A, st, auth = h.A, S["store"], S["auth"]
    ip, local = client(h)[0], client_local(h)
    parts = u.path.strip("/").split("/")[2:]
    try:
        if parts == ["auth", "setup"] and method == "POST":
            key = getattr(A, "open_key", None)  # none: nobody sets the first passcode (serve() always makes one)
            key_ok = bool(key) and hmac.compare_digest(str(b.get("key") or "").encode(), key.encode())
            auth.setup(b.get("passcode"), local, signed_in=bool(device(h)), key_ok=key_ok)
            token, did = auth.new_session(True, label_of(h), ip)
            return send_json(h, 200, {"ok": True}, cookie=cookie(token, True, secure(h)))
        if parts == ["auth", "skip"] and method == "POST":  # no passcode, on this Mac only (the first screen's Skip)
            key = getattr(A, "open_key", None)
            ok = bool(key) and hmac.compare_digest(str(b.get("key") or "").encode(), key.encode())
            token, did = auth.skip(local, ok, label_of(h), ip, studio.cookie_value(h.headers.get("Cookie")))
            import halo_control
            halo_control.write_open_key(A, A.cfg["ui_port"])  # a new key: the one in this browser's history is dead now
            return send_json(h, 200, {"ok": True}, cookie=cookie(token, True, secure(h)))
        if parts == ["auth", "unlock"] and method == "POST":
            if not auth.verify(b.get("passcode"), ip, local):
                time.sleep(0.4)
                return send_json(h, 403, {"error": "That passcode is not right."})
            token, did = auth.new_session(bool(b.get("remember")), label_of(h), ip,
                                          studio.cookie_value(h.headers.get("Cookie")))
            return send_json(h, 200, {"ok": True}, cookie=cookie(token, bool(b.get("remember")), secure(h)))
    except PermissionError as e:
        return send_json(h, 403, {"error": str(e)})
    except ValueError as e:
        return send_json(h, 400, {"error": str(e)})
    d = device(h)
    if not d:
        return send_json(h, 401, {"error": "locked", "set": auth.is_set()})
    if parts[:1] == ["agents"]:  # Agents (agents_http.py)
        import agents_http
        return agents_http.write(h, S, method, parts[1:], b)
    try:
        if parts == ["private", "unlock"] and method == "POST":  # the passcode again, then a token for this page
            pc = str(b.get("passcode") or "")
            if not auth.verify(pc, ip, local):
                time.sleep(0.4)
                return send_json(h, 403, {"error": "That passcode is not right."})
            chk = getattr(A, "code_check", None)  # only the landed code, unchanged since start, holds the keys
            code = chk.view() if chk is not None else {"clean": False, "problems": ["the running code wasn't checked at start"],
                                                       "line": "Running code: not checked"}
            if not code["clean"]:
                return send_json(h, 409, {"error": "Private can't unlock: the running code isn't the landed code ("
                                                   + "; ".join(code["problems"][:3]) + "). Restart StrixFold UI from the landed commit.",
                                          "why": "code", "code": code})
            st.vault.unlock(pc)
            auth.strengthen(pc)
            warm_private(A, S)
            return send_json(h, 200, dict(private_view(S), token=st.vault.issue(d["id"]), unlocked=True, code=code))
        if parts == ["private", "lock"] and method == "POST":
            lock_private(S)
            return send_json(h, 200, private_view(S))
        if parts == ["auth", "set"] and method == "POST":  # a passcode after a Skip, from Settings on this Mac
            auth.setup(b.get("passcode"), local, signed_in=True)  # every session made without one ends
            token, did = auth.new_session(True, d["label"], ip)
            return send_json(h, 200, {"ok": True}, cookie=cookie(token, True, secure(h)))
        if parts[:1] == ["direct"]:  # add, test and remove a computer for Direct access: this Mac only, never the phone
            if not local:
                return send_json(h, 403, {"error": DIRECT_HERE})
            import direct_setup
            if parts == ["direct", "test"] and method == "POST":
                if b.get("id"):  # a listed computer, with its own ssh options
                    return send_json(h, 200, direct_setup.test_listed(str(b["id"])))
                return send_json(h, 200, direct_setup.test(b.get("host"), b.get("shell")))
            if parts == ["direct"] and method == "POST":
                if auth.is_set() and not auth.verify(b.get("passcode"), ip, local):  # a shell on that computer: ask again
                    time.sleep(0.4)
                    return send_json(h, 403, {"error": "That passcode is not right."})
                cid, t = direct_setup.add(b.get("name"), b.get("host"), b.get("shell"), b.get("note") or "")
                return send_json(h, 200 if cid else 409, dict(t, id=cid, computers=direct_setup.computers(), **({} if cid else {"error": t["words"]})))
            if len(parts) == 2 and method == "DELETE":
                direct_setup.remove(parts[1])
                return send_json(h, 200, {"computers": direct_setup.computers()})
            return send_json(h, 404, {"error": "not found"})
        if parts == ["auth", "lock"] and method == "POST":
            lock_private(S)  # the app's own lock locks Private too
            auth.revoke(d["id"])
            return send_json(h, 200, {"ok": True}, cookie=f"{studio.COOKIE}=; Path=/; HttpOnly; SameSite=Strict{'; Secure' if secure(h) else ''}; Max-Age=0")
        if parts == ["auth", "change"] and method == "POST":
            auth.change(b.get("old"), b.get("new"), ip, local, before_lock=lambda: stop_private(S))  # it locks Private
            if S.get("private_gate"):
                S["private_gate"].close()
            token, did = auth.new_session(bool(d["remember"]), d["label"], ip)
            return send_json(h, 200, {"ok": True}, cookie=cookie(token, bool(d["remember"]), secure(h)))
        if len(parts) == 2 and parts[0] == "devices" and method == "DELETE":
            gone = auth.revoke(parts[1]) or {}
            return send_json(h, 200, {"ok": True, "self": (gone.get("label"), gone.get("client")) == (d["label"], d["client"])})
        if parts == ["settings"] and method == "PUT":
            return send_json(h, 200, {"settings": st.update_settings(b)})
        if parts == ["chats"] and method == "POST":
            kind = b.get("kind") or studio.KIND
            model = b.get("model") if b.get("model") in halo_chat.CHAT_PROFILES else None
            why = size_offered(A, model) if kind == "chat" and model else None
            if why:  # a size the composer shows disabled, as PUT and Start refuse it
                raise ValueError(why)
            if b.get("private") and (getattr(A, "cfg", None) or {}).get("connect"):
                import connect
                raise ValueError(connect.PRIVATE)
            if b.get("private") and b.get("perm") not in (None,) + studio.PRIVATE_LEVELS:  # before it exists
                raise ValueError(PRIVATE_NO)
            want = b.get("perm") if kind == "chat" and b.get("perm") not in (None, "none") else None
            perm = chat_tools.check_perm(None, b, perm_model_id(None, model)) if want and want != "full" else None
            if want == "full":  # offered for this model at all (J3), before anything is made
                ok, why = chat_tools.offered("full", perm_model_id(None, model))
                if not ok:
                    raise ValueError(f"{chat_tools.WORDS['full']['name']}: {why}")
            c = st.create_chat(b.get("title") or "", kind, b.get("project_id") or None,
                               model if kind == "chat" else None, b.get("effort") if kind == "chat" else None,
                               private=bool(b.get("private")) and kind == "chat")
            if want == "full":  # Full makes the chat's own folder, named for the chat (<title>-<id6>): the chat first
                try:
                    perm = chat_tools.check_perm(c, b, perm_model_id(None, model), fake=bool(getattr(A, "fake", False)), store=st.root)
                except (ValueError, PermissionError) as e:  # the chat stays, at No tools; the page keeps it and says why
                    return send_json(h, 400, {"error": str(e), "chat": c})
            return send_json(h, 200, set_level(st, c["id"], *perm) if perm else c)  # its divider shows the level it began at
        if len(parts) == 2 and parts[0] == "chats":
            if method == "PUT":
                if "model" in b and b["model"] not in halo_chat.CHAT_PROFILES:
                    raise ValueError("model: one of the chat models")
                if "model" in b:
                    why = size_offered(A, b["model"])
                    if why:
                        raise ValueError(why)
                if "perm" in b:
                    c = st.chat(parts[1])
                    new = chat_tools.check_perm(c, b, perm_model_id(st.chat(parts[1]) if "model" not in b else None,
                                                                    b.get("model") if "model" in b else None),
                                                fake=bool(getattr(A, "fake", False)), store=st.root)
                    set_level(st, parts[1], *new)
                c = st.update_chat(parts[1], b.get("title"), b.get("project_id") or None if "project_id" in b else False,
                                   b.get("model"),
                                   effort=b.get("effort"))
                lv = chat_tools.level_of(c)
                if "model" in b and lv != "none" and not chat_tools.offered(lv, perm_model_id(c, None))[0]:
                    c = set_level(st, parts[1], "none", {})  # e.g. Full, then Qwen 3.8 (Kyojin) chosen (J3)
                return send_json(h, 200, c)
            if method == "DELETE":
                if st.is_private(parts[1]):
                    st.vault.require()  # nothing of a Private chat is touched, not even its answer stopped, without the grant
                S["relay"].stop(parts[1])
                if st.is_private(parts[1]):  # its sealed folder on Toolbox, read from the plain columns (its key isn't opened
                    r = st.one("SELECT perm, pwork FROM chats WHERE id=?", (parts[1],)) or {}  # here: a damaged one deletes too)
                    boxed = r.get("perm") in ("box", "box_web") or bool(r.get("pwork"))
                else:
                    c = st.chat(parts[1])
                    boxed = chat_tools.level_of(c) in ("box", "box_web") or any(
                        m["role"] == "note" and (m["meta"].get("perm_to") or "").startswith("box") for m in c["messages"])
                st.delete_chat(parts[1])
                chat_tools.remove_chat_files(st, parts[1])  # its audit and its web tools' pages
                if boxed:
                    import chat_box
                    chat_box.wipe(parts[1], bool(getattr(A, "fake", False)), st)  # pending until Toolbox confirms
                return send_json(h, 200, {"ok": True})
        if len(parts) == 3 and parts[0] == "chats" and parts[2] == "answer" and method == "POST":
            st.chat(parts[1])
            if b.get("decision") == "ask_each":
                st.set_auto_approve(parts[1], False)
                return send_json(h, 200, {"ok": True})
            return send_json(h, 200, {"ok": chat_tools.ASKS.answer(parts[1], str(b.get("ask") or ""), b.get("decision"))})
        if len(parts) == 3 and parts[0] == "chats" and parts[2] == "compact" and method == "POST":
            return compact_now(h, S, parts[1])
        if len(parts) == 3 and parts[0] == "chats" and parts[2] == "goal" and method == "POST":  # /goal ... (chat_goal)
            c = st.chat(parts[1])
            if (c.get("kind") or studio.KIND) != "chat" or c.get("cousteau") or S["outbox"].goals is None:
                return send_json(h, 400, {"error": "a goal needs a chat"})
            try:
                r = S["outbox"].goals.ask(parts[1], str(b.get("text") or ""), b.get("effort") if b.get("effort") in halo_chat.EFFORTS else None)
            except ValueError as e:
                return send_json(h, 400, {"error": str(e)})
            return send_json(h, 200, r) if r else send_json(h, 400, {"error": "not a goal"})
        if len(parts) == 3 and parts[0] == "chats" and parts[2] == "stop" and method == "POST":
            return send_json(h, 200, {"ok": True, "stopped": S["relay"].stop(parts[1])})
        if len(parts) >= 3 and parts[0] == "chats" and parts[2] == "team" and S.get("team") is not None:  # team mode (chat_team)
            team = S["team"]
            if len(parts) == 3 and method == "PUT":
                try:
                    n = int(b.get("cap"))
                except (TypeError, ValueError):
                    raise ValueError("cap: a number")
                if n and (getattr(A, "cfg", None) or {}).get("connect"):
                    import connect
                    raise ValueError(connect.TEAM)
                team.set_cap(parts[1], n)
                return send_json(h, 200, team.view(parts[1]))
            if parts[3:] == ["stop-all"] and method == "POST":
                st.chat(parts[1])
                return send_json(h, 200, {"ok": True, "stopping": team.stop_all(parts[1], now=bool(b.get("now")))})
            if len(parts) == 5 and parts[4] in ("stop", "message", "retry") and method == "POST":  # the Agents panel
                if st.chat(parts[1]).get("private"):
                    return send_json(h, 404, {"error": "no such agent"})
                try:
                    if parts[4] == "stop":
                        team.stop(parts[1], parts[3], "stopped by the user", now=bool(b.get("now")))  # now: Stop now
                    elif parts[4] == "message":
                        team.message(parts[1], parts[3], b.get("text"), who="owner")
                    else:
                        return send_json(h, 200, {"agent": team.retry(parts[1], parts[3])["id"], "team": team.view(parts[1])})
                except KeyError:
                    return send_json(h, 404, {"error": "no such agent"})
                return send_json(h, 200, team.view(parts[1]))
        if len(parts) == 3 and parts[0] == "chats" and parts[2] == "version" and method == "PUT":
            st.show_version(parts[1], int(b.get("group")), int(b.get("version")))
            return send_json(h, 200, {"ok": True})
        if len(parts) == 3 and parts[0] == "waiting" and parts[2] == "now" and method == "POST":  # Send now (chats.Relay.send_now)
            def row_of(oid):
                return st.one("SELECT chat_id, created FROM outbox WHERE id=?", (oid,))
            cid, stopped = S["relay"].send_now(int(parts[1]), row_of)
            return send_json(h, 200, {"ok": True, "taken": cid is None, "stopped": stopped})
        if len(parts) == 2 and parts[0] == "waiting" and method == "DELETE":
            row = st.one("SELECT chat_id FROM outbox WHERE id=?", (int(parts[1]),))
            ok = bool(st.unqueue(int(parts[1])))
            # a chat named after the only message it had, which the user cancelled, is a new chat again
            if ok and row:
                c = st.one("SELECT id FROM chats WHERE id=?", (row["chat_id"],))
                if c and not st.is_private(row["chat_id"]) and not st.one("SELECT 1 FROM messages WHERE chat_id=? LIMIT 1", (row["chat_id"],)) \
                        and not st.waiting(row["chat_id"]):
                    st.rename_chat(row["chat_id"], "New chat")
            return send_json(h, 200, {"ok": ok})
        if parts == ["chat", "model", "start"] and method == "POST":
            # a chat's model, from the Switch bar: only the chat profiles, and never over an answer being written or an
            # agent session (switch_check: the bar names what stops)
            pid = str(b.get("profile") or "")
            if pid not in halo_chat.CHAT_PROFILES:
                return send_json(h, 400, {"error": "profile: one of the chat models"})
            if size_offered(A, pid):
                return send_json(h, 400, {"error": size_offered(A, pid)})
            chk = switch_check(A, S, pid)
            if chk["blockers"]:
                return send_json(h, 409, {"error": "Not now: " + "; ".join(chk["blockers"]) + ". Switch once that is done.",
                                          "why": "busy", "blockers": chk["blockers"]})
            ok, r = A.halod_post("/api/start", {"profile": pid, "replace": bool(b.get("switch"))})
            return send_json(h, 200 if ok else 409, r if ok else {"error": (r or {}).get("error") or "the model could not start"})
        if parts == ["transcribe", "warm"] and method == "POST":  # the microphone is about to be used: load the model now
            T = S.get("transcriber")
            if T and T.available():
                threading.Thread(target=T.warm, daemon=True).start()
            return send_json(h, 200, {"ok": bool(T and T.available())})
        if parts == ["projects"] and method == "POST":
            return send_json(h, 200, st.save_project(None, b.get("name"), b.get("instructions")))
        if len(parts) == 2 and parts[0] == "projects" and method == "PUT":
            return send_json(h, 200, st.save_project(parts[1], b.get("name"), b.get("instructions")))
        if len(parts) == 2 and parts[0] == "projects" and method == "DELETE":
            st.delete_project(parts[1])
            return send_json(h, 200, {"ok": True})
        if len(parts) == 3 and parts[0] == "chats" and parts[2] == "send" and method == "POST":
            return send_stream(h, S, parts[1], b)
    except KeyError:
        return send_json(h, 404, {"error": "not found"})
    except (ValueError, PermissionError) as e:
        return send_json(h, 400, {"error": str(e)})
    return send_json(h, 404, {"error": "not found"})


def secure(h):
    """Secure cookies over the tailnet's HTTPS; a fake dashboard's tailnet preview runs plain HTTP on a test port."""
    return bool(getattr(h, "tailnet", False)) and not h.A.fake


def cookie(token, remember, secure=False):
    """The session cookie: HttpOnly, SameSite=Strict, and Secure over the tailnet's HTTPS (on this Mac's own
    http://127.0.0.1 it stays usable: loopback never leaves the machine)."""
    return (f"{studio.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")
            + (f"; Max-Age={studio.PAIRED_S}" if remember else ""))


def ref_view(st, rid, looker=None):
    try:
        if rid.startswith("u"):
            u = st.upload(rid)
            small = os.path.exists(st.path("refs", f"{rid}.thumb.jpg"))
            return {"id": rid, "kind": u["kind"], "width": u["width"], "height": u["height"], "duration_s": u["duration_s"],
                    "name": u["name"], "url": f"/studio/ref/{rid}", "thumb_url": f"/studio/ref/{rid}.thumb" if small else f"/studio/ref/{rid}",
                    "chars": u.get("chars"), "detail": u.get("detail"),
                    "look": look_state(st, rid, u, looker),
                    **({"doc": u["orig"]} if u.get("orig") else {})}
    except KeyError:
        pass
    return {"id": rid, "missing": True}


def look_state(st, rid, u, looker=None):
    """A photo's description by the Mac's vision model (look.py), for its chip: "seen", "looking", "failed" or None."""
    if u.get("kind") != "image":
        return None
    if looker is not None:
        return looker.state(rid)
    return "seen" if look.look_text(st, rid) is not None else None


def state(A, S):
    st = S["store"]
    hs = A.halod_state() or {}
    p = hs.get("profile") or {}
    return {
        "t": time.time(), "settings": st.settings(), "outbox": len(st.waiting()),
        "llm": {"state": hs.get("state"), "connected": hs.get("connected"), "profile": p.get("id"), "profile_name": p.get("name"), "model_id": p.get("model_id"),
                "model_name": halo_chat.MODEL_NAMES.get(p.get("model_id")) or p.get("name"),
                "unc": p.get("model_id") == "dsv41-unc",
                "reason": failed_words(hs.get("reason")) if hs.get("state") == "failed" else None,  #
                "pairs": pairs_brief(A)} if hs else None,
    }


def speed_view(A, minutes=60):
    """Every pair's speeds over the last `minutes`, from each halod's own request log: by pair and profile (never
    mixed), halod's probes left out. tps: decode tok/s (replies of 16+ tokens); pp: prompt tok/s over the uncached part
    (256+ tokens); wait: time queued for a slot before reading began."""
    import statistics
    med = lambda xs: round(statistics.median(xs), 1) if xs else None  # noqa: E731
    now, out = time.time(), []
    for name, label, st in (A.pair_states() if hasattr(A, "pair_states") else []):
        d = A.pair_get(name, "/api/requests?limit=400") if hasattr(A, "pair_get") else None
        rows = [r for r in ((d or {}).get("requests") or []) if isinstance(r, dict) and not r.get("probe")
                and (r.get("t") or 0) >= now - minutes * 60]
        cur = (st or {}).get("profile") or {}
        if not rows:  # a pair with nothing in this window still shows (what it serves, idle)
            out.append({"pair": name, "label": label, "profile": cur.get("id"), "profile_name": cur.get("name"), "now": True,
                        "state": (st or {}).get("state") or "unreachable", "n": 0, "tps": None, "tps_best": None, "pp": None,
                        "pp_best": None, "wait": None, "last": None, "recent": []})
            continue
        by = {}
        for r in rows:
            by.setdefault(r.get("profile") or "?", []).append(r)
        for prof, rs in sorted(by.items(), key=lambda kv: (kv[0] != cur.get("id"), kv[0])):
            dec = [r["decode_tps"] for r in rs if r.get("decode_tps") and (r.get("tokens") or 0) >= 16]
            pp = [r["prefill_tps"] for r in rs if r.get("prefill_tps") and (r.get("prompt") or 0) - (r.get("cached") or 0) >= 256]
            wait = [max(0.0, (r.get("ttft_s") or 0) - (r.get("prefill_s") or 0)) for r in rs if r.get("ttft_s") is not None]
            rs.sort(key=lambda r: r.get("t") or 0)
            out.append({"pair": name, "label": label, "profile": prof,
                        "profile_name": cur.get("name") if prof == cur.get("id") else prof, "now": prof == cur.get("id"),
                        "state": (st or {}).get("state") if prof == cur.get("id") else None, "n": len(rs),
                        "tps": med(dec), "tps_best": max(dec) if dec else None, "pp": med(pp), "pp_best": max(pp) if pp else None,
                        "wait": med(wait), "last": rs[-1].get("t"),
                        "recent": [{"t": r.get("t"), "in": r.get("prompt"), "cached": r.get("cached"), "out": r.get("tokens"),
                                    "tps": r.get("decode_tps"), "pp": r.get("prefill_tps"),
                                    "wait": round(max(0.0, (r.get("ttft_s") or 0) - (r.get("prefill_s") or 0)), 1)}
                                   for r in rs[-8:]][::-1]})
    return {"minutes": minutes, "pairs": out, "at": now}


def pairs_brief(A):
    """The extra pairs' models for the chat header (a chat whose model one serves goes there: App.chat_state)."""
    out = []
    for name in getattr(A, "pairs", None) or {}:
        b = A.pair_state(name)
        if b:
            p = b.get("profile") or {}
            out.append({"pair": name, "state": b.get("state"), "model_id": p.get("model_id"),
                        "model_name": halo_chat.MODEL_NAMES.get(p.get("model_id")) or p.get("name")})
    return out


def window_view(A, S, chat):
    """The chat's window for its context chip: the size chosen, the size loaded when its model serves, how much of it
    the chat fills (its last answer's own count, else an estimate), and whether a compaction runs."""
    pid = halo_chat.profile_of(chat)
    ctx = halo_chat.context_of(pid)
    hs = A.halod_state() or {}
    if getattr(A, "chat_state", None) and not chat.get("private"):  # the extra pairs' size when they serve this chat's model
        hs = A.chat_state(halo_chat.need(chat)[1], hs, want=ctx) or {}
    p = hs.get("profile") or {}
    same = hs.get("state") in ("ready", "loading", "linking", "waiting") and p.get("model_id") == halo_chat.need(chat)[1]
    loaded = (p.get("settings") or {}).get("context") if same else None
    if loaded:  # what the chat may fill on that pair (halo_chat.chat_window: a 1M pool is not one chat's window)
        loaded = halo_chat.chat_window(chat, int(loaded))
    use = halo_chat.usage_of(chat, loaded or ctx)
    if not use:
        cpt = halo_chat.chars_per_token(chat)
        msgs, _ = S["chat_agent"].history(chat, loaded or ctx, cpt)
        use = {"used": halo_chat.est_tokens(msgs, cpt) if len(msgs) > 1 else 0, "context": loaded or ctx, "measured": False}
    m = halo_chat.model_of(chat)
    k = next((c["k"] for c in m["contexts"] if c["profile"] == pid), f"{round(ctx / 1024)}K")
    return dict(use, chosen=ctx, k=k, profile=pid, loaded=loaded, loaded_profile=p.get("id") if same else None,
                compacting=compacting(S, chat["id"]),
                summaries=sum(1 for x in chat["messages"] if x["role"] == "summary" and not x.get("hidden")))


def size_offered(A, pid):
    """None when a chat profile's window size is offered now (proven for its model and served by the live deploy's
    kit), else why not: a size the composer shows disabled is refused here too."""
    c = next((c for m in halo_chat.CHAT_MODELS for c in m["contexts"] if c["profile"] == pid), None)
    if not c:
        return None
    ok, why = halo_chat.size_ready(c, A.halod_state() or {})
    return None if ok else f"{c['k']} is not offered yet: {why[0].lower() + why[1:]}."


def compacting(S, cid):
    """This chat's conversation is being compacted now (its relay entry says so)."""
    with S["relay"].mu:
        return bool((S["relay"].active.get(cid) or {}).get("compacting"))


def compact_now(h, S, cid):
    """Compact now (the chat's ⋯ menu): the serving model summarises the older turns, on its own thread (the page
    follows the chat's rev). 409 when the chat's model is not the one serving or an answer is being written."""
    A, st, relay = h.A, S["store"], S["relay"]
    chat = st.chat(cid)
    # held from here: busy for a switch, the outbox and a send (taken before anything is checked, so a send
    # and a compaction never both get through); Stop ends it
    entry = relay.claim(cid, compacting=True)
    if entry is None:
        return send_json(h, 409, {"error": "Not while this chat is answering or compacting; try when it has finished."})
    try:
        hs = A.halod_state(fresh=True)
        pid, model_id = halo_chat.need(chat)
        why = S["outbox"].why_not(hs, model_id)
        # the loaded run's own window (halod's report), as a turn uses
        ctx = None if why else int(((hs.get("profile") or {}).get("settings") or {}).get("context") or halo_chat.context_of(pid))
    except BaseException:
        relay.release(cid, entry)
        raise
    if why:
        relay.release(cid, entry)
        return send_json(h, 409, {"error": "The chat's model has to be loaded to make the summary. Load it, then compact.", "why": why})
    ep = None
    if st.is_private(cid):  # the summary reads the whole conversation: the same gate and tunnel as a Private turn
        try:
            got = private_endpoint(S, model_id, hs)
        except BaseException:
            relay.release(cid, entry)
            raise
        if got and got[1]:
            relay.release(cid, entry)
            return send_json(h, 409, {"error": got[1], "why": "private_engine"})
        ep = got[0] if got else None
    ep = ep or chat_endpoint(A, S, hs, cid)
    stop = entry["stop"]

    def run():
        try:
            S["chat_agent"].compact(cid, ep, hs.get("served_model") or "dsv41", ctx, keep=4, manual=True,
                                    cpt=halo_chat.chars_per_token(chat), stop=stop)
        except Exception as e:  # noqa: BLE001 - said on the page; the chat is unchanged
            outbox.log(f"chat compaction {'stopped' if stop.is_set() else 'failed'}: {type(e).__name__}")
        finally:
            relay.release(cid, entry)
            st.x("UPDATE chats SET updated=? WHERE id=?", (time.time(), cid))  # the page's poll sees the end
    try:  # the thread keeps the request's context: a Private chat's grant goes with it
        threading.Thread(target=contextvars.copy_context().run, args=(run,), name="compact", daemon=True).start()
    except BaseException:
        relay.release(cid, entry)
        raise
    return send_json(h, 202, {"compacting": True})


def failed_words(reason):
    """halod's failure reason for the page: its last error, not the rank's log line."""
    r = re.sub(r"\s+", " ", str(reason or "")).strip()
    if "error:" in r:
        r = r.rsplit("error:", 1)[1].strip()
    return (r[:157] + "…") if len(r) > 160 else r


def switch_check(A, S, profile):
    """Loading `profile` now: what stands in its way (an answer being written anywhere, agent sessions on the model:
    never stopped by a switch) and what it would stop or keep waiting, in words for the bar."""
    import profiles as _p
    st = S["store"]
    hs = A.halod_state(fresh=True) or {}
    busy_states = ("ready", "loading", "linking", "waiting")
    loaded = (hs.get("profile") or {}).get("model_id") if hs.get("state") in busy_states else None
    want = ({p["id"]: p for p in _p.BUILTIN}.get(profile) or {}).get("model_id")
    name = lambda mid: halo_chat.MODEL_NAMES.get(mid) or mid or "the model"  # noqa: E731
    blockers = []
    comp = sum(1 for k in list(S["relay"].active) if compacting(S, k))
    writing = len(S["relay"].active) - comp + len(getattr(A, "relay", None).active if getattr(A, "relay", None) else ())
    if writing:
        blockers.append(f"{writing} answer{'s are' if writing != 1 else ' is'} being written")
    if comp:  # a summary of a long chat is being made: stopping the model now would lose it
        blockers.append(f"{comp} conversation{'s are' if comp != 1 else ' is'} being compacted")
    runner = getattr(A, "runner", None)
    agents = sum(1 for x in (runner.view().get("sessions") or []) if x.get("status") == "running") if runner else 0
    if agents:
        blockers.append(f"{agents} agent session{'s are' if agents != 1 else ' is'} using the model")
    if S.get("agents") and S["agents"].in_flight():  # Agents: only a request on the engine now, never a paused run
        blockers.append("an agent request is finishing (seconds)")
    waits = []
    if loaded and loaded != want:
        n = 0
        for w in st.waiting():
            row = st.one("SELECT kind, model FROM chats WHERE id=?", (w["chat_id"],)) or {}
            mid = halo_chat.need(row)[1]
            n += mid == loaded
        if n:
            waits.append(f"{n} waiting message{'s' if n != 1 else ''} for {name(loaded)} will wait until it is back")
    # the same model with another window (the context chip): a restart, not a switch
    lp = (hs.get("profile") or {}).get("id") if hs.get("state") in busy_states else None
    restarts = None
    if loaded and loaded == want and lp and lp != profile:
        k = next((c["k"] for m in halo_chat.CHAT_MODELS for c in m["contexts"] if c["profile"] == profile), None)
        restarts = f"This restarts {name(want)} with a {k} window, about a minute." if k else None
    return {"restarts": restarts, "blockers": blockers, "stops": name(loaded) if loaded and loaded != want else None, "waits": waits,
            "want": name(want), "loaded": name(loaded) if loaded else None}


def chat_send(h, S, chat, b):
    """A chat's answer as server-sent events. It runs on its own thread: when the page goes away (a phone locked
    mid-answer) the answer goes on and is kept, and the page follows it when it comes back. When the chat's model is
    not the one serving, the message waits (the outbox) and is sent once it serves."""
    A, st = h.A, S["store"]
    cid = chat["id"]
    # the chat is claimed first of all, under the relay's lock (two sends, or a send and Compact now, both got through
    # a check-then-register gap before): every way out before the answer starts gives it back
    entry = S["relay"].claim(cid)
    if entry is None:
        if compacting(S, cid):
            return send_json(h, 409, {"error": "this chat is being compacted; send when it has finished, or stop it"})
        plain = (b.get("mode") or "send") == "send"
        if not plain:
            return send_json(h, 409, {"error": "this chat is answering already; stop it first"})
        return queue_while_answering(h, S, chat, b)
    q = None
    try:
        hs = A.halod_state(fresh=True)
        pid, model_id = halo_chat.need(chat)
        if getattr(A, "chat_state", None):  # the extra pairs when they serve this chat's model, else the main boxes
            hs = A.chat_state(model_id, hs, bool(chat.get("private")), want=halo_chat.context_of(pid), key=chat["id"])
        mode = b.get("mode") or "send"
        effort = b.get("effort") if b.get("effort") in halo_chat.EFFORTS else None
        why = S["outbox"].why_not(hs, model_id)
        if mode in ("send", "edit"):  # a message alone bigger than the chat's model takes: refused now, not after a swap
            chars = len(str(b.get("content") or ""))
            for a in b.get("attachments") or []:
                try:
                    chars += halo_chat.attachment_chars(st, st.upload(str((a or {}).get("id"))), S.get("looker"))
                except (KeyError, TypeError, ValueError):
                    pass
            big = halo_chat.too_long(chat, chars, hs)
            if big:
                return send_json(h, 400, {"error": big, "why": "too_long"})
        if why or waiting_before(st, cid, entry):
            if mode != "send":
                return send_json(h, 409, {"error": "edit and regenerate need the chat's model loaded first", "why": why})
            content = str(b.get("content") or "")
            atts = [{"id": str((a or {}).get("id"))} for a in b.get("attachments") or []]
            if not content.strip() and not atts:
                return send_json(h, 400, {"error": "the message is empty"})
            for a in atts:
                st.upload(a["id"])  # it exists
            oid = st.enqueue(cid, content[:100_000], dict({"attachments": atts, "effort": effort}, **({"no_tools": True} if b.get("no_tools") else {})))
            if chat["title"] == "New chat" and content.strip() and not any(m["role"] == "user" for m in chat["messages"]):
                t = re.sub(r"\s+", " ", content).strip()
                st.rename_chat(cid, (t[:59] + "…") if len(t) > 60 else t)  # named now, not when the model comes
            return send_json(h, 202, {"queued": True, "id": oid, "why": why or "after the messages before it", "model": pid})
        tools = not b.get("no_tools")
        if tools and chat_tools.level_of(chat) in ("box", "box_web") and not chat.get("cousteau"):
            import chat_box
            bb_ok, bb = chat_box.health(bool(getattr(A, "fake", False)), store=st)
            if not bb_ok:
                return send_json(h, 409, {"error": chat_box.NO_DISK if bb.get("chats_fs") is False else chat_box.OFFLINE, "why": "toolbox"})
        ep = chat_endpoint(A, S, hs, cid)
        if chat.get("private"):  # only an engine whose boxes pass the facts, through the app's own private tunnel
            ep, refusal = private_endpoint(S, model_id, hs) or (ep, None)
            if refusal:
                return send_json(h, 409, {"error": refusal, "why": "private_engine"})
        gen = S["chat_agent"].turn(cid, ep, hs.get("served_model") or "dsv41", (hs.get("profile") or {}).get("id"),
                                   b.get("content"), b.get("attachments"), mode, effort,
                                   served_context=((hs.get("profile") or {}).get("settings") or {}).get("context"), claim=entry,
                                   tools=tools)
        try:
            first = next(gen)
        except (ValueError, KeyError) as e:
            return send_json(h, 400, {"error": str(e) if isinstance(e, ValueError) else "that attachment is gone"})
        q = halo_chat.run_detached(gen, first)
    finally:
        if q is None:  # refused, queued, or the turn's own checks said no: the chat is free again
            S["relay"].release(cid, entry)
    detached_events(h, q)


def queue_while_answering(h, S, chat, b):
    """A message the user sends while the chat is answering: it waits in the outbox marked steer. A turn with tools
    takes it in at its next step (halo_chat._take_steers), so the model reads it before it goes on; otherwise the outbox
    sends it when the answer ends (Stop sends it at once)."""
    st, cid = S["store"], chat["id"]
    content = str(b.get("content") or "")
    atts = [{"id": str((a or {}).get("id"))} for a in b.get("attachments") or []]
    if not content.strip() and not atts:
        return send_json(h, 400, {"error": "the message is empty"})
    chars = len(content)
    try:
        for a in atts:
            chars += halo_chat.attachment_chars(st, st.upload(a["id"]), S.get("looker"))
    except (KeyError, TypeError, ValueError):
        return send_json(h, 400, {"error": "that attachment is gone"})
    big = halo_chat.too_long(chat, chars)
    if big:
        return send_json(h, 400, {"error": big, "why": "too_long"})
    effort = b.get("effort") if b.get("effort") in halo_chat.EFFORTS else None
    with S["relay"].mu:  # the answer it came during (waiting_before: a send still starting never waits behind it)
        cur = S["relay"].active.get(cid)
        tok = cur.setdefault("tok", secrets.token_hex(6)) if cur is not None else None
    meta = dict({"attachments": atts, "effort": effort, "steer": True}, **({"steer_of": tok} if tok else {}),
                **({"no_tools": True} if b.get("no_tools") else {}))
    oid = st.enqueue(cid, content[:100_000], meta)
    return send_json(h, 202, {"queued": True, "id": oid, "why": "answering", "steer": True})


def waiting_before(st, cid, entry):
    """The waiting messages a send that holds the chat (entry) goes behind: all but those the user sent while it was starting
    (queue_while_answering marks them with its claim), which come after it. Read before the mark: a row seen here was
    marked before it was written."""
    rows = st.waiting(cid)
    tok = entry.get("tok")
    return [w for w in rows if not (tok and (w.get("meta") or {}).get("steer_of") == tok)]


def detached_events(h, q):
    """A detached turn's events as server-sent events, until it ends or the page goes away (the turn goes on and is
    stored as it comes)."""
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    h.close_connection = True
    while True:
        ev = q.get()
        if ev is None:
            break
        try:
            h.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
            h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            break


def send_stream(h, S, cid, b):
    """A chat's answer as server-sent events (one JSON object per data: line)."""
    c = S["store"].chat(cid)
    return chat_send(h, S, c, b)


def upload(h, S):
    """An attached file: raw bytes with the page token and a session; the type is read from the bytes."""
    if not h.host_ok():
        return send_json(h, 403, {"error": "host not allowed"})
    origin = h.headers.get("Origin")
    if origin and origin not in h.origins():  # https://<tailnet name> on the tailnet listener, http://<host> elsewhere
        return send_json(h, 403, {"error": "origin not allowed"})
    if not secrets.compare_digest(h.headers.get("X-Halo-UI-Token") or "", h.expected_token()):
        return send_json(h, 403, {"error": "missing or wrong token"})
    if not device(h):
        return send_json(h, 401, {"error": "locked"})
    ctype = (h.headers.get("Content-Type") or "").lower()
    for_chat = h.headers.get("X-Upload-For") == "chat"  # a chat's: any file (the bytes decide); else an image, audio or video
    if not for_chat and not ctype.startswith(UPLOAD_TYPES):
        return send_json(h, 415, {"error": "an image, audio or video file"})
    n = int(h.headers.get("Content-Length") or 0)
    if n <= 0 or n > max(studio.UPLOAD_MAX.values()):
        return send_json(h, 413, {"error": f"files up to {max(studio.UPLOAD_MAX.values()) >> 20} MB"})
    data = h.rfile.read(n)
    cid = h.headers.get("X-Chat") or None
    if h.headers.get("X-Private") == "1" and not (cid and S["store"].is_private(cid)):
        # a Private composer's file is only ever saved sealed with its chat: never as an ordinary upload, which every
        # upload-time reader (the look on this Mac, OCR, a thumbnail) would process in plaintext
        return send_json(h, 400, {"error": "A Private chat's file is saved only with its chat. Try again in the chat."})
    # an Office file is read as text up to what the chat's model can take with room for the question and the answer
    limit = int(halo_chat.context_of(h.headers.get("X-Model") if h.headers.get("X-Model") in halo_chat.CHAT_PROFILES else halo_chat.DEFAULT_MODEL)
                * halo_chat.CHARS_PER_TOKEN * 0.6)
    try:
        u = S["store"].add_upload(data, unquote(h.headers.get("X-Filename") or ""), cid, text_ok=for_chat, text_limit=limit)
    except ValueError as e:
        return send_json(h, 400, {"error": str(e)})
    if for_chat and u["kind"] in ("audio", "video"):  # the text models read its transcript (local Whisper, first 10 min)
        S["store"].set_upload_text(u["id"], *media_text(S, data, u, limit))
    L = S.get("looker")
    if for_chat and u["kind"] == "image" and not u.get("private") and L is not None and L.available() and \
            look_at_upload(S["store"], h.headers.get("X-Chat"), h.headers.get("X-Model")):  # never a Private chat's photo
        L.start(u["id"])  # described on this Mac while the user types (look.py), for a model that reads text only
    return send_json(h, 200, ref_view(S["store"], u["id"], L))


def look_at_upload(st, chat_id, model):
    """Whether a chat's photo is described on the Mac as soon as it is uploaded (look.py): not when the chat's model
    looks at photos itself (the composer's guess, as its words; the turn decides by the engine that serves it, and
    describes then if it must), nor in a Private chat (the look is off there: chat_look.private_chat)."""
    import chat_look
    if halo_chat.model_of({"model": model}).get("vision"):
        return False
    return not chat_look.private_chat(st.one("SELECT * FROM chats WHERE id=?", (chat_id,)) if chat_id else None)


def media_text(S, data, u, limit):
    """(text, chip words) for an attached audio or video file: its sound track transcribed on this Mac."""
    import filetext
    mmss = lambda x: f"{int(round(x)) // 60}:{int(round(x)) % 60:02d}"  # noqa: E731
    head = f"{u['kind']} · {mmss(u['duration_s'])}" if u.get("duration_s") else u["kind"]
    T = S.get("transcriber")
    if not T or not T.available():
        return "", f"{head} · not transcribed (dictation is not set up)"
    try:
        text, total, secs = T.transcribe_media(data)
    except (ValueError, RuntimeError) as e:
        return "", f"{head} · not transcribed ({e})"
    head = f"{u['kind']} · {mmss(total)}"  # its length as the sound track measures it
    if not text:
        return "", f"{head} · no speech found"
    text, cut = filetext.cap(text, limit, "transcript")
    more = f" · first {mmss(secs)} transcribed" if secs + 1 < total else ""
    note = f"\n\n(the transcript covers the first {mmss(secs)} of {mmss(total)})" if more else ""
    return f"Transcript of its sound (Whisper, on the user's Mac):\n{text}{note}", f"{head} · {len(text.split()):,} words{more}"


def raw_guard(h):
    """The checks a raw (non-JSON) write passes: Host, an allowed Origin, the page token, a device session. None when
    they pass, else the response already sent (after reading the body, up to the dictation cap, so a client still
    sending a recording reads the refusal instead of a broken connection)."""
    why = None
    if not h.host_ok():
        why = (403, "host not allowed")
    elif (h.headers.get("Origin") or None) and h.headers.get("Origin") not in h.origins():
        why = (403, "origin not allowed")
    elif not secrets.compare_digest(h.headers.get("X-Halo-UI-Token") or "", h.expected_token()):
        why = (403, "missing or wrong token")
    elif not device(h):
        why = (401, "locked")
    if why is None:
        return None
    n = int(h.headers.get("Content-Length") or 0)
    if 0 < n <= studio_transcribe.MAX_BYTES:
        h.rfile.read(n)
    return send_json(h, why[0], {"error": why[1]})


def transcribe(h, S):
    """A recording from the chat's microphone, as text for the composer (studio_transcribe.py: on this Mac, deleted
    once read). Nothing about it is logged."""
    bad = raw_guard(h)
    if bad is not None:
        return bad
    T = S.get("transcriber")
    if not T or not T.available():
        return send_json(h, 503, {"error": "dictation is not set up on this Mac"})
    ctype = (h.headers.get("Content-Type") or "").lower()
    if not ctype.startswith(studio_transcribe.TYPES):
        return send_json(h, 415, {"error": "an audio recording"})
    n = int(h.headers.get("Content-Length") or 0)
    if n <= 0:
        return send_json(h, 400, {"error": "nothing was recorded"})
    if n > studio_transcribe.MAX_BYTES:
        return send_json(h, 413, {"error": "recordings up to two minutes"})
    data = h.rfile.read(n)
    try:
        return send_json(h, 200, {"text": T.transcribe(data, ctype)})
    except ValueError as e:
        return send_json(h, 400, {"error": str(e)})
    except RuntimeError as e:
        return send_json(h, 503, {"error": str(e)})
    except Exception:  # noqa: BLE001  (always an answer: the page must not be left with a dropped connection)
        return send_json(h, 500, {"error": "the recording could not be turned into text; try again, or use the keyboard's dictation"})


def stream_file(h, path, mime, name, dl, csp=None):
    try:
        size = os.path.getsize(path)
    except OSError:
        return send_json(h, 404, {"error": "not found"})
    r = studio.parse_range(h.headers.get("Range"), size)
    if r is False:
        h.send_response(416)
        h.send_header("Content-Range", f"bytes */{size}")
        h.send_header("Content-Length", "0")
        h.end_headers()
        return
    start, end = r if r else (0, size - 1)
    h.send_response(206 if r else 200)
    h.send_header("Content-Type", mime)
    h.send_header("Content-Length", str(end - start + 1))
    if r:
        h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Cross-Origin-Resource-Policy", "same-origin")
    h.send_header("Referrer-Policy", "no-referrer")
    h.send_header("Content-Security-Policy", csp or "default-src 'none'; sandbox")
    h.send_header("Content-Disposition", f'{"attachment" if dl else "inline"}; filename="{name}"')
    h.end_headers()
    if h.command == "HEAD":
        return
    with open(path, "rb") as f:
        f.seek(start)
        left = end - start + 1
        try:
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                h.wfile.write(chunk)
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def doc_file(h, uid, dl=False):
    """A chat document's original (chat_docs): a PDF opens in the browser's own viewer (no CSP: Chrome's viewer is a
    plugin a sandbox or object-src 'none' would block; the bytes are checked to be a PDF, and nosniff keeps them one),
    anything else, or ?dl=1, downloads. A Private chat's is opened in memory with the grant, never written."""
    import docs
    d = _doc(h, uid)
    if not d:
        return send_json(h, 404, {"error": "not found"})
    u, body, kind = d
    name = docs.file_name(u.get("name"), "document", "." + kind)
    ascii_name = re.sub(r'[^A-Za-z0-9 ._()-]', "_", name)
    inline = kind == "pdf" and not dl
    h.send_response(200)
    h.send_header("Content-Type", docs.MIME.get(kind, "application/octet-stream"))
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Cross-Origin-Resource-Policy", "same-origin")
    h.send_header("Referrer-Policy", "no-referrer")
    if not inline:
        h.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
    h.send_header("Content-Disposition", f'{"inline" if inline else "attachment"}; filename="{ascii_name}"; '
                                         f"filename*=UTF-8''{quote(name, safe='')}")
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)
    return None


def _doc(h, uid):
    """A chat document's (record, original bytes, kind), or None: not one, not kept whole, or a Private chat's without
    its grant."""
    st = h.A.studio["store"]
    try:
        u = st.upload(uid)  # a Private chat's: Locked without the grant
        body = st.upload_orig(uid) if u.get("orig") else None
    except (KeyError, ValueError, OSError):
        return None
    kind = u.get("orig") if body is not None else None
    if not kind or (kind == "pdf" and body[:5] != b"%PDF-"):
        return None
    return u, body, kind


PAGES = {}            # (upload id, page) -> its JPEG, drawn for the viewer (never a Private chat's)
PAGES_KEEP = 64
PAGES_AT_ONCE = 2     # pages drawn at once: each runs the helper (or the sealed reader) on the whole PDF
_pages_mu = threading.Lock()
_pages_run = threading.BoundedSemaphore(PAGES_AT_ONCE)


def doc_view(h, uid, n):
    """A PDF in the app's own viewer (studio.js renderDoc), drawn here by PDFKit: n 0 is {"pages", "sizes"}, else page n
    as a JPEG. The installed app (display: standalone) shows a PDF it opens with no way back, and a phone's browser's own
    viewer differs from a Mac's. A Private chat's pages are drawn by the sealed reader and kept nowhere."""
    import docs
    d = _doc(h, uid)
    if not d or d[2] != "pdf":
        return send_json(h, 404, {"error": "not found"})
    u, body, _ = d
    private = bool(u.get("private"))
    try:
        if not n:
            i = docs.info(body, private=private)
            return send_json(h, 200, {"pages": int(i.get("pages") or 0), "sizes": i.get("sizes") or []})
        with _pages_mu:
            jpeg = None if private else PAGES.get((uid, n))
        if jpeg is None:
            with _pages_run:  # a fast scroll through a long PDF asks for many at once
                jpeg = docs.page(body, n, side=1600, private=private)["jpeg"]
            if not private:
                with _pages_mu:
                    PAGES[(uid, n)] = jpeg
                    while len(PAGES) > PAGES_KEEP:
                        PAGES.pop(next(iter(PAGES)))
    except docs.DocError as e:
        return send_json(h, 422, {"error": str(e)})
    h.send_response(200)
    h.send_header("Content-Type", "image/jpeg")
    h.send_header("Content-Length", str(len(jpeg)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Cross-Origin-Resource-Policy", "same-origin")
    h.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(jpeg)
    return None


def ref(h, uid, small=False):
    st = h.A.studio["store"]
    try:
        u = st.upload(uid)  # a Private chat's: Locked without the grant (404 locked)
    except KeyError:
        return send_json(h, 404, {"error": "not found"})
    if u.get("private"):  # opened in memory, never written; no thumbnail of a private photo exists
        if u.get("kind") != "image":
            return send_json(h, 404, {"error": "not found"})
        body = st.upload_bytes(uid)
        h.send_response(200)
        h.headers_common(u["mime"] or "application/octet-stream", len(body))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(body)
        return None
    if small and os.path.exists(st.path("refs", f"{uid}.thumb.jpg")):
        return stream_file(h, st.path("refs", f"{uid}.thumb.jpg"), "image/jpeg", f"reference-{uid[:9]}-small.jpg", False)
    return stream_file(h, st.path("refs", f"{uid}.{u['type']}"), u["mime"], f"reference-{uid[:9]}.{u['type']}", False)
