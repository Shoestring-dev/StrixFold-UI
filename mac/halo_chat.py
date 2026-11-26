"""StrixFold UI's chat: a conversation with one of the text models on the boxes, in the private store and behind its passcode.

A chat is a row of the store's `chats` table (studio.Store) with kind "chat", which keeps its messages, versions (edit,
regenerate), waiting messages (the outbox) and revs. What this adds: the model is chosen per chat from the built-in profiles that suit a
conversation (CHAT_MODELS), the effort maps to the engine's own request settings (thinking on or off, the reply budget),
an answer keeps going on the Mac when the page goes away (a phone locked mid-reply), and text files are read into the
message. Photos: Qwen 3.8 on Kyojin looks at them itself (VISION_MODELS); the other models get the text read from them
on the Mac, and are told they can't see the picture.
"""
import base64
import http.client
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import traceback
from urllib.parse import urlparse

import chat_loops
import chat_tools as _tools
import look
import studio
import mediainfo  # noqa: E402  (box/mediainfo.py, on sys.path through studio)
import profiles as _profiles  # noqa: E402  (box/profiles.py, on sys.path through studio)

# The models a chat may run, in the order the model sheet shows them, each with the context sizes it is proven at:
# a size is a whitelisted built-in profile (CONTEXT, PEAK_GIB, prompt cache), never a free-form value; a chat's
# `model` is the profile of the size chosen. Coding-agent and benchmark profiles are not here: they are the
# dashboard's. 128K and 256K wait for the team's checks (and a bigger RoPE table than serve_lib's 65,487 cap).
# The cost of each size, in plain words: the team's first read of a full window, then a cached follow-up (measured; 512K
# for both and the official 1M are estimates, so every line says "about"). The follow-up is end to end from this Mac:
# a cached 387K-token prompt took 2.7-3.3 s over the direct tunnels, about 2.4 s of it on the boxes.
SIZE_NOTES = {
    "16K": "Fastest start",
    "64K": "Everyday. A full window takes about 46 s to read the first time, then about a second",
    "128K": {"dsv41": "A full window takes about 80 s to read the first time, then a second or two",
             "dsv41-unc": "A full window takes about 95 s to read the first time, then a second or two"},
    "256K": {"dsv41": "A full window takes about 2.8 min to read the first time, then a second or two",
             "dsv41-unc": "A full window takes about 3.3 min to read the first time, then a second or two"},
    "512K": {"dsv41": "A full window takes about 6 min to read the first time, then a few seconds",
             "dsv41-unc": "A full window takes about 7.5 min to read the first time, then a few seconds"},
    "1M": {"dsv41": "A full window takes about 13 min to read the first time, then a few seconds",
           "dsv41-unc": "A full window takes about 16 min to read the first time, then a few seconds"},
}


def _sizes(base, small, model_id):
    """A DeepSeek model's sizes: 16K and 64K, then profiles.LONG_SIZES (each offered only once proven and served)."""
    out = [{"k": "16K", "profile": small}, {"k": "64K", "profile": base}]
    out += [{"k": label, "profile": f"{base}-{label.lower()}"} for label, _, _ in _profiles.LONG_SIZES]
    note = lambda k: (SIZE_NOTES.get(k) or {}).get(model_id) if isinstance(SIZE_NOTES.get(k), dict) else SIZE_NOTES.get(k)  # noqa: E731
    return [dict(c, note=note(c["k"]) or "Long files") for c in out]


def _qwen_sizes(model_id):
    """A community Qwen engine's sizes (profiles.QWEN_SIZES: 16K to 256K, Kyojin's 512K marked experimental), each
    offered once its measured run has proven it."""
    return [{"k": label, "profile": f"{model_id}-{label.lower()}",
             "note": "Experimental (past the model's own 262,144)" if ctx > 262144 else ("Long files" if ctx > 65536 else "Everyday")}
            for label, ctx, per in _profiles.QWEN_SIZES if model_id in per]


CHAT_MODELS = [
    {"profile": "quick-chat", "name": "DeepSeek V4.1", "desc": "The official model, for everyday questions and long files", "main": True,
     "contexts": _sizes("quick-chat", "quick-chat-16k", "dsv41")},
    {"profile": "unc-chat", "name": "DeepSeek uncensored", "desc": "Answers what the official model refuses", "main": True,
     "contexts": _sizes("unc-chat", "unc-chat-16k", "dsv41-unc")},
    # Qwen 3.8 on Halogen, a copy on each box (it replaced halo-qwen's Gufo, Gufo v0.21 and Kyojin). It reads photos
    # itself.
    {"profile": "qwen38-halogen-64k", "name": "Qwen 3.8", "desc": "Another model family, for a second opinion. It can look at photos",
     "main": True, "vision": True, "contexts": _qwen_sizes("qwen38-halogen")},
]
# chats from before the context chip: their "long files" profiles are now the 64K size of the same model; chats on the
# Qwen engines Halogen replaced continue on Halogen at the same size (Kyojin's 512K at Halogen's 128K)
LEGACY = {"long-documents": "quick-chat", "unc-agent": "unc-chat", "qwen38-flash-next": "qwen38-halogen-64k",
          **{f"qwen38-kyojin-{k}": f"qwen38-halogen-{k}" for k in ("16k", "64k", "128k", "256k")},
          "qwen38-kyojin-512k": "qwen38-halogen-128k",
          "quick-chat-64k": "quick-chat"}  # (SERVER_64K: the 64K itself, apart from connect.py's server)
SERVER_64K = "quick-chat-64k"  # connect.py: the default model's 64K beside its server's own window, which takes its place
# Each model's one name in StrixFold UI (the chat, the header), by halod's model id
MODEL_NAMES = {"dsv41": "DeepSeek V4.1", "dsv41-unc": "DeepSeek uncensored", "qwen38": "Qwen 3.8", "qwen38-kyojin": "Qwen 3.8 (Kyojin)",
               "qwen38-gufo21": "Qwen 3.8 (Gufo 0.21)", "qwen38-halogen": "Qwen 3.8"}
CHAT_PROFILES = {c["profile"] for m in CHAT_MODELS for c in m["contexts"]} | {m["profile"] for m in CHAT_MODELS} | set(LEGACY)


def size_ready(c, hs=None):
    """(offered, why not): a size is offered once the team (DeepSeek) or the team's measured run (the Qwen engines) has
    proven it for its model (the profile's proven flag) and it fits what serves it: DeepSeek's deploy RoPE kit (halod's
    limits; the bundle kit's 65,487 without a report), a Qwen engine's own longest window."""
    p = _builtin().get(LEGACY.get(c["profile"], c["profile"]) if c["profile"] == SERVER_64K else c["profile"]) or {}
    if (hs or {}).get("connected") and c["profile"] in own_sizes():  # connect.py: its server's window holds any size up to it
        n = int((((hs or {}).get("profile") or {}).get("settings") or {}).get("context") or context_of(DEFAULT_MODEL))
        mine = int((p.get("settings") or {}).get("context") or 0)
        return (True, None) if c["profile"] == DEFAULT_MODEL or (p and mine <= n) else (False, "Bigger than your server's window")
    if not p:
        return False, "Not tested yet"
    if p.get("proven") is False:
        return False, "Not tested yet"
    own = _profiles.model_window(p.get("model_id"))
    limit = own or ((hs or {}).get("limits") or {}).get(c["profile"]) or _profiles.ROPE_ROWS
    if int((p.get("settings") or {}).get("context") or 0) > int(limit):
        return False, "Longer than its engine serves" if own else "Needs a deploy with the long RoPE kit"
    return True, None
DEFAULT_MODEL = "quick-chat"


def shown_size(pid, n):
    """connect.py: the size a chat shows as on a server of window n: a size the window caps is the server's own."""
    if pid in own_sizes() and pid != DEFAULT_MODEL and context_of(pid) >= n:
        return DEFAULT_MODEL
    return pid


def own_sizes():
    """The default model's sizes: the one model connect.py's server runs, at any of them up to its window."""
    return {c["profile"] for c in next(m for m in CHAT_MODELS if m["profile"] == DEFAULT_MODEL)["contexts"]} | {SERVER_64K}


# Effort, as the engine understands it (both families take enable_thinking per request; max_tokens is per request too)
EFFORTS = {
    "low": {"name": "Low", "desc": "Answers at once, no thinking", "thinking": False, "max_tokens": None},
    "high": {"name": "High", "desc": "Thinks before answering", "thinking": True, "max_tokens": 4096},
    "extra": {"name": "Extra", "desc": "Thinks, with room for long answers", "thinking": True, "max_tokens": 8192},
}
DEFAULT_EFFORT = "low"
SYSTEM = ("You are StrixFold UI, a helpful assistant that runs privately on the user's own computers: nothing they write here leaves "
          "their machines. Answer plainly and directly, in their language. Use Markdown when it helps (lists, tables, code "
          "blocks with the language named). You read text only: when the user attaches a photo you get the text recognised "
          "in it on their Mac, never the picture. When that matters, say you can't see the photo, and that Qwen 3.8 "
          "in the model menu can look at photos.")
SEES = ("You can see photos: the newest ones the user attached come with their messages as pictures. Older ones come as the "
        "text recognised in them; if one of those matters, ask them to attach it again.")
MAX_FILE_CHARS = 400_000

# ---------------------------------------------------------------- photos. Kyojin's Qwen 3.8 pack is a vision model and
# Kyojin's server loads its vision tower by
# default (OpenAI image_url parts, data: URIs). A photo goes as a picture only when the model
# SERVING the turn is one of VISION_MODELS and that engine's own /health says its vision is on (never because the chat
# chose it); otherwise as the text read from it on this Mac, as before. Only data: URIs made here from the user's
# uploads are ever sent: Kyojin would also fetch an http(s) address given as an image.
VISION_MODELS = ("qwen38-kyojin", "qwen38-halogen")  # halod model ids
VISION_PICTURES = 4                 # the newest photos of the conversation go as pictures; older ones as their text
VISION_PIXELS = 1 << 20             # shrunk to about a megapixel first: the pack takes up to 16 MP, ~16,000 tokens
VISION_TOKENS = 1100                # one such picture in the window, about (a token per 32 x 32 pixels, its markers)
VISION_MAX_BYTES = 6 << 20
VISION_TTL_S = 30                   # how long an engine's answer about its vision is trusted
_VISION = {}                        # endpoint -> (time asked, sees)
_VISION_MU = threading.Lock()


def sees_images(endpoint, model_id, now=None):
    """Whether the engine serving `model_id` at `endpoint` reads pictures now: a VISION_MODELS model whose /health
    answers {"source": "kyojin", "vision": true}. Any doubt (no answer, loading, another engine) is no."""
    if model_id not in VISION_MODELS or not endpoint:
        return False
    now = time.time() if now is None else now
    with _VISION_MU:
        hit = _VISION.get(endpoint)
    if hit and now - hit[0] < VISION_TTL_S:
        return hit[1]
    u, sees = urlparse(endpoint), False
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=4)
        try:
            conn.request("GET", "/health")
            r = conn.getresponse()
            body = json.loads(r.read(65536) or b"{}") if r.status == 200 else {}
        finally:
            conn.close()
        sees = isinstance(body, dict) and body.get("source") == "kyojin" and body.get("vision") is True
    except (OSError, http.client.HTTPException, ValueError):
        sees = False
    with _VISION_MU:
        _VISION[endpoint] = (now, sees)
    return sees


# A phone keeps a portrait photo as landscape pixels plus an EXIF Orientation tag, and the store keeps that tag so the
# photo shows upright. Kyojin's server opens images without reading the tag, so the copy it gets is turned upright
# here. sips flips before it rotates within one call (measured), hence 5 and 7's angles.
UPRIGHT = {2: ["-f", "horizontal"], 3: ["-r", "180"], 4: ["-f", "vertical"], 5: ["-r", "270", "-f", "horizontal"],
           6: ["-r", "90"], 7: ["-r", "90", "-f", "horizontal"], 8: ["-r", "270"]}


def _orientation(path):
    try:
        with open(path, "rb") as f:
            return mediainfo.jpeg_orientation(f.read(1 << 18))  # the tag sits in the first segments
    except OSError:
        return None


def _without_exif(d):
    """A JPEG without its EXIF segments (the vision copy needs none; sips carries the Orientation tag over)."""
    out, i = [d[:2]], 2
    while i + 4 <= len(d) and d[i] == 0xFF:
        m = d[i + 1]
        if m == 0xDA or m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            break
        n = int.from_bytes(d[i + 2:i + 4], "big")
        if not (m == 0xE1 and d[i + 4:i + 10] == b"Exif\x00\x00"):
            out.append(d[i:i + 2 + n])
        i += 2 + n
    out.append(d[i:])
    return b"".join(out)


def picture(store, uid):
    """A photo as a vision model gets it: a JPEG of about a megapixel made by macOS's sips, kept beside the upload as
    refs/<id>.see.jpg (600), as a data: URI. None when it can't be made (the model then gets the photo's text)."""
    try:
        u = store.upload(uid)
    except KeyError:
        return None
    if u.get("kind") != "image":
        return None
    if u.get("private"):  # a Private chat's photo: made in memory by Private's own reader, never written
        import private_reader
        try:
            got = private_reader.reader().see(store.upload_bytes(uid), VISION_PIXELS)
        except (private_reader.ReaderError, OSError, ValueError):
            return None
        if not got or not 0 < len(got[0]) <= VISION_MAX_BYTES:
            return None
        return "data:image/jpeg;base64," + base64.b64encode(got[0]).decode()
    out = store.path("refs", f"{uid}.see.jpg")
    if not os.path.exists(out):
        src = store.path("refs", f"{uid}.{u.get('type')}")
        w, h = int(u.get("width") or 0), int(u.get("height") or 0)
        if not (w and h and os.path.exists(src)):
            return None
        scale = min(1.0, (VISION_PIXELS / (w * h)) ** 0.5)
        tmp = f"{out}.tmp-{secrets.token_hex(4)}"
        turn = UPRIGHT.get(_orientation(src) if u.get("type") == "jpeg" else None, [])
        argv = ["/usr/bin/sips"] + turn + (["-Z", str(max(32, int(max(w, h) * scale)))] if scale < 1.0 else []) + \
               ["-s", "format", "jpeg", "-s", "formatOptions", "85", src, "--out", tmp]
        try:
            r = subprocess.run(argv, capture_output=True, timeout=60)
            if r.returncode != 0 or not os.path.exists(tmp):
                return None
            with open(tmp, "rb") as f:
                d = f.read()
            if turn or mediainfo.jpeg_orientation(d) not in (None, 1):
                with open(tmp, "wb") as f:
                    f.write(_without_exif(d))  # the pixels are upright now: the tag would turn them again
            os.chmod(tmp, 0o600)
            os.replace(tmp, out)
        except (OSError, subprocess.TimeoutExpired):
            return None
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    try:
        with open(out, "rb") as f:
            data = f.read(VISION_MAX_BYTES + 1)
    except OSError:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(data).decode() if 0 < len(data) <= VISION_MAX_BYTES else None


def content_chars(content, cpt=None):
    """A message's size in characters as the window counts it: its text, and each picture as VISION_TOKENS tokens."""
    if isinstance(content, list):
        per = int(VISION_TOKENS * (cpt or CHARS_PER_TOKEN_GUESS))
        return sum((len(p.get("text") or "") if p.get("type") == "text" else per) for p in content if isinstance(p, dict))
    return len(content or "")


def text_of(content):
    """A message's text: its own string, or its text parts (pictures left out)."""
    if isinstance(content, list):
        return " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content or "")


def trim_middle(text, head=None, tail=None):
    """A long tool output cut to its ends, saying how much went (a mid-turn compaction without its summary)."""
    t, head, tail = str(text or ""), head or TRIM_HEAD, tail or TRIM_TAIL
    if len(t) <= head + tail + 80:
        return t
    return f"{t[:head]}\n[... {len(t) - head - tail:,} characters cut to make room ...]\n{t[-tail:]}"


def pictures_in(msgs):
    return sum(1 for m in msgs if isinstance(m.get("content"), list) for p in m["content"] if p.get("type") == "image_url")


def _builtin():
    return {p["id"]: p for p in _profiles.BUILTIN}


def profile_of(chat):
    """The profile a chat's messages run on: its chosen size of its model (a legacy "long files" chat: that model's 64K)."""
    pid = chat.get("model") if chat.get("model") in CHAT_PROFILES else DEFAULT_MODEL
    if pid == SERVER_64K and CONNECTED is not None and CONNECTED():
        return pid
    return LEGACY.get(pid, pid)


def model_of(chat):
    """The chat's model entry (a known chat profile, at any of its sizes; else the default)."""
    pid = profile_of(chat)
    pid = LEGACY.get(pid, pid)
    return next(m for m in CHAT_MODELS if pid == m["profile"] or pid in [c["profile"] for c in m["contexts"]])


def models(hs=None):
    """The model sheet: each chat profile with its model, context and whether it (or its model) is loaded now."""
    b, p = _builtin(), (hs or {}).get("profile") or {}
    serving = p.get("model_id") if (hs or {}).get("state") in ("ready", "loading", "linking", "waiting") else None
    out = []
    for m in CHAT_MODELS:
        prof = b.get(m["profile"]) or {}
        sizes = []
        for c in m["contexts"]:
            ok, why = size_ready(c, hs)
            sizes.append(dict(c, context=context_of(c["profile"]), ready=ok, why=why))
        out.append(dict(m, contexts=sizes, model_id=prof.get("model_id"), context=(prof.get("settings") or {}).get("context"),
                        loaded=bool(serving and serving == prof.get("model_id")), state=(hs or {}).get("state") if serving == prof.get("model_id") else None))
    if (hs or {}).get("connected"):  # connect.py: the one model, at its sizes up to the window its server has
        n = int((p.get("settings") or {}).get("context") or context_of(DEFAULT_MODEL))
        m = next(x for x in out if x["profile"] == DEFAULT_MODEL)
        sizes = [dict(c, profile=SERVER_64K if c["profile"] == DEFAULT_MODEL else c["profile"], ready=True, why=None, note="")
                 for c in m["contexts"]]
        sizes = [dict(c, context=context_of(c["profile"])) for c in sizes]
        sizes = [c for c in sizes if c["context"] < n]
        own = next(c for c in m["contexts"] if c["profile"] == DEFAULT_MODEL)
        sizes.append(dict(own, k=size_name(n), context=n, ready=True, why=None, note="What your server has"))
        named = CONNECTED is not None and CONNECTED()
        out = [dict(m, context=n, contexts=sizes, more=MORE if named else "Your server hasn't said its window yet. " + MORE)]
    label = MODEL_NAMES.get(p.get("model_id")) or p.get("name")  # the loaded model, by its one name
    legacy = LEGACY
    if (hs or {}).get("connected"):  # the page shows a chat on a size the window caps as on the server's own
        legacy = {**{k: v for k, v in LEGACY.items() if k != SERVER_64K},
                  **{c: DEFAULT_MODEL for c in own_sizes() if shown_size(c, n) == DEFAULT_MODEL and c != DEFAULT_MODEL}}
    return {"models": out, "legacy": legacy, "efforts": [{"id": k, "name": v["name"], "desc": v["desc"]} for k, v in EFFORTS.items()],
            "serving": {"profile": p.get("id"), "name": label, "model_id": p.get("model_id"), "state": (hs or {}).get("state"),
                        "context": (p.get("settings") or {}).get("context")} if hs else None}


def need(chat):
    """(profile id, model id) a chat's message needs serving: its chosen size. Any size of the same model serves it
    (each answer fits the window that is loaded); the size is what a Switch starts."""
    pid = profile_of(chat)
    return pid, (_builtin().get(LEGACY.get(pid, pid)) or {}).get("model_id")


CHARS_PER_TOKEN = 3.2
THINK_BUDGET = {"High": 6144, "Extra": 16384}  # experiment think_budget: thinking tokens before the answer's own room
LONGER_REPLY = 8192  # experiment longer_replies: a reply's cap at Low, and a tool step's first cap (the room left clamps it)
GROUNDING = ("Your knowledge stops at your training cutoff, and the date and time are given beside the user's newest message. "
             "For anything recent, or anything you can't check here, say so plainly instead of guessing; never invent "
             "numbers, quotes, names or web addresses; when you're unsure, say how sure you are. Without tools in this "
             "chat you can't look anything up: say so, and suggest turning on Container + web.")


def now_line():
    """Experiment grounding: today's date and time, after the user's newest message (never in the cached system prompt)."""
    return time.strftime("(Now: %A %d %B %Y, %H:%M %Z.)")


def think_fields(e):
    """Experiment think_budget: the thinking budget the engine ends thinking at (chat.zig), and Extra at DeepSeek's
    maximum effort (the template's default is 75 for both High and Extra)."""
    if "think_budget" not in _tools.EXPERIMENTS or not e["thinking"]:
        return {}
    out = {"thinking_budget": THINK_BUDGET.get(e["name"], 6144)}
    if e["name"] == "Extra":
        out["chat_template_kwargs"] = {"enable_thinking": True, "reasoning_effort": "max"}
    return out


def sample_seed():
    """A sampled request's seed (the eval harness fixes it, so a run can be repeated)."""
    return secrets.randbelow(1 << 31)


CONNECTED = None  # connect.py: what says the window of the server the app is connected to


MORE = "For more, raise CONTEXT in my.conf on both boxes, then restart."  # (the README says what past 63,488 needs)


def size_name(n):
    return "64K" if n == 63488 else f"{round(n / 1e6)}M" if n >= 1_000_000 else f"{round(n / 1024)}K"


def context_of(profile_id):
    if CONNECTED is not None and CONNECTED():  # connect.py: the server's window, or a smaller size the chat chose
        n = CONNECTED()
        if profile_id == DEFAULT_MODEL or profile_id not in own_sizes():
            return n
        own = int(((_builtin().get(LEGACY.get(profile_id, profile_id)) or {}).get("settings") or {}).get("context") or 0)
        return min(own, n) if own else n
    if profile_id == SERVER_64K:
        profile_id = DEFAULT_MODEL
    return int(((_builtin().get(profile_id) or {}).get("settings") or {}).get("context") or 16384)


def too_long(chat, chars, hs=None):
    """Why one new message of `chars` characters (its text and attachments) cannot fit the chat's window, or None. A
    long conversation is compacted, never refused; one message bigger than the window is: say so, and name a bigger
    size of the same model when one is offered."""
    m, pid = model_of(chat), profile_of(chat)
    ctx = context_of(pid)
    if chars <= ctx * CHARS_PER_TOKEN * 0.8:
        return None
    return longer_words(m, pid, "This message is", hs)


def photos_described(store, atts):
    """This turn's photos that reach a text-only model as a description made on the Mac (look.py, W6: a plug-in like
    chat_tools' toolsets, so 0 while it isn't there)."""
    try:
        import look
    except Exception:  # noqa: BLE001 - missing or broken: nothing was described
        return 0
    n = 0
    for a in atts:
        if a.get("kind") == "image":
            try:
                n += look.look_text(store, a["id"]) is not None
            except Exception:  # noqa: BLE001 - an unreadable description counts as none; the turn goes on
                pass
    return n


def close_toolsets(entry):
    """A turn's toolsets closed once (scratch folders, proxies, sessions): by the tool loop's end, or by turn()'s for a
    turn that ended before it (before() raising). Never raises."""
    for t in entry.pop("toolsets", None) or []:
        try:
            t.close()
        except Exception:  # noqa: BLE001
            pass


def attachment_chars(store, a, looker=None):
    """One attachment's characters as too_long counts them: the text read from it, and for a photo the description the
    Mac's vision model adds (look.py), or the longest one can be while it is still being made."""
    n = int(a.get("chars") or 0)
    if a.get("kind") == "image" and looker is not None and looker.available():
        d = look.look_text(store, a["id"])
        n += look.BLOCK_CHARS if d is None else len(d) + 300
    return n


def longer_words(m, pid, what="This is", hs=None):
    """"This is longer than the 64K window. Choose 128K in the composer, or split it." (the next size offered; the user
    never sees the engine's own wording)."""
    ctx, connected = context_of(pid), CONNECTED is not None and CONNECTED()
    sizes = m["contexts"] + ([{"k": "64K", "profile": SERVER_64K}] if connected and m["profile"] == DEFAULT_MODEL else [])
    sizes = sorted(sizes, key=lambda c: context_of(c["profile"]))  # (connected: the default's own is the server's)
    bigger = next((c for c in sizes if context_of(c["profile"]) > ctx and size_ready(c, hs)[0]), None)
    if bigger and connected:
        bigger = dict(bigger, k=size_name(context_of(bigger["profile"])))
    tip = f" Choose {bigger['k']} in the composer, or split it." if bigger else " Split it into parts."
    k = size_name(ctx) if connected else next((c["k"] for c in m["contexts"] if c["profile"] == pid), f"{round(ctx / 1024)}K")
    return f"{what} longer than {m['name']}'s {k} window.{tip}"


# ---------------------------------------------------------------- the window: reply budget and compaction
# (The engine's rule: prompt + max_tokens + EXTENT_PAD fit the window.)
EXTENT_PAD = _profiles.EXTENT_PAD   # 18
SAFETY_TOKENS = 96                  # beside it: the chat template's own tokens, rounding
MIN_REPLY = 512                     # less room than this: compact first
COMPACT_AT = 0.75                   # of the window, the history (with the new message and a reply) may fill
KEEP_MESSAGES = 6                   # the newest messages a compaction keeps word for word
CHARS_PER_TOKEN_GUESS = 3.0         # cautious, before a chat's own answers have measured it
OLD_ATTACHMENT_CHARS = 6000         # an older turn's attachment, shortened in the prompt (the full file stays)
SUMMARY_ATTACHMENT_CHARS, SUMMARY_MESSAGE_CHARS, SUMMARY_INPUT_SHARE, SUMMARY_TOKENS = 2000, 6000, 0.6, 1500
# a tool turn that outgrows COMPACT_AT of the window is compacted between its steps: its older steps become a summary,
# the newest KEEP_STEPS stay word for word; at most TURN_COMPACTIONS times an answer; an older step's output is cut to
# its ends when the summary cannot be made
KEEP_STEPS = 2
TURN_COMPACTIONS = 3
TRIM_HEAD, TRIM_TAIL = 800, 400
STEPS_SUMMARY_SYSTEM = ("Summarise the earlier steps of this answer below so that the work can continue without them: "
                        "what each command or tool call did and what it found (facts, numbers, file names, errors and "
                        "identifiers exactly as written), what is done and what is left. Plain sentences or short lists; "
                        "no commentary.")
SUMMARY_SYSTEM = ("Summarise the conversation below so that it can continue without it. Keep it factual and complete: "
                  "decisions made, facts and numbers given, names of files and attachments and what they held, open "
                  "questions and tasks, and what the user asked for last. Keep identifiers, names and numbers exactly as "
                  "written. Plain sentences or short lists; no commentary and no greeting.")
# experiment compact_v2 (Claude Code's compaction keeps, in sections, the requests, the facts, the files, the
# decisions, every one of the user's messages, what is open and what was being done; StrixFold UI's asked for it in a
# sentence and got at most 1,500 tokens): the sections, and up to SUMMARY_TOKENS_V2 tokens (a sixth of the window at
# most). What the chat needs to carry on comes first and the list that grows with the chat last, so a summary cut at
# its limit loses the least, and says it was cut
SUMMARY_SYSTEM_V2 = ("Summarise the conversation below so that it can continue without it: nothing else of it will be "
                     "kept. Write these sections, each under its number and name, and leave one out only if it has "
                     "nothing:\n"
                     "1. What the user wants: each of their requests and what they meant by it.\n"
                     "2. Last: what was being done at the end, and the next step if one was agreed.\n"
                     "3. Open: questions not yet answered and tasks not yet done.\n"
                     "4. Facts: every name, number, date, time, address, code, amount and reference given, exactly as "
                     "written, with what it is.\n"
                     "5. Files, attachments and pages: what each was and what in it mattered.\n"
                     "6. Decisions: what was settled, and what it replaced if it changed.\n"
                     "7. Problems and fixes.\n"
                     "8. The user's messages: each one in a line, in order (their own words, shortened; never tool output). In a "
                     "long conversation, group the older ones by subject and keep their last 20 one to a line.\n"
                     "When a summary so far comes first, merge the new part into it: keep everything in it unless the new "
                     "part changes it. When in doubt, keep a fact rather than drop it. Plain sentences or short lists; no "
                     "commentary and no greeting.")
SUMMARY_CUT = "\n(This summary reached its length limit and stops here.)"
SUMMARY_TOKENS_V2 = 4000


TUNNEL_GAP_S = 5  # a tunnel rebuilt while a chat is between requests is back in about a second


def connect(conn, stop=None):
    """conn's connection, tried again for TUNNEL_GAP_S while it is refused: nothing was sent yet, so nothing runs twice
    (a turn died at step 80 as its tunnel, unused while a command ran, was rebuilt)."""
    end = time.time() + TUNNEL_GAP_S
    while True:
        try:
            return conn.connect()
        except ConnectionRefusedError:
            if time.time() >= end or (stop or threading.Event()).wait(0.2):  # (Stop ends the wait)
                raise


def loop_watch(msgs, steps=False):
    """chat_loops.Watch for a reply to `msgs` (None: loop_stop is off): lines its tools returned may repeat, and lines
    the user wrote themselves. A nudge of the loop's own, or FINAL_ASK, is not the user's message."""
    if "loop_stop" not in _tools.EXPERIMENTS:
        return None
    asked = next((t for t in (text_of(m.get("content")) for m in reversed(msgs) if m["role"] == "user")
                  if t not in NUDGE.values() and FINAL_ASK.split("}", 1)[1] not in t), "")
    return chat_loops.Watch("\n".join(text_of(m.get("content")) for m in msgs if m["role"] == "tool")[-400_000:], asked, steps)


def summary_form(context):
    """(system, max_tokens) of a conversation summary: compact_v2's when it is on."""
    if "compact_v2" in _tools.EXPERIMENTS:
        return SUMMARY_SYSTEM_V2, max(SUMMARY_TOKENS, min(SUMMARY_TOKENS_V2, int(context or 0) // 6))
    return SUMMARY_SYSTEM, SUMMARY_TOKENS


# each engine's words for a prompt longer than its window: (pattern, the group holding the limit, the prompt's)
CTX_ERRORS = (
    (re.compile(r"maximum context length is (\d+) tokens, but the rendered prompt has (\d+) tokens", re.I), 1, 2),  # TensorFold
    (re.compile(r"prompt is (\d+) tokens, the server context is (\d+)", re.I), 2, 1),  # Kyojin (qbench's fixture)
    (re.compile(r"prompt has (\d+) tokens but the context is (\d+)", re.I), 2, 1),  # Gufo's scheduler (its source)
    (re.compile(r"prompt exceeds the (\d+)-token context", re.I), 1, None),  # Gufo's chat route (qbench): no count
)


# a turn with tools, ending: its last request goes without tools, and it says what it found, never what it will do next
# (after the limits its last reply was one more "Let me search for release notes."); the user's note
# says why it stopped
FINAL_ASK = ("({why} Answer the user now from what you found. Nothing else will run, so don't say what you will do next: say "
             "what you found, and plainly what you didn't find or couldn't finish.)")
FINAL_SPENT = "You have used the tools this message allows."
FINAL_REPEATS = "Your last calls repeated earlier ones and got the same results, so tools are off for the rest of this answer."
FINAL_STALLED = "Your last replies stopped short without making a call, so tools are off for the rest of this answer."
NOTE_SPENT = ("It reached this message's tool limit ({steps} replies, {calls} calls or {mins} minutes), so it answers with "
              "what it found.")
NOTE_REPEATS = "It kept making the same calls and getting the same results, so it was stopped and answers with what it found."
NOTE_STALLED = "It kept stopping short of its next step, so it answers with what it found."
FINAL_LOOP = "Your replies kept repeating themselves, so tools are off for the rest of this answer."
NOTE_LOOP = "It kept repeating itself, so it was stopped and answers with what it found."
NOTE_LOOPED = "It was repeating itself, so StrixFold UI stopped it here."
THOUGHT_LOOP = "It kept repeating itself while thinking, so StrixFold UI stopped it. Send your message again, or try Low effort."
# a reply that stopped short: cut off by the reply cap, it carries on; ending on a step it announced but never took, or
# with no words at all, it is sent back to do it or say it is done (NUDGES times a message at most; that follow-up shows
# only if it carries on); a call written out as words runs (chat_tools.calls_in_text), one too garbled to read is made
# again.
# NUDGES counts the times in a row (a step that runs a call starts it afresh: the user, a long task's third stall in
# 96 steps ended it); still short after them, it answers without tools rather than end on a promise (FINAL_STALLED).
# The answer without tools that still makes a call, or says nothing, is asked once more ("final"); a call cut off by
# the reply cap never runs ("cut_call") and the next reply gets twice the room, up to TOOL_REPLY_MAX tokens
NUDGES = 2
TOOL_REPLY_MAX = 8192
REFITS = 2  # a prompt the engine counted longer than the window, fitted again by its count, at most this often an answer
NUDGE = {"cut": "(Your reply was cut off by the length limit. Carry on from exactly where it stopped, without repeating "
                "anything.)",
         "cut_call": "(Your reply was cut off by the length limit in the middle of a tool call, so the call did not run. Make "
                     "it again; if it carries a lot of text, such as a whole file, split it over several smaller calls.)",
         "final": "(Tools are off now, so nothing more will run. Answer the user in words from what you found: what you did, what "
                  "you found, and plainly what is left.)",
         "stray": "(Your reply ended with a tool call written out as words, so nothing ran. If you meant to run it, make "
                  "the call now with the tools you were given. If that was only an example, reply with only the word DONE.)",
         "said": "(You said what you would do next but ended your reply without doing it. If there is more to do, do it now "
                 "with your tools. If you are finished, reply with only the word DONE.)",
         "empty": "(Your reply had no words for the user. If there is more to do, do it now with your tools; if not, give the user "
                  "your answer.)",
         "garbled": "(Your tool call came out garbled, so it did not run. Make the call again with your tools.)",
         "loop": "(Your reply was repeating itself, so it was stopped there and nothing ran. Don't say again what you will "
                 "do: make the next call now, or give the user your answer.)"}
OVER = "(Not run: only the first {n} calls of a reply are taken. Make this call again if you still need it.)"
# the user's message sent while it worked, taken in at a step's end inside that step's last result, as Claude Code does:
# a user turn there made the engine's template drop the answer's reasoning so far, and the model started over from the
# user's message
# (after the toolset's untrusted block, which a page cannot close; the tag, new each start, is what a page cannot copy,
# so only these are ever read back as the user's words: _compact_turn)
STEER_TAG = secrets.token_hex(4)
STEER_IN = f"\n\n[The user sent this message while you were working (from the user themselves, not from the tool; #{STEER_TAG}):]\n"
STEER_OUT = (f"\n[End of the user's message #{STEER_TAG}. Take it into account as you carry on with this answer: change course "
             "if they ask you to, otherwise keep going and answer them too.]")
STEER_RE = re.compile(re.escape(STEER_IN) + r"(.*?)" + re.escape(STEER_OUT), re.S)
REPEATED = ("(This call repeats one you already made in this answer, and its result is the same as before, above. Running "
            "it again changes nothing: use that result, try something different, or answer the user.)")


def ctx_refusal(text):
    """(window, prompt tokens or None when the engine did not say) when an engine refused a prompt longer than its
    window, else None. tests/fixtures/qwen-refusals.json holds the bodies the engines returned."""
    for rx, lim, pr in CTX_ERRORS:
        m = rx.search(text or "")
        if m:
            return int(m.group(lim)), (int(m.group(pr)) if pr else None)
    return None


def msg_chars(m, cpt=CHARS_PER_TOKEN_GUESS):
    """A message's characters as the engine reads them: its words, a tool step's calls (a whole file written goes in
    its arguments) and the thinking DeepSeek reads back within an answer."""
    return (content_chars(m.get("content"), cpt) + len(m.get("reasoning_content") or "")
            + sum(len(str((c.get("function") or {}).get("arguments") or "")) + 24 for c in m.get("tool_calls") or []))


def est_tokens(msgs, cpt=CHARS_PER_TOKEN_GUESS):
    """Tokens a request's messages take, estimated (the chat template adds a few per message; a picture VISION_TOKENS)."""
    return int(sum(msg_chars(m, cpt) for m in msgs) / cpt) + 6 * len(msgs) + 16


def chars_per_token(chat):
    """This chat's measured characters per token (its last answer's prompt: characters sent over the tokens the engine
    counted), a little cautious; the guess before any."""
    for m in reversed(chat.get("messages") or []):
        meta = m.get("meta") or {}
        pt, pc = (meta.get("usage") or {}).get("prompt_tokens"), meta.get("prompt_chars")
        if m["role"] == "assistant" and pt and pc:
            return max(1.2, min(4.5, pc / pt * 0.95))
    return CHARS_PER_TOKEN_GUESS


def latest_summary(chat):
    return next((m for m in reversed(chat.get("messages") or []) if m["role"] == "summary" and not m.get("hidden")), None)


def usage_of(chat, context):
    """How much of the window the chat fills now: {"used", "context"} in tokens (the last answer's own count, else an
    estimate)."""
    for m in reversed(chat.get("messages") or []):
        u = (m.get("meta") or {}).get("usage") or {}
        if m["role"] == "assistant" and not m.get("hidden") and u.get("prompt_tokens"):
            return {"used": int(u["prompt_tokens"]) + int(u.get("completion_tokens") or 0), "context": context, "measured": True}
    return None


def describe_file(u):
    return f"{u.get('name') or 'file'} ({u.get('chars') or 0:,} characters)"


class ChatAgent:
    """One answer for a chat: the history (trimmed to the serving profile's context), the effort, the stream."""

    def __init__(self, store, relay):
        self.s, self.relay = store, relay
        # a project's thread (chats.cousteau): its orchestrator's system prompt, from mac/cousteau.py
        self.orchestrator = None  # callable(project id) -> system prompt, or None for an unknown project
        self.app = None           # the dashboard (halo_control sets it): tools' web factory, the fake flag
        self.looker = None        # the Mac's photo look (look.Looker): describes photos for the text-only models

    def history(self, chat, context, cpt=CHARS_PER_TOKEN_GUESS, pending=0, sees=False, note="", tail=""):
        """System (with the project's instructions and the latest summary of compacted turns) and the visible turns after
        that summary, fitted to `context` tokens with room for `pending` more (the new message, when it is not stored
        yet) and a reply of at least MIN_REPLY: older turns' attachments are shortened first, then the oldest turns go
        (the last resort when a summary could not be made). With `sees` (the serving engine reads pictures) the newest
        VISION_PICTURES photos go as image_url parts of their turns. `tail` (the toolsets' turn_note: this message's PDFs,
        its photos' findings) goes after the user's newest message, never in the system prompt, whose start the engine's prompt
        cache keeps from turn to turn. (messages, turns dropped)."""
        system = SYSTEM
        if chat.get("cousteau") and self.orchestrator:
            system = self.orchestrator(chat["cousteau"]) or SYSTEM
        if sees:
            system = SYSTEM.split(" You read text only:")[0] + " " + SEES if system is SYSTEM else system + "\n\n" + SEES
        if "grounding" in _tools.EXPERIMENTS and not chat.get("cousteau"):  # not a project thread (its prompt names its own toggles)
            system += "\n\n" + GROUNDING
            tail = (tail + "\n\n" if tail else "") + now_line()
        if note:  # what this turn's tools are (chat_tools: each toolset's system_note)
            system += "\n\n" + note + "\n\n" + _tools.ACT_NOW
        if chat.get("project_id"):
            try:
                pr = self.s.project(chat["project_id"])
                if (pr.get("instructions") or "").strip():
                    system += f"\n\nThis chat belongs to the project \"{pr['name']}\". Its instructions:\n{pr['instructions'].strip()}"
            except KeyError:
                pass
        summary = latest_summary(chat)
        if summary:
            system += ("\n\nEarlier parts of this conversation were compacted to save room. Their summary (the full text "
                       f"stays in the user's chat):\n{summary['content'].strip()}")
        system = chat_mark(chat) + system  # last, so the checks above still see SYSTEM itself (the seeing models' swap)
        upto = summary["meta"].get("upto", 0) if summary else 0
        visible = [m for m in chat["messages"] if not m.get("hidden") and m["role"] in ("user", "assistant") and m["id"] > upto
                   and not (m["role"] == "assistant" and not (m["content"] or "").strip() and not m["meta"].get("steps")
                            and not m["meta"].get("parts"))]  # an answer that failed or stopped early
        last_user = max((m["id"] for m in visible if m["role"] == "user"), default=None)
        pics = {}  # upload id -> data: URI, the newest photos first
        for m in reversed(visible if sees else []):
            for a in reversed([a for a in (m["meta"].get("attachments") or []) if a.get("kind") == "image"] if m["role"] == "user" else []):
                if len(pics) < VISION_PICTURES and a.get("id") not in pics:
                    uri = picture(self.s, a.get("id"))
                    if uri:
                        pics[a["id"]] = uri
        def build(cap):
            turns = []
            for m in visible:
                text = self.turn_text(m, None if m["id"] == last_user else cap, pics, sees)
                if tail and m["id"] == last_user:
                    text += "\n\n" + tail
                mine = [a["id"] for a in (m["meta"].get("attachments") or []) if m["role"] == "user" and a.get("id") in pics]
                turns.append({"role": m["role"], "content": [{"type": "text", "text": text}] + [
                    {"type": "image_url", "image_url": {"url": pics[i]}} for i in dict.fromkeys(mine)] if mine else text})
            return turns
        room = context - EXTENT_PAD - SAFETY_TOKENS - MIN_REPLY - pending
        # experiment attach_full: an older turn's attachment stays whole while the history fits (the review: cut
        # to 6,000 characters every turn, a follow-up about page 5 had only page 1)
        # while they fit under the line a compaction starts at, too: past it, the compaction would summarise the file
        # away (the review)
        turns = build(None) if "attach_full" in _tools.EXPERIMENTS else None
        whole = min(room, COMPACT_AT * context - 2048)
        if turns is None or est_tokens([{"role": "system", "content": system}] + turns, cpt) > whole:
            turns = build(OLD_ATTACHMENT_CHARS)
        dropped = 0
        msgs = [{"role": "system", "content": system}] + turns
        while len(msgs) > 2 and est_tokens(msgs, cpt) > room:
            msgs.pop(1)
            dropped += 1
        while len(msgs) > 1 and msgs[1]["role"] != "user":
            msgs.pop(1)
            dropped += 1
        return msgs, dropped

    def turn_text(self, m, attachment_cap=None, pictures=(), sees=False):
        """A turn as the model reads it: its words, then each attachment's text (the newest turn's in full, an older
        turn's shortened to attachment_cap characters with a note: the file stays in the chat). A photo in `pictures`
        goes beside this text as a picture (history); with `sees`, an older photo that does not is said to be older."""
        text = m["content"] or ""
        if m["role"] == "assistant" and "loop_stop" in _tools.EXPERIMENTS:
            text = chat_loops.collapse(text)  # (a loop the user stopped would start the next one)
        tools = [p["tool"]["line"] for p in m["meta"].get("parts") or [] if p.get("tool")] if m["role"] == "assistant" else []
        if tools:  # an answer that used tools: what it did, one line each (never the outputs)
            text += ("\n\n" if text else "") + "(Tools used for this answer: " + "; ".join(tools[:12]) + \
                    (f"; and {len(tools) - 12} more" if len(tools) > 12 else "") + ")"
        if m["role"] == "assistant" and m["meta"].get("stopped"):  # Stop, or Send now: where it was when the user cut in
            text += ("\n\n" if text else "") + "(the user stopped this answer here.)"
        for a in (m["meta"].get("attachments") or []) if m["role"] == "user" else []:
            if a.get("kind") == "text":
                try:
                    body = self.s.upload_text(a["id"])[:MAX_FILE_CHARS]
                except (KeyError, ValueError, OSError):
                    body = "(this file was removed)"
                if attachment_cap and len(body) > attachment_cap:
                    body = body[:attachment_cap] + "\n(shortened here: the file was read in full when it was sent)"
                fence = "````" if "```" in body else "```"
                if a.get("doc") == "pdf":  # kept whole: the document tools (chat_docs) take it by its file_id
                    text += (f"\n\n[{a.get('name') or 'A PDF'}: a PDF the user attached, file_id {a['id']} "
                             f"({a.get('detail') or 'PDF'}). Its text follows; the document tools work on the PDF itself.]")
                text += f"\n\n{fence} {a.get('name') or 'file'}\n{body}\n{fence}"
            else:
                try:  # an image: the text found in it on the Mac (OCR), if any; audio or video: its transcript
                    seen = self.s.upload_text(a["id"])[:MAX_FILE_CHARS] if a.get("chars") else ""
                except (KeyError, ValueError, OSError):
                    seen = ""
                if attachment_cap and len(seen) > attachment_cap:
                    seen = seen[:attachment_cap] + "\n(shortened here)"
                what = {"image": "An image", "audio": "An audio file", "video": "A video"}.get(a.get("kind"), "A file")
                fence = "````" if "```" in seen else "```"
                if a.get("kind") == "image" and a.get("id") in pictures:
                    text += (f"\n\n[Photo attached: {a.get('name') or 'photo'} ({a.get('detail') or 'no details'}); it comes with "
                             "this message as a picture.]")
                    if seen.strip():
                        text += f"\n[The text recognised in it on the user's Mac:]\n{fence}\n{seen}\n{fence}"
                elif a.get("kind") == "image" and sees:  # an older photo past VISION_PICTURES (or one that could not be shrunk)
                    text += (f"\n\n[A photo was attached here ({a.get('name') or 'photo'}); it is not shown again as a picture, only "
                             + (f"the text recognised in it:]\n{fence}\n{seen}\n{fence}" if seen.strip() else "its name.]"))
                elif a.get("kind") == "image" and look.look_text(self.s, a["id"]) is not None:  # a text-only model: the
                    # description made on the Mac (look.py), then the text recognised in the photo
                    text += (f"\n\n[A photo was attached here ({a.get('detail') or a.get('name') or 'no details'}). This model reads "
                             "text only; a description of it made on the user's Mac follows.]\n" + look.block(self.s, a["id"], attachment_cap))
                    if seen.strip():
                        text += f"\n[The text recognised in it on the user's Mac:]\n{fence}\n{seen}\n{fence}"
                elif seen.strip():
                    fence = "````" if "```" in seen else "```"
                    how = "the text recognised in it" if a.get("kind") == "image" else "what was read from it"
                    text += (f"\n\n[{what} was attached here ({a.get('detail') or a.get('name') or 'no details'}). "
                             f"This model reads text only; {how} on the user's Mac:]\n{fence}\n{seen}\n{fence}")
                else:
                    text += f"\n\n[{what} was attached here; this model reads text only.]"
        return text

    def compact(self, chat_id, endpoint, served_model, context, keep=KEEP_MESSAGES, manual=False, cpt=CHARS_PER_TOKEN_GUESS, stop=None):
        """Summarise the turns before the newest `keep` messages (and the summary before them) with the serving model, as
        Claude compacts a long conversation; store it as a "summary" message (nothing is deleted: the turns stay in the
        chat and on the page, behind a "Conversation compacted" divider). The summary message or None (nothing to
        compact). Raises RuntimeError when the model does not answer or Stop ends it (`stop`; the chat's relay entry,
        which the caller holds, gets each summary request's connection so Stop closes it)."""
        chat = self.s.chat(chat_id)
        prev = latest_summary(chat)
        upto0 = prev["meta"].get("upto", 0) if prev else 0
        visible = [m for m in chat["messages"] if not m.get("hidden") and m["role"] in ("user", "assistant") and m["id"] > upto0
                   and not (m["role"] == "assistant" and not (m["content"] or "").strip() and not m["meta"].get("steps"))]
        cover = visible[:-keep] if keep else visible
        while cover and cover[-1]["role"] == "user":  # a turn is never split: its answer goes with it
            cover = cover[:-1]
        if not cover:
            return None
        parts = [f"{'the user' if m['role'] == 'user' else 'Assistant'}: {self.turn_text(m, SUMMARY_ATTACHMENT_CHARS)[:SUMMARY_MESSAGE_CHARS]}"
                 for m in cover]
        budget = int(context * SUMMARY_INPUT_SHARE * cpt)
        # a history longer than one request can hold is summarised in order, a part at a time, each part's summary
        # carried into the next (nothing old is skipped)
        text, chunk = (prev["content"].strip() if prev else ""), []
        system, tokens = summary_form(context)
        with self.relay.mu:
            if chat_id in self.relay.active:
                self.relay.active[chat_id]["compacting"] = True
        try:
            for part in parts:
                if chunk and len(text) + sum(len(x) for x in chunk) + len(part) > budget:
                    text = self._summarise(endpoint, served_model, text, chunk, budget, chat_id, stop, system, tokens)
                    chunk = []
                chunk.append(part[:budget // 2])
            text = self._summarise(endpoint, served_model, text, chunk, budget, chat_id, stop, system, tokens)
        finally:
            with self.relay.mu:
                if chat_id in self.relay.active:
                    self.relay.active[chat_id]["compacting"] = False
        mid = self.s.add_message(chat_id, "summary", text, {"upto": cover[-1]["id"], "covered": len(cover) + (prev["meta"].get("covered", 0) if prev else 0),
                                                            "manual": manual, "chars": sum(len(m["content"] or "") for m in cover)})
        return self.s.message(mid)

    def _summarise(self, endpoint, served_model, so_far, parts, budget, chat_id=None, stop=None, system=SUMMARY_SYSTEM,
                   tokens=SUMMARY_TOKENS):
        if stop is not None and stop.is_set():
            raise RuntimeError("stopped")
        transcript = (f"Summary so far:\n{so_far}\n\n" if so_far else "") + "\n\n".join(parts)
        body = {"model": served_model, "stream": False, "temperature": 0, "max_tokens": tokens,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": transcript[-budget:]}]}
        u = urlparse(endpoint)
        # a long history's part is a long read too (at most SUMMARY_INPUT_SHARE of the window); 2 characters a token
        # is the generous side of what chats measure
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=read_timeout(len(transcript[-budget:]) / 2.0, 600))
        with self.relay.mu:
            if chat_id in self.relay.active:
                self.relay.active[chat_id]["conn"] = conn  # Stop closes it
        lease = _lease(endpoint, chat_id)  # a summary is a long request on the chat's pair too
        try:
            connect(conn, stop)
            conn.request("POST", u.path.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                         {"Content-Type": "application/json", "Authorization": "Bearer none"})
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as e:
            raise RuntimeError("stopped" if stop is not None and stop.is_set() else f"the model endpoint did not answer: {e}")
        finally:
            conn.close()
            _unlease(lease)
        try:
            choice = json.loads(raw)["choices"][0]
            text, cut = (choice["message"].get("content") or "").strip(), choice.get("finish_reason") == "length"
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            text, cut = "", False
        if resp.status != 200 or not text:
            raise RuntimeError(f"the summary could not be made (HTTP {resp.status})")
        if cut and "compact_v2" in _tools.EXPERIMENTS:  # (a summary so far that says so keeps saying it once)
            _log(f"chat: a summary reached its {tokens} tokens and was cut")
            text = text.replace(SUMMARY_CUT, "") + SUMMARY_CUT
        return text

    def turn(self, chat_id, endpoint, served_model, served_profile, content=None, attachments=None, mode="send", effort=None,
             served_context=None, claim=None, tools=True, user_meta=None):
        """Yield page events: user, start, reasoning, delta, done, error. The chat is held (relay.active) from the first
        step to the last: a switch, the outbox and a second send all see it as busy, while it is compacted too, and Stop
        ends a compaction as it ends an answer. `claim` is the entry a caller took before its own checks (a send, the
        outbox); without one the turn claims it here."""
        entry = claim or self.relay.claim(chat_id)
        if entry is not None:
            entry["chat_id"] = chat_id  # the conductor counts its requests (conductor.track)
        if entry is None:
            raise ValueError("this chat is answering already; stop it first")
        try:
            yield from self._turn(entry, chat_id, endpoint, served_model, served_profile, content, attachments, mode, effort,
                                  served_context, tools, user_meta)
        finally:
            close_toolsets(entry)  # a turn that ended before or outside its tool loop (before() raising)
            self.relay.release(chat_id, entry)
            import conductor  # the main boxes it kept between its steps are lent to agents again
            if conductor.C is not None:
                conductor.C.turn_over(chat_id)

    def _turn(self, entry, chat_id, endpoint, served_model, served_profile, content, attachments, mode, effort, served_context,
              tools=True, user_meta=None):
        chat = self.s.chat(chat_id)
        msgs = [m for m in chat["messages"] if not m.get("hidden")]
        version = None
        if mode in ("edit", "regenerate"):
            last_user = next((m for m in reversed(msgs) if m["role"] == "user"), None)
            if last_user is None:
                raise ValueError("there is no message to resend")
            if mode == "edit" and not (content or "").strip() and not last_user["meta"].get("attachments"):
                raise ValueError("the message is empty")
            if mode == "regenerate":
                content = last_user["content"]
            if attachments is None:
                attachments = [{"id": a["id"]} for a in last_user["meta"].get("attachments") or []]
            version = last_user["id"]  # the turn becomes a version only once this one passes every check below
            mode = "send"
        if mode != "send":
            raise ValueError("mode: send, edit or regenerate")
        text = (content or "").strip()
        atts = []
        for a in attachments or []:
            rid = str((a or {}).get("id") or "")
            if not rid.startswith("u"):
                raise ValueError("unknown attachment")
            self.s.check_attachment(chat_id, rid)  # a Private chat's file never leaves it, and it takes no other
            u = self.s.upload(rid)
            atts.append(dict({"id": rid, "kind": u["kind"], "name": u["name"], "chars": u.get("chars"), "detail": u.get("detail")},
                             **({"doc": u["orig"]} if u.get("orig") else {})))  # a document kept whole (chat_docs)
        if not text and not atts:
            raise ValueError("the message is empty")
        if len(text) > 400_000:
            raise ValueError("the message is longer than 400,000 characters")
        why = too_long(chat, len(text) + sum(attachment_chars(self.s, a, self.looker) for a in atts))
        if why:
            raise ValueError(why)
        eff = effort if effort in EFFORTS else (chat.get("effort") if chat.get("effort") in EFFORTS else DEFAULT_EFFORT)
        # every check has passed: now the edited or regenerated turn is kept as a version (a refused one hides nothing;
        # a refused regenerate hid the whole turn and left the chat looking empty)
        version = self.s.new_version(chat_id, version) if version is not None else None
        uid = self.s.add_message(chat_id, "user", text, dict(user_meta or {}, attachments=atts, effort=eff), version,
                                 from_outbox=entry.pop("from_outbox", None))  # a waiting message leaves the outbox here
        yield {"type": "user", "id": uid, "content": text, "meta": dict(user_meta or {}, attachments=atts, effort=eff)}
        if chat["title"] == "New chat" and not [m for m in msgs if m["role"] == "user"]:
            t = re.sub(r"\s+", " ", text).strip() or (atts[0]["name"] if atts else "New chat")
            self.s.rename_chat(chat_id, (t[:59] + "…") if len(t) > 60 else t)
        prof = _builtin().get(served_profile) or {}
        settings = prof.get("settings") or {}
        e = EFFORTS[eff]
        # the window the loaded run has (halod's own report: a run started before a profile changed keeps its size)
        ctx = chat_window(chat, int(served_context or settings.get("context") or 16384))
        want = int(e["max_tokens"] or settings.get("max_tokens") or 2048)
        if "longer_replies" in _tools.EXPERIMENTS:  # (the review: plain answers stopped dead at 2,048)
            want = max(want, LONGER_REPLY)
        if "think_budget" in _tools.EXPERIMENTS and e["thinking"]:  # its thinking has its own room before the answer
            want += THINK_BUDGET.get(e["name"], 0)
        # the window: a long conversation is compacted (a summary of its older turns, made by the serving model) before
        # it would fill COMPACT_AT of the window; each reply gets at most the room that is left
        stop = entry["stop"]
        cpt = chars_per_token(self.s.chat(chat_id))
        # photos as pictures only when the engine SERVING this turn reads them (its own /health), and only when the
        # conversation has a photo (no probe otherwise)
        served_id = (_builtin().get(served_profile) or {}).get("model_id")
        sees = served_id in VISION_MODELS and any(a.get("kind") == "image" for m in self.s.chat(chat_id)["messages"]
                                                  if m["role"] == "user" and not m.get("hidden")
                                                  for a in (m["meta"].get("attachments") or [])) and sees_images(endpoint, served_id)
        # tools (chat_tools): the chat's level (none for a "send without tools" message, and on a project thread) and
        # every plug-in that applies; their notes join the system prompt
        row = self.s.chat(chat_id)
        priv = bool(row.get("private"))  # a Private chat: the sealed container levels only
        lv0 = _tools.level_of(row) if tools and not row.get("cousteau") else "none"
        tctx = _tools.TurnCtx(chat_id, row, self.s, served_id,
                              lv0 if not priv or lv0 in studio.PRIVATE_LEVELS else "none",
                              _tools.opts_of(row), sees=sees, private=priv, stop=stop, app=self.app,
                              fake=bool(getattr(self.app, "fake", False)))
        tctx.endpoint, tctx.served_model = endpoint, served_model  # chat_docs asks a seeing engine about a page itself
        toolsets = _tools.toolsets_for(tctx) if not row.get("cousteau") else []
        entry["toolsets"] = toolsets  # closed once by close_toolsets, whichever way the turn ends
        # the toolsets' before() runs first, so what it waits for (a photo's description made on the Mac: chat_look) is in
        # the request and the answer's meta; its notes are kept for meta.parts (the page draws them on its next load)
        early = []
        for t in toolsets:
            for ev in t.before(tctx):
                if ev.get("type") == "note":
                    early.append({"note": str(ev.get("text") or "")})
                yield ev
        note = "\n\n".join(n for n in (t.system_note(tctx) for t in toolsets) if n)
        tail = "\n\n".join(n for n in (t.turn_note(tctx) for t in toolsets if hasattr(t, "turn_note")) if n)
        msgs, dropped = self.history(self.s.chat(chat_id), ctx, cpt, sees=sees, note=note, tail=tail)
        compacted = None
        if dropped or est_tokens(msgs, cpt) + min(want, 2048) > COMPACT_AT * ctx:
            yield {"type": "compacting"}
            try:
                compacted = self.compact(chat_id, endpoint, served_model, ctx, cpt=cpt, stop=stop)
            except RuntimeError:
                compacted = None  # the oldest turns are left out instead (history's last resort)
            if compacted:
                yield {"type": "compacted", "summary": {"id": compacted["id"], "covered": compacted["meta"]["covered"]}}
            else:  # no summary made (the model failed, or nothing older): the page's divider says so instead of waiting
                yield {"type": "compacted", "failed": True}
            msgs, dropped = self.history(self.s.chat(chat_id), ctx, cpt, sees=sees, note=note, tail=tail)
        # the model that answered: the one served (a project lead on "Any" read Qwen's answer as "DeepSeek V4.1")
        label = MODEL_NAMES.get(served_id) or model_of(chat)["name"]
        meta = {"model": label, "profile": served_profile, "effort": eff, "started": time.time(), "dropped": dropped,
                "context": ctx}
        if sees:
            meta["pictures"] = pictures_in(msgs)  # the photos it was shown as pictures (the page says "saw 2 photos")
        elif any(a.get("kind") == "image" for a in atts):  # this turn's photos reached it as a description or their text only
            described = photos_described(self.s, atts)  # (said too: "read a description of the photo")
            meta["photo_text"] = sum(1 for a in atts if a.get("kind") == "image") - described
            if described:
                meta["described"] = described
        specs = [x for t in toolsets for x in t.specs()]
        if toolsets:
            meta.update(level=tctx.level, parts=early)
        mid = self.s.add_message(chat_id, "assistant", "", meta, version)
        yield {"type": "start", "id": mid, "model": label, **({"level": tctx.level} if toolsets else {})}
        if toolsets:
            yield from self._tool_loop(entry, chat_id, chat, endpoint, served_model, msgs, meta, mid, ctx, want, e, cpt, tctx,
                                       toolsets, specs)
            return
        u = urlparse(endpoint)
        conn = None
        text_out, reasoning, usage, finish, err, first, done = [], [], None, None, None, None, False
        last_save = time.time()
        lease = _lease(endpoint, chat_id)  # the answer holds a slot on its pair while it streams, as a tool step does
        try:
            try:
                forced = None
                for attempt in (1, 2):  # the engine refuses a request that does not fit: once more, as it counted
                    if stop.is_set():  # stopped while the conversation was compacted
                        break
                    room = ctx - EXTENT_PAD - SAFETY_TOKENS - est_tokens(msgs, cpt)
                    if (forced or room) < min(MIN_REPLY, want):  # no room for a real answer even after compacting
                        err = longer_words(model_of(chat), profile_of(chat))
                        break
                    body = dict({"model": served_model, "stream": True, "stream_options": {"include_usage": True}, "temperature": 0,
                                 "max_tokens": forced or min(want, room), "chat_template_kwargs": {"enable_thinking": e["thinking"]},
                                 "messages": msgs}, **think_fields(e))
                    meta.update(max_tokens=body["max_tokens"], prompt_chars=sum(content_chars(m["content"]) for m in msgs))
                    note_start(chat_id, body, 1, cpt)
                    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=read_timeout(est_tokens(msgs, cpt)))
                    with self.relay.mu:
                        entry["conn"] = conn  # Stop closes it
                    connect(conn, stop)
                    conn.request("POST", u.path.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Accept": "text/event-stream", "Authorization": "Bearer none"})
                    resp = conn.getresponse()
                    if resp.status == 200:
                        break
                    raw = resp.read().decode("utf-8", "replace")[:600]
                    try:
                        er = json.loads(raw).get("error")
                        err = er.get("message") if isinstance(er, dict) else er
                    except ValueError:
                        err = raw
                    m = ctx_refusal(str(err or raw))
                    if attempt == 1 and m and not stop.is_set():
                        limit, prompt = m
                        conn.close()
                        # its count, not ours; an engine that only says "over" (Gufo's chat route): over by a fifth, cautiously
                        prompt = prompt or int(limit * 1.2)
                        cpt = max(0.5, sum(content_chars(x["content"]) for x in msgs) / max(1, prompt) * 0.95)
                        if limit - prompt - 16 >= MIN_REPLY:  # the same prompt, the reply cut to the room it has
                            forced = min(want, limit - prompt - 16)
                        else:  # no room left: compact (once) and fit again by its count
                            if not compacted:
                                try:
                                    compacted = self.compact(chat_id, endpoint, served_model, ctx, cpt=cpt, stop=stop)
                                except RuntimeError:
                                    compacted = None
                            msgs, dropped = self.history(self.s.chat(chat_id), ctx, cpt, sees=sees, note=note, tail=tail)
                        meta.update(dropped=dropped, retried=True)
                        err = None
                        continue
                    if m:  # a window compaction could not make room in: plain words and the bigger sizes, never the engine's
                        err = longer_words(model_of(chat), profile_of(chat))
                    else:
                        err = f"the model answered HTTP {resp.status}: {err or raw}"
                    break
                if not err:
                    said, thought = loop_watch(msgs), loop_watch(msgs)
                    while not stop.is_set():
                        line = resp.readline()
                        if not line:
                            break
                        line = line.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            done = True
                            break
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        if obj.get("error"):
                            er = obj["error"]
                            err = er.get("message") if isinstance(er, dict) else str(er)
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
                                keep = thought.add(d["reasoning_content"]) if thought else None
                                if keep is not None:
                                    finish, reasoning = "loop", ["".join(reasoning)[:keep]]
                                    if not "".join(text_out).strip():
                                        err = THOUGHT_LOOP
                            if d.get("content"):
                                text_out.append(d["content"])
                                first = first or time.time()
                                yield {"type": "delta", "text": d["content"]}
                                keep = said.add(d["content"]) if said else None
                                if keep is not None:  # it goes round in circles: kept to its first round
                                    finish, whole = "loop", "".join(text_out)
                                    text_out = [whole[:keep] + ("" if whole[:keep].endswith("\n") else "\n\n") + f"({NOTE_LOOPED})"]
                                    yield {"type": "retract", "chars": len(whole[keep:].encode("utf-16-le")) // 2}
                                    yield {"type": "delta", "text": text_out[0][keep:]}
                        if finish == "loop":
                            meta["loops"] = 1
                            _log("chat: a reply without tools went round in circles; stopped there")
                            break
                        if time.time() - last_save > 0.6:  # other pages (and this one after a lock) follow it as it grows
                            self.s.set_message(mid, "".join(text_out), dict(meta, partial=True, reasoning="".join(reasoning)))
                            last_save = time.time()
            except (OSError, http.client.HTTPException) as ex:
                if not stop.is_set():
                    err = f"the model endpoint did not answer: {ex}"
            if not err and not stop.is_set() and not done and not finish:
                # the stream ended without its end (the model was stopped under it, e.g. by a switch)
                err = "The answer was cut off: the model stopped before it finished. Retry when it is back."
                meta["interrupted"] = True
        finally:  # the turn's end, whatever happened
            _unlease(lease)
            if stop.is_set() and entry.get("why"):  # stopped by Lock (studio_http.stop_private), said in the thread
                err = f"Stopped: {entry['why']}."
            try:
                if conn is not None:
                    conn.close()
            except OSError:
                pass
            t = time.time()
            meta.update(finish=finish or ("stopped" if stop.is_set() else None), usage=usage, error=err, stopped=stop.is_set(),
                        reasoning="".join(reasoning), ttft_s=round(first - meta["started"], 3) if first else None,
                        total_s=round(t - meta["started"], 3))
            if usage and first and (usage.get("completion_tokens") or 0) > 1 and t > first:
                meta["tok_s"] = round((usage["completion_tokens"] - 1) / (t - first), 1)
            self.s.set_message(mid, "".join(text_out), meta)
            self.relay.release(chat_id, entry)  # kept, and free before the page hears done (it may send at once)
        log_end(self.s, chat_id, err, stop.is_set())
        if err:
            yield {"type": "error", "error": err, "id": mid}
        yield {"type": "done", "id": mid, "finish": meta["finish"], "usage": usage, "stopped": stop.is_set()}

    # ---------------------------------------------------------------- the tool loop (chat_tools)
    def _held_step(self, entry, endpoint, body, cpt, on_reasoning, names=()):
        """_stream_step whose words wait (the caller shows them, or not); its thinking and other events go on as usual."""
        gen = self._stream_step(entry, endpoint, body, cpt, lambda s: None, on_reasoning, names)
        while True:
            try:
                ev = next(gen)
            except StopIteration as stopped:
                return dict(stopped.value, retract=0)  # nothing of it was shown
            if ev.get("type") not in ("delta", "retract"):
                yield ev

    def _stream_step(self, entry, endpoint, body, cpt, on_text, on_reasoning, names=()):
        """One streamed request with tools (`names`: the tools offered this answer). Yields the page's delta, retract and
        reasoning events; returns {text, reasoning, calls, usage, finish, done, err, ttft, written, garbled, shape,
        retract}. Stop closes the connection (relay.stop)."""
        out = {"text": "", "reasoning": "", "calls": [], "usage": None, "finish": None, "done": False, "err": None, "ttft": None}
        import conductor  # this request holds a slot on its pair while it streams
        lease = conductor.C.track(endpoint, "chat", entry.get("chat_id") or "", label="the user's chat") if conductor.C is not None else None
        try:
            return (yield from self._stream_step_in(entry, endpoint, body, cpt, on_text, on_reasoning, out, names))
        finally:
            if conductor.C is not None:
                conductor.C.release(lease)

    def _stream_step_in(self, entry, endpoint, body, cpt, on_text, on_reasoning, out, names=()):
        u = urlparse(endpoint)
        stop = entry["stop"]
        calls, t0, shown = {}, time.time(), 0  # shown: how much of out["text"] the page has (never DeepSeek's markup)
        said, thought = (loop_watch(body["messages"], bool(body.get("tools"))) for _ in range(2))
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=read_timeout(est_tokens(body["messages"], cpt)))
        with self.relay.mu:
            entry["conn"] = conn
        try:
            connect(conn, stop)
            conn.request("POST", u.path.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                         {"Content-Type": "application/json", "Accept": "text/event-stream", "Authorization": "Bearer none"})
            resp = conn.getresponse()
            if resp.status != 200:
                raw = resp.read().decode("utf-8", "replace")[:600]
                try:
                    er = json.loads(raw).get("error")
                    out["err"] = er.get("message") if isinstance(er, dict) else er
                except ValueError:
                    out["err"] = raw
                out["status"] = resp.status
                return out
            while not stop.is_set():
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    out["done"] = True
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("error"):
                    er = obj["error"]
                    out["err"] = er.get("message") if isinstance(er, dict) else str(er)
                    break
                if obj.get("usage"):
                    out["usage"] = obj["usage"]
                if isinstance(obj.get("tensorfold") or obj.get("timings"), dict):  # the engine's own clock (speeds)
                    out["engine"] = obj.get("tensorfold") or obj.get("timings")
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    if ch.get("finish_reason"):
                        out["finish"] = ch["finish_reason"]
                    if d.get("reasoning_content"):
                        out["reasoning"] += d["reasoning_content"]
                        out["ttft"] = out["ttft"] or round(time.time() - t0, 3)
                        on_reasoning(d["reasoning_content"])
                        yield {"type": "reasoning", "text": d["reasoning_content"]}
                        keep = thought.add(d["reasoning_content"]) if thought else None
                        if keep is not None:
                            out.update(loop="thinking", reasoning=out["reasoning"][:keep])
                    if d.get("content"):
                        out["text"] += d["content"]
                        out["ttft"] = out["ttft"] or round(time.time() - t0, 3)
                        # from call markup on it waits for the end (a call written out?)
                        upto = shown + _tools.held_from(out["text"][shown:])
                        if upto > shown:
                            new, shown = out["text"][shown:upto], upto
                            on_text(new)
                            yield {"type": "delta", "text": new}
                        keep = said.add(d["content"]) if said else None
                        if keep is not None:
                            out.update(loop="answer", keep=keep)
                    for tc in d.get("tool_calls") or []:
                        k = tc.get("index", len(calls))
                        cl = calls.setdefault(k, {"id": None, "name": "", "args": ""})
                        cl["id"] = tc.get("id") or cl["id"]
                        fn = tc.get("function") or {}
                        cl["name"] += fn.get("name") or ""
                        cl["args"] += fn.get("arguments") or ""
                if out.get("loop"):  # it goes round in circles: the request ends here (the engine stops as it is closed)
                    break
        except (OSError, http.client.HTTPException) as ex:
            if not stop.is_set():
                out["err"] = f"the model endpoint did not answer: {ex}"
        finally:
            try:
                conn.close()
            except OSError:
                pass
        if out.get("loop"):  # its repeats are taken back, and a call it had only begun never runs
            calls, out["finish"], out["err"] = {}, "loop", None
            if out.get("keep") is not None and shown > out["keep"]:
                out["retract"] = shown - out["keep"]
                yield {"type": "retract", "chars": len(out["text"][out["keep"]:shown].encode("utf-16-le")) // 2}
                shown = out["keep"]
            out["text"] = out["text"][:out.get("keep")]
            shown = min(shown, len(out["text"]))
        out["calls"] = [{"id": c["id"] or f"call_{k}_{secrets.token_hex(3)}", "name": c["name"], "args": c["args"]}
                        for k, c in sorted(calls.items()) if c["name"]]
        out["shape"] = _tools.call_shape(out["text"])  # for the app's log and a cut-off call (never the words)
        words, written, garbled = _tools.calls_in_text(out["text"], names)
        if written or garbled:  # calls the engine handed back as words: they run (or it is asked to make them again)
            if shown > len(words):  # shown as it came (a JSON call, the space before markup): the page takes it back
                out["retract"] = out.get("retract", 0) + shown - len(words)  # characters, for the stored words
                # ... and the page's count: a browser string counts UTF-16 units (an emoji is 2; gate review)
                yield {"type": "retract", "chars": len(out["text"][len(words):shown].encode("utf-16-le")) // 2}
            elif len(words) > shown:  # held back from an earlier "<" that was no call: shown now (gate review)
                new, shown = words[shown:], len(words)
                on_text(new)
                yield {"type": "delta", "text": new}
            out["text"] = words
            if not out["calls"]:
                out["calls"] = [dict(c, id=f"call_{k}_{secrets.token_hex(3)}") for k, c in enumerate(written)]
                out["written"] = bool(written)
            out["garbled"] = garbled and not out["calls"]
        elif len(out["text"]) > shown:  # held words that were no call after all
            new, shown = out["text"][shown:], len(out["text"])
            on_text(new)
            yield {"type": "delta", "text": new}
        out["elapsed"] = round(time.time() - t0, 3)
        return out

    def _compact_turn(self, chat_id, endpoint, served_model, ctx, cpt, msgs, turn0, stop):
        """A tool turn past COMPACT_AT of the window, between two of its steps: its older steps become one summary (their
        outputs cut to their ends when the model can't make it), the newest KEEP_STEPS stay word for word, and the user's words
        sent meanwhile stay; still too big, the conversation before this answer is summarised into the system prompt (its
        oldest turns left out when that fails). msgs[turn0:] is this answer. (messages, turn0, words for the step)."""
        head, tail, words = list(msgs[:turn0]), list(msgs[turn0:]), []
        budget = int(ctx * SUMMARY_INPUT_SHARE * cpt)
        starts = [i for i, m in enumerate(tail) if m["role"] == "assistant" and m.get("tool_calls")]
        if len(starts) > KEEP_STEPS:
            cut = starts[-KEEP_STEPS]
            old, keep = tail[:cut], tail[cut:]
            owner = []  # the user's words sent meanwhile, in order: the user's messages, and those a step's result carries (STEER_IN)
            for m in old:
                said = str(m.get("content") or "")
                if m["role"] == "user" and not said.startswith(("(The earlier steps", "(The conversation is nearly full")) \
                        and said not in NUDGE.values() and FINAL_ASK.split("}", 1)[1] not in said:  # the loop's own words aren't the user's
                    owner.append(m)
                elif m["role"] == "tool" and isinstance(m.get("content"), str):
                    owner += [{"role": "user", "content": said} for said in STEER_RE.findall(m["content"])]
            n = sum(1 for m in old if m["role"] == "assistant" and m.get("tool_calls"))
            lines = []
            for m in old:
                if m["role"] == "assistant":
                    calls = "; ".join(f"{c['function']['name']}({c['function'].get('arguments') or ''})"[:600]
                                      for c in m.get("tool_calls") or [])
                    lines.append(f"Assistant: {text_of(m.get('content'))[:1500]}" + (f"\nCalled: {calls}" if calls else ""))
                elif m["role"] == "tool":
                    out = STEER_RE.sub("", m["content"]) if isinstance(m.get("content"), str) else text_of(m.get("content"))
                    lines.append(f"Result: {_tools.ends(out, SUMMARY_MESSAGE_CHARS)}")  # its end too ("1 failed")
                    lines += [f"the user (while it worked): {said[:SUMMARY_MESSAGE_CHARS]}"
                              for said in (STEER_RE.findall(m["content"]) if isinstance(m.get("content"), str) else [])]
                elif m in owner:
                    lines.append(f"the user (while it worked): {text_of(m.get('content'))[:SUMMARY_MESSAGE_CHARS]}")
                else:  # an earlier summary of steps
                    lines.append(text_of(m.get("content"))[:SUMMARY_MESSAGE_CHARS])
            try:
                text = self._summarise(endpoint, served_model, "", lines, budget, chat_id, stop, system=STEPS_SUMMARY_SYSTEM)
                tail = [{"role": "user", "content": "(The earlier steps of this answer, summarised to make room. Carry on "
                                                    "from them:)\n" + text}] + owner + keep
                words.append(f"summarised {n} earlier step{'' if n == 1 else 's'}")
            except (RuntimeError, OSError, ValueError, KeyError, IndexError, TypeError):
                if stop is not None and stop.is_set():
                    raise
                tail = [dict(m, content=trim_middle(STEER_RE.sub("", m["content"])) + "".join(
                            STEER_IN + said + STEER_OUT for said in STEER_RE.findall(m["content"])))  # the user's words stay whole
                        if m["role"] == "tool" and isinstance(m.get("content"), str)
                        else dict(m, content=trim_middle(m["content"])) if m["role"] == "tool" else m for m in old] + keep
                words.append(f"cut {n} earlier step{'' if n == 1 else 's'}' outputs to their ends")
        if est_tokens(head + tail, cpt) > COMPACT_AT * ctx and len(head) > 2:
            past = head[1:-1]
            lines = [f"{'the user' if m['role'] == 'user' else 'Assistant'}: {text_of(m.get('content'))[:SUMMARY_MESSAGE_CHARS]}"
                     for m in past]
            try:
                text = self._summarise(endpoint, served_model, "", lines, budget, chat_id, stop, *summary_form(ctx))
                sys0 = dict(head[0], content=text_of(head[0].get("content")) + "\n\nEarlier parts of this conversation were "
                            "summarised to make room. Their summary:\n" + text)
                head = [sys0, head[-1]]
                words.append("summarised the conversation before it")
            except (RuntimeError, OSError, ValueError, KeyError, IndexError, TypeError):
                if stop is not None and stop.is_set():
                    raise
                left = 0
                while len(head) > 2 and est_tokens(head + tail, cpt) > COMPACT_AT * ctx:
                    head.pop(1)
                    left += 1
                words.append(f"left out the {left} oldest message{'' if left == 1 else 's'}")
        return head + tail, len(head), "; ".join(words)

    def _tool_loop(self, entry, chat_id, chat, endpoint, served_model, msgs, meta, mid, ctx, want, e, cpt, tctx, toolsets, specs):
        """A turn with tools: the model answers or calls tools (at most STEP_CALLS a reply); each call is checked
        against the chat's level as it is now, run by its toolset, shown and audited, and its result goes back as a tool
        message; MAX_STEPS replies, MAX_CALLS calls and TURN_WALL_S at most, then one answer without tools. Text and
        steps are kept in order in meta.parts; the message's content is its text. A message the user sends meanwhile
        (_take_steers) goes in at the next step: this answer ends there, the user's message follows it, and a new
        answer goes on with everything this one did, with a fresh allowance of steps, calls and time."""
        stop = entry["stop"]
        parts = meta["parts"]
        by_name = {x["function"]["name"]: t for t in toolsets for x in t.specs()}
        events = queue.Queue()
        cur = {"id": None, "part": None}
        state = {"last": 0.0, "reasoning": ""}
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        err, finish, done_any, calls_n, t_start, first = None, None, False, 0, time.time(), None
        turn0, compactions = len(msgs), 0  # msgs[turn0:] is this answer's steps (_compact_turn)
        seen, repeats, sample = {}, 0, True  # each call's result in this answer (call_key); the engine samples until it refuses
        # NUDGE: times in a row it was sent back (and in all); hold: its next words wait until it carries on; stalled: it
        # stayed short after NUDGES, so it answers without tools; asked: that answer's FINAL_ASK is in; again: it was asked
        # once more after it; room_final: it answers without tools for want of room (never asked again); reply_cap: the
        # reply's budget (raised after a cut); refits: prompts fitted again by the engine's own count
        nudges, nudged, hold, stalled, asked, again, room_final, refits, bare = 0, 0, False, False, False, False, False, 0, False
        looped, circles = 0, False  # replies in a row that went round in circles; it answers without tools for that
        reply_cap = want

        def text_all():
            return "\n\n".join(p["text"] for p in parts if p.get("text"))

        def save(partial=True, force=False):
            if force or time.time() - state["last"] > 0.6:
                self.s.set_message(mid, text_all(), dict(meta, partial=partial, reasoning=state["reasoning"]))
                state["last"] = time.time()

        def on_text(s):
            if not parts or "text" not in parts[-1]:
                parts.append({"text": ""})
            parts[-1]["text"] += s
            save()

        def on_reasoning(s):
            state["reasoning"] += s
            save()

        def emit(ev):  # a running call's live output
            if ev.get("type") == "tool_out" and cur["part"] is not None:
                live = cur["part"]["tool"].get("live", "") + str(ev.get("text") or "")
                cur["part"]["tool"]["live"] = live[-4000:]
            events.put(dict(ev, id=cur["id"]))

        def ask(req):
            req = {k: str(req.get(k) or "")[:2000] for k in ("command", "folder", "why")}

            def opened(aid):
                parts.append({"ask": dict(req, id=aid, call=cur["id"], open=True)})
                save(force=True)
                events.put(dict(req, type="ask", id=aid, call=cur["id"]))
            decision, aid, how = _tools.ASKS.wait(
                chat_id, req, stop, on_open=opened,
                auto_allow=lambda: bool(self.s.chat(chat_id).get("auto_approve")),
                on_allow_all=lambda: self.s.set_auto_approve(chat_id, True))
            if how == "chat_auto":
                parts.append({"ask": dict(req, id=aid, call=cur["id"], open=False,
                                          decision=decision, how=how)})
            for p in parts:
                if p.get("ask", {}).get("id") == aid:
                    p["ask"].update(open=False, decision=decision, how=how)
            save(force=True)
            events.put({"type": "ask_done", "id": aid, "decision": decision, "how": how})
            return decision

        def close():  # the answer as it stands, finished (the turn's end, or the user's message cut in after it)
            for p in parts:  # an ask still open when the turn ended (Stop): denied
                if p.get("ask", {}).get("open"):
                    p["ask"].update(open=False, decision="deny", how="stop")
                if p.get("tool", {}).get("status") == "running":
                    p["tool"].update(status="stopped")
            tnow = time.time()
            meta.update(finish=finish or ("stopped" if stop.is_set() else None), usage=meta.pop("last_usage", None), usage_total=usage,
                        error=err, stopped=stop.is_set(), reasoning=state["reasoning"], calls=calls_n,
                        ttft_s=round(first - meta["started"], 3) if first else None, total_s=round(tnow - meta["started"], 3))
            meta.pop("partial", None)
            self.s.set_message(mid, text_all(), meta)

        def steered(got, in_step=False):
            """the user's messages cut in (written into the chat already): on the page this answer ends, the user's
            messages follow and a new answer goes on. The model reads them at a step's end (in_step) inside that step's
            last result, so its turn and its reasoning carry on (STEER_IN); after an answer it had finished, as the
            user's next turn."""
            nonlocal meta, parts, mid, usage, first, finish, calls_n, t_start, step, repeats, nudges, stalled, asked, again, room_final, bare
            nonlocal looped, circles
            close()
            nudges, stalled, asked, again, room_final, bare = 0, False, False, False, False, False
            looped, circles = 0, False
            last = msgs[-1] if in_step and msgs and msgs[-1].get("role") == "tool" and isinstance(msgs[-1].get("content"), str) else None
            for g in got:
                if last is not None:
                    last["content"] += STEER_IN + g["content"] + STEER_OUT
                else:
                    msgs.append({"role": "user", "content": g["content"]})
                yield {"type": "user", "id": g["id"], "content": g["content"], "meta": g["meta"], "from_outbox": g["outbox"]}
            meta = dict({k: meta[k] for k in ("model", "profile", "effort", "context", "level") if k in meta},
                        started=time.time(), dropped=0, steered=True, parts=[])
            parts = meta["parts"]
            mid = self.s.add_message(chat_id, "assistant", "", meta)
            state.update(last=0.0, reasoning="")
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
            first, finish, calls_n, t_start, step, repeats = None, None, 0, time.time(), 0, 0
            seen.clear()
            tctx.deadline = time.monotonic() + _tools.TURN_WALL_S
            yield {"type": "start", "id": mid, "model": meta["model"], "level": tctx.level}

        tctx.emit, tctx.ask = emit, ask
        tctx.deadline = time.monotonic() + _tools.TURN_WALL_S
        step = 0
        try:
            # past MAX_STEPS: the answer without tools, asked once more, and the prompts fitted again (each ends or goes on)
            while step <= _tools.MAX_STEPS + 2 + REFITS:  # (the toolsets' before() ran in _turn, before the request was built)
                step += 1
                if stop.is_set():
                    break
                spent = step > _tools.MAX_STEPS or time.time() - t_start > _tools.TURN_WALL_S or calls_n >= _tools.MAX_CALLS
                final = spent or repeats >= _tools.REPEAT_LIMIT or stalled
                if final and not asked:  # once an answer (asked once more after it: NUDGE "final")
                    asked = True
                    msgs.append({"role": "user", "content": FINAL_ASK.format(
                        why=FINAL_SPENT if spent else FINAL_REPEATS if repeats >= _tools.REPEAT_LIMIT else FINAL_LOOP if circles else FINAL_STALLED)})
                    why = (NOTE_SPENT.format(steps=_tools.MAX_STEPS, calls=_tools.MAX_CALLS, mins=_tools.TURN_WALL_S // 60)
                           if spent else NOTE_REPEATS if repeats >= _tools.REPEAT_LIMIT else NOTE_LOOP if circles else NOTE_STALLED)
                    parts.append({"note": why})
                    save(force=True)
                    yield {"type": "note", "text": why}
                # the answer without tools still offers them (FINAL_ASK says nothing more runs): the engine renders the
                # tools right after the system words, so dropping them re-read the whole conversation (a lead's 131K at
                # its time limit, 143 s); only an answer that calls one anyway, or no room for them, goes without (bare)
                tools_chars = 0 if bare else len(json.dumps(specs))
                foldable = (sum(1 for m in msgs[turn0:] if m["role"] == "assistant" and m.get("tool_calls")) > KEEP_STEPS
                            or turn0 > 2)  # older steps of this answer, or the conversation before it
                if (not final and compactions < TURN_COMPACTIONS and foldable
                        and est_tokens(msgs, cpt) + int(tools_chars / cpt) + min(want, 2048) > COMPACT_AT * ctx):
                    compactions += 1
                    cid = f"compact-{mid}-{compactions}"
                    cpart = {"tool": {"id": cid, "name": "compact", "line": "Compacting earlier steps to make room", "status": "running"}}
                    parts.append(cpart)
                    save(force=True)
                    yield {"type": "tool", "id": cid, "line": cpart["tool"]["line"]}
                    t0c = time.time()
                    try:
                        new, turn0, how = self._compact_turn(chat_id, endpoint, served_model, ctx, cpt, msgs, turn0, stop)
                    except RuntimeError:  # Stop during the summary
                        cpart["tool"].update(status="stopped", line="Compacting stopped")
                        yield {"type": "tool_done", "id": cid, "line": "Compacting stopped", "status": "stopped", "out": ""}
                        break
                    msgs[:] = new
                    line = f"Compacted to make room: {how}" if how else "Compacting found nothing older to fold"
                    cpart["tool"].update(status="ok", line=line, secs=round(time.time() - t0c, 2), out="")
                    save(force=True)
                    yield {"type": "tool_done", "id": cid, "line": line, "status": "ok", "secs": cpart["tool"]["secs"], "out": ""}
                room = ctx - EXTENT_PAD - SAFETY_TOKENS - est_tokens(msgs, cpt) - int(tools_chars / cpt)
                if room < min(MIN_REPLY, want) and final and not bare:  # the answer fits without the tools: they go
                    bare, tools_chars = True, 0  # (it ended on "longer than the window" instead: review)
                    room = ctx - EXTENT_PAD - SAFETY_TOKENS - est_tokens(msgs, cpt)
                if room < min(MIN_REPLY, want):
                    if final or step == 1 or not done_any:
                        err = longer_words(model_of(chat), profile_of(chat))
                        break
                    final = room_final = bare = True  # no room for more tool output: answer from what is here
                    msgs.append({"role": "user", "content": "(The conversation is nearly full. Answer the user now, without tools.)"})
                    room = ctx - EXTENT_PAD - SAFETY_TOKENS - est_tokens(msgs, cpt)
                    if room < min(MIN_REPLY, want):
                        err = longer_words(model_of(chat), profile_of(chat))
                        break
                sampling = dict(_tools.SAMPLED, seed=sample_seed()) if sample else {"temperature": 0}
                body = dict({"model": served_model, "stream": True, "stream_options": {"include_usage": True},
                             "max_tokens": min(reply_cap, room),
                             "chat_template_kwargs": {"enable_thinking": e["thinking"] and not looped},  # (after a loop: no more of it)
                             **(think_fields(e) if not looped else {}),
                             "messages": msgs}, **sampling)
                if not bare:
                    body["tools"], body["tool_choice"] = specs, "auto"
                note_start(chat_id, body, step, cpt)
                if hold:  # after a NUDGE "said" or "stray": its words show only if it carries on, never a bare DONE
                    res = yield from self._held_step(entry, endpoint, body, cpt, on_reasoning, by_name)
                    hold = False
                    if res["calls"] or not _tools.said_done(res["text"]):
                        lead = "" if not parts or "text" not in parts[-1] or parts[-1]["text"].endswith("\n") else "\n\n"
                        if res["text"]:
                            on_text(lead + res["text"])
                            yield {"type": "delta", "text": lead + res["text"]}
                    else:
                        res["text"], res["nudged_done"] = "", True
                else:
                    res = yield from self._stream_step(entry, endpoint, body, cpt, on_text, on_reasoning, by_name)
                if res.get("err") and e["thinking"] and res.get("status") and not done_any:  # thinking with tools refused: once without
                    body["chat_template_kwargs"] = {"enable_thinking": False}
                    meta["thinking_off"] = True
                    res = yield from self._stream_step(entry, endpoint, body, cpt, on_text, on_reasoning, by_name)
                if res.get("err") and sample and res.get("status") and not done_any and not ctx_refusal(str(res["err"])):
                    sample = False  # an engine that won't sample: greedy for the rest of the turn, as before
                    body = dict({k: v for k, v in body.items() if k not in _tools.SAMPLED and k != "seed"}, temperature=0)
                    meta["sampled_off"] = True
                    res = yield from self._stream_step(entry, endpoint, body, cpt, on_text, on_reasoning, by_name)
                if res.get("retract") and parts and "text" in parts[-1]:  # a JSON call it had shown as words
                    parts[-1]["text"] = parts[-1]["text"][:-res["retract"]]
                sent = sum(msg_chars(m, cpt) for m in msgs) + len(json.dumps(body.get("tools") or ""))
                pt = int((res.get("usage") or {}).get("prompt_tokens") or 0)
                if pt > 200:  # the engine's own count: what tool output really costs (code and JSON take more tokens)
                    cpt = max(1.0, min(cpt, sent / pt * 0.95))
                first = first or (res.get("ttft") and time.time())
                for k in ("prompt_tokens", "completion_tokens"):
                    usage[k] += int((res.get("usage") or {}).get(k) or 0)
                meta["last_usage"] = res.get("usage")
                if res.get("usage"):  # this answer's speeds, step by step (the footer's tok/s and pp, the Speed sheet)
                    sp = _tools.step_speed(res["usage"], res.get("engine"), res.get("ttft"), res.get("elapsed"))
                    meta["speed_steps"] = (meta.get("speed_steps") or [])[-59:] + [sp]
                    meta["speed"] = _tools.speed_sum(meta["speed_steps"])
                lu = res.get("usage") or {}
                if lu.get("prompt_tokens"):  # the context tracker follows each step (the page's ring and 90% warning)
                    yield {"type": "window", "used": int(lu["prompt_tokens"]) + int(lu.get("completion_tokens") or 0), "context": ctx}
                fit = ctx_refusal(str(res.get("err") or ""))
                if fit and refits < REFITS and not stop.is_set():  # longer than its estimate (dense tool output): fitted
                    refits += 1  # again by the engine's own count, next step (compacted, a smaller reply, or no tools)
                    cpt = max(0.5, min(cpt, sent / max(1, fit[1] or int(fit[0] * 1.2)) * 0.95))
                    _log(f"chat: the engine counted the prompt longer than estimated at step {step}; fitted again")
                    continue
                if res.get("err"):
                    err = (longer_words(model_of(chat), profile_of(chat)) if fit
                           else f"the model answered: {str(res['err'])[:300]}")
                    break
                if stop.is_set():
                    break
                finish = res.get("finish")
                cut_call = finish == "length" and bool(res.get("written") or res.get("garbled") or (res.get("shape") and not res["calls"]))
                if cut_call:  # a call cut off by the reply cap never runs: its arguments are cut too
                    res["calls"], res["garbled"] = [], False
                if finish == "length":  # the next reply gets more room (a whole file in one call)
                    reply_cap = max(reply_cap, min(TOOL_REPLY_MAX, reply_cap * 2))
                ended = not res["calls"] or final
                if ended:
                    done_any = done_any or res.get("done") or bool(finish)
                    if not (res.get("done") or finish):
                        err = "The answer was cut off: the model stopped before it finished. Retry when it is back."
                        meta["interrupted"] = True
                        break
                    text = res["text"] or ""
                    stray = not final and _tools.stray_call(text, by_name)  # a call to a tool it hasn't ("bash"), as words
                    short = None
                    waits = not final and _lead_waits(chat_id, text)  # a team lead ending its answer to wait for its agents
                    if stop.is_set() or res.get("nudged_done"):
                        pass
                    elif final:  # the answer without tools said nothing or only what it would do (a call it made anyway
                        # is dropped): asked once more, then without the tools in view; a real answer with a call after it stands
                        if res["calls"] or res.get("written") or res.get("garbled"):
                            bare = True
                        if not again and not room_final and (not text.strip() or _tools.announces(text)):
                            short = "final"
                    elif res.get("loop"):  # sent back once; again, and it answers without tools
                        looped += 1
                        meta["loops"] = meta.get("loops", 0) + 1
                        short = "loop" if looped == 1 else None
                        if looped > 1:
                            circles = meta["circles"] = True  # (a goal pauses for it: chat_goal)
                        if short and parts and parts[-1].get("text") and not parts[-1]["text"].endswith("\n"):
                            on_text("\n\n")
                            yield {"type": "delta", "text": "\n\n"}
                    elif nudges < NUDGES:
                        short = ("cut_call" if cut_call else "garbled" if res.get("garbled") else "cut" if finish == "length"
                                 else "stray" if stray else "said" if _tools.announces(text) and not waits
                                 else "empty" if not text.strip() else None)
                    back = {"role": "assistant", "content": text}
                    if res.get("reasoning"):
                        back["reasoning_content"] = res["reasoning"]
                    if short:  # it stopped short: back to it (NUDGE), the turn goes on
                        nudges += short != "final"
                        nudged += 1
                        again = again or short == "final"
                        meta["nudges"] = nudged
                        msgs.extend([back, {"role": "user", "content": NUDGE[short]}])
                        hold = short in ("said", "stray")  # its next words show only if it carries on, never a bare DONE
                        _log(f"chat: a reply stopped short ({short}{', ' + res['shape'] if res.get('shape') else ''}) at step "
                             f"{step} after {calls_n} call(s); sent back ({nudged} in this answer)")
                        continue
                    if (not final and not stop.is_set() and not res.get("nudged_done")
                            and (cut_call or res.get("garbled") or res.get("loop") or stray or (_tools.announces(text) and not waits)
                                 or not text.strip())):
                        stalled = True  # still short after NUDGES in a row: it answers without tools, never on a promise
                        msgs.append(back)
                        _log(f"chat: a reply went round in circles again ({res['loop']}) at step {step}; it answers without tools" if res.get("loop")
                             else f"chat: a reply stopped short {nudges} times in a row at step {step}; it answers without tools")
                        continue
                    if final and res.get("loop"):
                        meta["loops"] = meta.get("loops", 0) + 1
                        parts.append({"note": NOTE_LOOPED})
                        yield {"type": "note", "text": NOTE_LOOPED}
                        _log(f"chat: an answer without tools went round in circles ({res['loop']}); stopped there")
                    if res.get("shape") and not res["calls"]:
                        _log(f"chat: an answer ended on words shaped like a tool call ({res['shape']}) at step {step}")
                    got = self._take_steers(chat_id, tctx)  # the user wrote while it answered: it goes on, with the user's words
                    if not got:
                        break
                    msgs.append({"role": "assistant", "content": text})
                    yield from steered(got)
                    continue
                done_any = True
                nudges = looped = 0  # it carries on with its tools: a later stall starts afresh
                take = max(1, min(_tools.STEP_CALLS, _tools.MAX_CALLS - calls_n))  # never past the turn's MAX_CALLS
                calls, over = res["calls"][:take], res["calls"][take:]
                # the calls past STEP_CALLS stay in its reply and are answered "not run" (review "model": 2% of
                # replies with tools made 5 or 6 calls; dropped without a word, its 5th and 6th edits never happened)
                said = {"role": "assistant", "content": res["text"] or "", "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for c in calls + over]}
                if over:
                    meta["over"] = meta.get("over", 0) + len(over)
                if res.get("reasoning"):
                    said["reasoning_content"] = res["reasoning"]  # DeepSeek's interleaved thinking reads its own back
                msgs.append(said)
                fresh = True  # no call of this step repeats an earlier one: a repeat before is forgiven
                for c in calls:
                    name = c["name"]
                    try:
                        args = json.loads(c["args"] or "{}")
                        bad = not isinstance(args, dict)
                    except ValueError:
                        bad = True
                    args = args if not bad else {}
                    calls_n += 1
                    t = by_name.get(name)
                    if t is not None and not bad:
                        args = _tools.coerce(args, next((x for x in specs if (x.get("function") or {}).get("name") == name), None))
                    lv = tctx.current_level() if tctx.level != "none" else "none"
                    part = {"tool": {"id": c["id"], "name": name, "line": _brief_line(name, args), "status": "running"}}
                    parts.append(part)
                    cur.update(id=c["id"], part=part)
                    yield {"type": "tool", "id": c["id"], "name": name, "line": part["tool"]["line"]}
                    save(force=True)
                    if t is None:
                        r = _tools.result(f"There is no tool called {name}.", f"Unknown tool {name[:40]}", "refused", "refused")
                    elif bad:  # never run with no arguments: it is told, and makes the call again
                        r = _tools.result("This call's arguments were not a valid JSON object, so it didn't run. Make the call "
                                          "again with its arguments as one JSON object.", f"{name}: unreadable arguments",
                                          "refused", "refused")
                    elif _tools.RANK[lv] < _tools.RANK[t.level]:
                        r = _tools.result(f"the user changed this chat's permissions to {_tools.WORDS[lv]['name']}: this call didn't run, "
                                          "and tools above that level are gone for the rest of this answer.",
                                          f"Not run: permissions are now {_tools.WORDS[lv]['name']}", "refused", "refused")
                    else:
                        box = {}

                        def work(t=t, name=name, args=args, box=box):
                            try:
                                box["r"] = t.call(name, args, tctx)
                            except Exception as ex:  # noqa: BLE001 - a toolset's bug is a failed call, never a dead turn
                                box["r"] = _tools.result(f"The tool failed: {type(ex).__name__}: {str(ex)[:300]}", f"{name} failed", "failed")
                        # the turn's context goes with it: a Private chat's grant (its sealed folder's key comes from the chat's)
                        import contextvars
                        th = threading.Thread(target=contextvars.copy_context().run, args=(work,), name="halo-chat-tool", daemon=True)
                        th.start()
                        while th.is_alive() or not events.empty():
                            try:
                                ev = events.get(timeout=0.2)
                            except queue.Empty:
                                continue
                            if ev.get("type") == "tool_out":
                                save()
                            yield ev
                        r = box.get("r")
                        if not (isinstance(r, dict) and isinstance(r.get("text"), str) and isinstance(r.get("line"), str)
                                and r.get("status") in _tools.STATUSES):  # a toolset's bug is a failed call, never a dead turn
                            r = _tools.result("The tool failed: it gave back no result.", f"{name} failed", "failed")
                    # the same call with the same result as earlier in this answer: it learns nothing new, and is told so
                    # in a line rather than the result again; REPEAT_LIMIT of them and it answers without tools (a step of
                    # only new calls takes one off: a long task that checks something again now and then isn't a loop)
                    key = _tools.call_key(name, args)
                    same = seen.get(key) == r["text"] and not getattr(t, "repeats_ok", False)
                    seen[key] = r["text"]
                    if same:
                        repeats += 1
                        fresh = False
                        r = dict(r, line=r["line"] + " · same result as before")
                    pub = {k: r.get(k) for k in ("line", "status", "decision", "exit", "secs", "changed", "refused")}
                    pub["shown"] = (r.get("audit") or {}).get("shown")
                    pub["file"] = (r.get("audit") or {}).get("file")  # a file the call made (chat_docs): its card
                    pub["plan"] = (r.get("audit") or {}).get("plan")  # the checklist update_plan keeps (chat_plan)
                    pub["replaces"] = None  # ... made again in this answer: the earlier card and its file go
                    for p_ in parts if (r.get("audit") or {}).get("replaces") else ():
                        if ((p_.get("tool") or {}).get("file") or {}).get("id") == r["audit"]["replaces"]:
                            p_["tool"]["file"], pub["replaces"] = None, r["audit"]["replaces"]
                            self.s.drop_made(pub["replaces"])
                    part["tool"].update(pub, out=r.get("out") or "")
                    part["tool"].pop("live", None)
                    yield dict(pub, type="tool_done", id=c["id"], out=(r.get("out") or "")[-_tools.OUT_PAGE:])
                    _tools.audit(self.s, chat_id, dict({"msg": mid, "level": lv, "model": tctx.model_id, "tool": name, "args": args,
                                                        "status": r["status"], "decision": r["decision"], "exit": r.get("exit"),
                                                        "secs": r.get("secs"), "out_bytes": len(r.get("out") or ""),
                                                        "changed": r.get("changed"), "refused": r.get("refused"),
                                                        **({"repeat": True} if same else {})},
                                                       **{k: v for k, v in (r.get("audit") or {}).items() if k not in ("shown", "file")},
                                                       **({"made": r["audit"]["file"]["id"]} if (r.get("audit") or {}).get("file") else {})),
                                 private=tctx.private)
                    room = ctx - EXTENT_PAD - SAFETY_TOKENS - MIN_REPLY - est_tokens(msgs, cpt) - int(tools_chars / cpt)
                    cap = min(_tools.READ_MODEL if name == "read_file" else _tools.OUT_MODEL, max(0, room) * int(cpt) // max(1, len(calls)))
                    text = REPEATED if same else _tools.ends(r["text"], cap) if cap >= 400 or len(r["text"]) <= cap else (
                        "(No room left in the conversation for this output: answer with what you have.)")
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": text})
                    cur.update(id=None, part=None)
                    save(force=True)
                    if stop.is_set():
                        break
                if fresh and repeats:
                    repeats -= 1
                for c in over:  # no step ran, so the page shows none
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": OVER.format(n=take)})
                got = [] if stop.is_set() else self._take_steers(chat_id, tctx)  # the step is done: the user's message, if any
                if got:
                    yield from steered(got, in_step=True)
        except Exception as ex:  # noqa: BLE001 - a bug here ends the answer with its reason kept (the page shows it after a
            # reload too), never as a tool call and then nothing
            err = f"StrixFold UI hit a problem and stopped this answer ({type(ex).__name__}). Send a message to carry on."
            _log("chat: a turn died: " + _where(ex))
        finally:
            close_toolsets(entry)
            close()
            self.relay.release(chat_id, entry)
        log_end(self.s, chat_id, err, stop.is_set(), step, calls_n,
                next((p["tool"].get("name") for p in reversed(parts) if "tool" in p), None))
        if err:
            yield {"type": "error", "error": err, "id": mid}
        yield {"type": "done", "id": mid, "finish": meta["finish"], "usage": meta.get("usage"), "stopped": stop.is_set()}


    def _take_steers(self, chat_id, tctx):
        """The messages the user sent while this chat answered (studio_http.queue_while_answering: meta.steer), written into
        the chat now, each leaving the outbox in the same step (Store.add_message's from_outbox), oldest first. Only the
        leading ones, and only words: a waiting message that isn't one of them (a file with it, Send without tools, one
        that waited for the model) keeps its place, and it and those after it go when the answer ends (the outbox).
        A Private chat's are read and written sealed, with the turn's grant."""
        got = []
        for w in self.s.waiting(chat_id):
            m = w.get("meta") or {}
            text = str(w.get("content") or "")
            if m.get("team") or m.get("goal"):  # a team update (chat_team._wake) or a goal's turn waits for the answer's end; it never holds the user's words back
                continue        # (review: one waiting ahead kept the user's message out of every step, and Send now too)
            words = m.get("steer") and not m.get("attachments") and not m.get("no_tools") and text.strip()
            if not words:
                break
            um = {"attachments": [], "effort": m.get("effort"), "steer": True}
            with self.relay.mu:  # Send now (chats.Relay.send_now) marks under it: one the user pressed it on goes on its own
                if self.relay.is_now(w):
                    break
                try:
                    uid = self.s.add_message(chat_id, "user", text, um, from_outbox=w["id"])
                except studio.OutboxGone:
                    continue  # cancelled just now
            got.append({"id": uid, "content": text, "meta": um, "outbox": w["id"]})
        return got


def _lead_waits(chat_id, text):
    """chat_team.lead_waits: the team lead's answer ends to wait for its working agents (not a step it stopped short of)."""
    try:
        import chat_team
        return chat_team.lead_waits(chat_id, text)
    except Exception:  # noqa: BLE001 - never a dead turn over it
        return False


def _brief_line(name, args):
    """A call's line before it runs ("Running: npm test"); its toolset's own line replaces it when it ends."""
    a = args or {}
    s = a.get("summary") or a.get("command") or a.get("path") or a.get("pattern") or a.get("query") or a.get("url") or a.get("image_id") or ""
    s = " ".join(str(s).split())
    s = s if len(s) <= 80 else s[:79] + "…"
    return {"run_command": f"Running: {s}", "web_search": f"Searching the web for “{s}”", "web_read": f"Reading {s}",
            "read_pages": "Reading pages", "look_at_page": f"Looking at page {a.get('page') or ''}".rstrip(),
            "form_fields": "Reading the form's fields", "fill_form": "Filling in the form", "add_text": "Adding text",
            "combine_pdfs": "Combining pages", "make_pdf": "Making a PDF"}.get(
        name, f"{name.replace('_', ' ').capitalize()}{': ' + s if s else ''}")


def _lease(endpoint, key):
    """A slot on the request's pair for as long as it runs (conductor.track): the conductor counts it, so agents aren't
    lent the main boxes meanwhile (review: a plain answer or a summary took none)."""
    import conductor
    return conductor.C.track(endpoint, "chat", key or "", label="the user's chat") if conductor.C is not None else None


def _unlease(lease):
    import conductor
    if conductor.C is not None:
        conductor.C.release(lease)


def read_timeout(tokens, floor=900):
    """How long a reply may keep the socket silent: the engine sends nothing while it reads the prompt (unc 512K's full
    window about 7.5 min, 1M's about 16), so 300 s plus 1 s per 800 prompt tokens (1M about 26 min), at least `floor`."""
    return max(floor, 300 + tokens / 800)


def _log(msg):
    """One line in the app's log (its stdout: ~/Library/Logs/halo-control.log, as halo_control.log writes)."""
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def _where(ex):
    """An exception as its type and the code it went through, innermost first: never its message (it may hold a chat's
    words)."""
    tb = traceback.extract_tb(ex.__traceback__)[-5:][::-1]
    return f"{type(ex).__name__} at " + " < ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in tb)


def log_end(store, chat_id, err, stopped, step=None, calls=None, last_tool=None):
    """A turn that ended on an error or was stopped, in the app's log (left no trace there; the page alone showed why).
    The step, the calls and the last tool's name; the error's words unless the chat is Private."""
    if not err and not stopped:
        return
    try:
        private = store.is_private(chat_id)
    except Exception:  # noqa: BLE001
        private = True
    where = "" if step is None else f" at step {step} after {calls} call(s)" + (f" (last: {last_tool})" if last_tool else "")
    why = "" if not err else " (Private)" if private else f": {str(err)[:200]}"
    _log(f"chat: a turn {'ended on an error' if err else 'was stopped'}{where}{why}")


_STARTS = {}  # chat id -> its last request's (system words, tools, thinking), hashed in this process only


WINDOW_CAP = 262144  # the most a chat's history grows to on a pair whose pool is larger, unless the chat chose more


def chat_window(chat, served):
    """The window a chat's history is fitted and compacted to: what its pair serves, but past WINDOW_CAP only when the
    chat chose a larger window. The main boxes' 1M pool keeps several long conversations' kept rows (two chats of
    110K and 170K taking turns resumed in 4 s instead of re-reading for 96-149 s); one chat growing toward 750K would
    fill it and, pushed out, read cold for 13-16 minutes."""
    if CONNECTED is not None and CONNECTED():  # connect.py: one server's window, and the chat keeps to the size it chose
        return min(served, context_of(profile_of(chat)))
    return min(served, max(context_of(profile_of(chat)) or 0, WINDOW_CAP))


def chat_mark(chat):
    """The system text's first line, this chat's own: chats that share the rest of the system text and the tools must
    differ before the engine's first 2,048-token chunk end, or one resumes in place over the other's kept rows and
    the other reads its whole conversation again (on the live engine: 25,826 tokens from 0; with this line the
    other resumed at 22,528 and the new chat read only its own start, +3.2 s once)."""
    return f"(Chat {str(chat.get('id') or '')[:8]}.)\n" if chat.get("id") else ""


def note_start(chat_id, body, step, cpt):
    """The app's log says when a chat's prompt changes at its start (the system words, the tools, the thinking line),
    which re-reads the whole conversation in the engine: which part, never its words (a 92K and a 131K chat read
    again from nothing)."""
    msgs = body.get("messages") or []
    sig = (hash(str(msgs[0].get("content")) if msgs else ""), hash(json.dumps(body.get("tools") or [], sort_keys=True)),
           bool((body.get("chat_template_kwargs") or {}).get("enable_thinking")))
    was, _STARTS[chat_id] = _STARTS.get(chat_id), sig
    if was and was != sig:
        what = [w for w, a, b in zip(("the system words", "the tools", "the thinking line"), was, sig) if a != b]
        _log(f"chat: the prompt's start changed ({', '.join(what)}) at step {step}: the engine reads it again "
             f"(~{est_tokens(msgs, cpt) // 1000}K tokens)")


def run_detached(gen, first):
    """Run a turn's generator on its own thread (the answer is kept when the page goes away) and hand the page its events
    through a queue; None ends them."""
    import queue
    q = queue.Queue()
    q.put(first)

    def run():
        try:
            for ev in gen:
                q.put(ev)
        except Exception as e:  # noqa: BLE001  (the page hears it; the thread must end)
            _log("chat: a turn died: " + _where(e))  # and the app's log, where (never its words)
            q.put({"type": "error", "error": str(e)[:300]})
        finally:
            q.put(None)
    import contextvars
    # the turn's thread keeps the request's context: a Private chat's grant (private_store.GRANT) goes with it
    threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True).start()
    return q
