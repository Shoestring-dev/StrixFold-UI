"""Permissions and tools for a chat (no tools, the research container on Toolbox without internet, the same with
internet, full access like Claude Code).

The chat's own model runs the tools: halo_chat.ChatAgent._turn is a tool loop (this module's limits, asks and audit);
what a level offers comes from toolsets. A toolset is a plug-in module with `toolset(ctx) -> Toolset | None` that
decides from the turn's context whether it applies:

  chat_box    Container and Container + web (Toolbox halo-tools, a bwrap sandbox per call)
  chat_full   Full access on this Mac
  chat_look   look_at_image for a model that can't see photos (any level)
  chat_docs   the chat's PDFs: read, look at, fill in, write on, combine; make a PDF (any level)
  chat_browse a browser on the VPS: open, read, click, type, look (Container + web and up)

A module that isn't there yet is skipped, so each lands on its own. The loop owns: the model requests, at most
STEP_CALLS calls taken from one reply, MAX_STEPS replies, MAX_CALLS calls and TURN_WALL_S per turn, the level read
again before every call (a lower level refuses the call; a higher one waits for the next message), the asks (a tap:
ask/ask_done events, the answer route, ASK_TIMEOUT_S, Stop = deny), the page events, meta.parts and the audit file.
A toolset owns what its calls do, their own timeouts and rules, and honours ctx.stop.

Storage (studio.Store, the chats table): perm (LEVELS; NULL = "none") and perm_opts (JSON; Full: {"scope": "folder" |
"home", "folder": path, "dirs": [{"path", "write"}]}). The send body never carries the level: the server reads the row.
"""
import json
import re
import os
import secrets
import threading
import time

LEVELS = ("none", "box", "box_web", "full", "direct")
DEFAULT = "none"
# the chip (short on phones) and the sheet's row, in plain words
WORDS = {
    "none": {"chip": "Off", "name": "No tools", "desc": "Talks only. It can't run anything."},
    "box": {"chip": "Box", "name": "Container", "desc": "Runs commands and edits files in its own folder on Toolbox. No internet."},
    "box_web": {"chip": "Box+web", "name": "Container + web",
                "desc": "The same, plus web search, downloads and a browser on your VPS, through the filter. Never your home network."},
    "full": {"chip": "Full", "name": "Full access",
             "desc": "Runs commands and changes files on this Mac, like Claude Code. Risky commands wait for your tap."},
}
WORDS["direct"] = {"chip": "Direct", "name": "Direct access", "desc": "Run commands and edit files on the computers you add to direct_targets. No approval prompts or network filter."}
RANK = {"none": 0, "box": 1, "box_web": 2, "full": 3, "direct": 4}

PRIVATE_NO_FULL = "Full access isn't offered in a Private chat: its files would sit unsealed on this Mac."
STEP_CALLS = 4          # tool calls taken from one model reply (lead_tools.STEP_CALLS)
# a browsing turn hit the old limits (24 replies and 40 calls). The repeat check (REPEAT_LIMIT) still ends a turn that goes round in circles
MAX_STEPS = 96          # model replies in one turn
MAX_CALLS = 160         # tool calls in one turn
TURN_WALL_S = 30 * 60   # a turn with tools holds the chat (and makes Studio wait) at most this long
REPEAT_LIMIT = 3        # calls in one answer that repeat an earlier one and get its same result: then it answers without tools
# a turn with tools samples (DeepSeek kept getting stuck in loops): at temperature 0 it wrote
# the same four "Let me search…" lines and calls over and over until the limits. DeepSeek's and Qwen's own advice for
# thinking models is about 0.6, never greedy; the DeepSeek engine wants top_k (1-56) with it, and a seed to vary at all
SAMPLED = {"temperature": 0.6, "top_k": 40}
ASK_TIMEOUT_S = 10 * 60 # an unanswered tap is a Deny
OUT_PAGE = 20_000       # a call's output kept for the page (meta.parts); the model gets its own `text`
# Switches for an eval harness (not in this repo): a change under test sits behind a name here until a run shows it
# helps. ON: the ones its runs proved,
# on their own items against the plain build: grounding 0/10 -> 10/10, attach_full 1/11 -> 11/11,
# longer_replies 0/6 -> 6/6 (McNemar p 0.002, 0.002, 0.031; a repeat plain pass flipped none); think_budget changed
# nothing. web_parts: facts past a long page's first part 0/6 -> 6/6 (p 0.031, its first version; the plain run
# was stopped early), and the final version read 13 of 14 (the other found its fact, worded past the check); plain can't
# pass these: the loop never gives the model the text. honest_limits (p 0.062 in its replicate), read_first and
# compact_v2 (no effect) stay off. loop_stop ends a reply that repeats itself and keeps the repeats out of the history;
# off until asked for, as a loop can't be had on demand and so wasn't measured (chat_loops' tests read a real one's
# shape, and text that may repeat). HALO_EXPERIMENTS=a,-b adds a and takes b away, in the app and in the harness.
ON = frozenset({"grounding", "attach_full", "longer_replies", "web_parts"})


def experiments(spec=""):
    names = [x.strip() for x in (spec or "").split(",") if x.strip()]
    return (set(ON) | {x for x in names if x[0] != "-"}) - {x[1:] for x in names if x[0] == "-"}


EXPERIMENTS = experiments(os.environ.get("HALO_EXPERIMENTS", ""))
OUT_MODEL = 12_000      # what the model reads of a call's output, at most (the loop also fits it to the window)

PLUGINS = ("chat_box", "chat_look", "chat_docs", "chat_full", "chat_direct", "chat_browse", "chat_team", "chat_plan")
STATUSES = ("ok", "refused", "denied", "failed", "stopped")
DECISIONS = ("ran", "tap_allowed", "tap_denied", "refused")


class TurnCtx:
    """What a toolset knows about the turn it serves. Built by the loop; toolsets read it, never change it.

    chat_id, chat      the chat's id and row (studio.Store.chat) at the turn's start
    store              studio.Store (the private store)
    model_id           halod's model id SERVING the turn (dsv41, dsv41-unc, qwen38, qwen38-kyojin)
    level, opts        the chat's perm and perm_opts at the turn's start (current_level() reads them again)
    sees               the serving engine reads pictures (halo_chat.sees_images)
    private            the chat is Private: toolsets write nothing in plaintext, the audit is metadata
    stop               threading.Event: Stop pressed, or the turn's wall time is up; calls end as soon as they can
    ask(req)           a tap: req {"command", "folder", "why"} -> "allow" | "deny" (blocks; Stop or ASK_TIMEOUT_S =
    deny)
    emit(ev)           a live page event while a call runs ({"type": "tool_out", "id", "text"}); never raises
    app                the dashboard (web tools' factory, the proxy guard); None in unit tests
    fake               the app runs --fake: toolsets use their fakes (never Toolbox, never the internet)
    deadline           time.monotonic() when the turn's TURN_WALL_S ends (a command never runs past it)
    endpoint           the serving engine's address and its model name (served_model), set by the turn (chat_docs asks
                       a seeing engine about a page); None in unit tests
    """

    def __init__(self, chat_id, chat, store, model_id, level=DEFAULT, opts=None, sees=False, private=False, stop=None,
                 ask=None, emit=None, app=None, fake=False):
        self.chat_id, self.chat, self.store, self.model_id = chat_id, chat, store, model_id
        self.level, self.opts, self.sees, self.private = level, dict(opts or {}), sees, private
        self.stop = stop or threading.Event()
        self.ask = ask or (lambda req: "deny")
        self.emit = emit or (lambda ev: None)
        self.app, self.fake = app, fake
        self.endpoint = self.served_model = None
        self.deadline = time.monotonic() + TURN_WALL_S

    def time_left(self):
        return max(0.0, self.deadline - time.monotonic())

    def current_level(self):
        """The chat's level now (the user may lower it while the turn runs)."""
        try:
            return level_of(self.store.chat(self.chat_id))
        except KeyError:
            return DEFAULT


class Toolset:
    """A plug-in's tools for one turn. `level` is the lowest chat level its calls need ("none" for look_at_image):
    the loop refuses a call when the chat's level has gone below it."""
    level = "none"

    def specs(self):
        """OpenAI tool specs ({"type": "function", "function": {...}}) offered this turn."""
        return []

    def before(self, ctx):
        """A generator run once before the turn's request is built (so what it waits for, e.g. a photo's description,
        is in it): page events it yields go to the page ({"type": "looking", "photos": 2} while descriptions finish;
        a {"type": "note", "text"} is also kept in meta.parts). It runs before the "start" event. Default: nothing."""
        return iter(())

    def system_note(self, ctx):
        """A sentence or two added to the system prompt for this turn ("" for none)."""
        return ""

    def turn_note(self, ctx):
        """Words about this message only ("" for none), put after the user's newest message: the system prompt's start must stay
        the same from turn to turn, or the engine reads the whole conversation again (a 100K chat: ~2 min)."""
        return ""

    def call(self, name, args, ctx):
        """Run one call of a tool this toolset offered. Returns result(...). Never raises for an ordinary failure: a
        refusal or a failure is a result the model reads."""
        raise NotImplementedError

    def close(self):
        """The turn ended (scratch folders, proxies, sessions)."""


# A reply that says what it will do next and then ends without the call: the loop sends it back once or twice to do it,
# or to say it is done (halo_chat.NUDGE, chat_team)
ACT_NOW = ("When you say you will do something with your tools, make that call in the same reply. Never end a reply by "
           "announcing a next step: take it, or give the user your answer.")
_INTENT = re.compile(r"\b(let me|let's|i'll|i will|i'm going to|i am going to|i'm now going to|i need to|now i(?:'ll| will)?|"
                     r"next,? i|first,? i|then i'll|i should|i can now|time to|going to)\b", re.I)
# ... or says it is doing it as it ends ("Running the tests now.", "Checking the config.", "On it.")
_DOING = re.compile(r"^(?:(?:ok(?:ay)?|alright|right|now|next|then|so)[,:]?\s+)*(?:running|re-?running|checking|reading|looking|opening|"
                    r"creating|writing|installing|building|testing|fetching|starting|trying|retrying|searching|proceeding|"
                    r"continuing|executing|launching|loading|downloading|updating|fixing|applying|editing|adding|verifying|"
                    r"inspecting|examining|pulling|cloning|compiling|setting up|making|generating|calling|scanning|moving on|"
                    r"heading|digging|diving|grabbing|switching|restarting|rebuilding|patching)\b[^.!?]{0,100}[.!:…]*$"
                    r"|\b(?:on it|one moment|stand by|hang on|will now|about to)\b", re.I)
_NOT_INTENT = re.compile(r"\b(let me know|if you(?:'d)? like|if you want|want me to|would you like|should i|shall i|do you want|"
                         r"happy to|feel free|i'll leave|i will leave|say so|say the word|just say|tell me (?:if|whether|and))\b",
                         re.I)
# ... or says what it will run and then shows the command in a code block instead of running it ("Let me check the
# logs:" and a ```bash block): the page shows what looks like a step, and the answer ended there (review "model")
_FENCE_END = re.compile(r"\n[ \t]*```(?:bash|sh|shell|zsh|console)?[ \t]*\n(?:(?!```).)*\n[ \t]*```\s*$", re.S)
# (a shell block only: "Here's what I'll change:" and a ```diff, or "Let's look at an example:" and a ```python, are
# answers, not steps; gate review)


def announces(text):
    """True when a reply ends by saying what it is about to do (a call it never made): its last sentence says "let me /
    I'll / next I ..." or that it is doing it ("Running the tests now.") and isn't a question or an offer, or it ends on
    a colon or an ellipsis; or it says so and then shows the command in a code block instead of running it."""
    t = (text or "").strip()
    m = _FENCE_END.search(t)
    if m and t[:m.start()].strip():  # what it said before the block it ended on
        before = [x for x in re.split(r"(?<=[.!])\s+|\n+", t[:m.start()].strip()[-500:]) if x.strip()]
        return bool(before) and bool(_INTENT.search(before[-1])) and not _NOT_INTENT.search(before[-1])
    if not t or t.endswith("?"):
        return False
    tail = [x for x in re.split(r"(?<=[.!])\s+|\n+", t[-500:]) if x.strip()]
    last = tail[-1] if tail else t
    if _NOT_INTENT.search(" ".join(tail[-2:])):
        return False
    return t.endswith((":", "…", "...")) or bool(_INTENT.search(last)) or bool(_DOING.search(last.strip()))


def step_speed(usage, engine=None, ttft=None, elapsed=None):
    """One request's speeds: decode tok/s, prompt reading tok/s over the uncached prompt (pp), and the wait for a slot,
    from the engine's own clock (TensorFold's "tensorfold" block, llama.cpp's "timings"), else decode from the stream's
    clock (first token to the end)."""
    u = usage or {}
    pin, out = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    e = engine or {}
    s = {"in": pin, "cached": cached, "out": out, "tps": None, "pp": None, "wait": None}
    if e.get("tokens_per_second") is not None:  # TensorFold
        pre = float(e.get("prefill_seconds") or 0)
        s["tps"] = round(float(e["tokens_per_second"]), 1) if out > 1 else None
        s["pp"] = round((pin - cached) / pre) if pre > 0.02 and pin - cached > 32 else None
        s["wait"] = round(max(0.0, float(e.get("time_to_first_token") or 0) - pre), 1)
    elif e.get("predicted_per_second") is not None:  # llama.cpp's timings
        s["tps"] = round(float(e["predicted_per_second"]), 1) if out > 1 else None
        s["pp"] = round(float(e.get("prompt_per_second") or 0)) or None
    elif ttft and elapsed and elapsed - ttft > 0.2 and out > 1:
        s["tps"] = round(out / (elapsed - ttft), 1)
    return s


def speed_sum(steps):
    """A message's (or an agent's) speeds over its steps: decode and pp weighted by tokens, what it read and wrote."""
    dec = sum(x["out"] / x["tps"] for x in steps if x.get("tps"))
    dec_n = sum(x["out"] for x in steps if x.get("tps"))
    pre = sum((x["in"] - x["cached"]) / x["pp"] for x in steps if x.get("pp"))
    pre_n = sum(x["in"] - x["cached"] for x in steps if x.get("pp"))
    return {"tps": round(dec_n / dec, 1) if dec > 0 else None, "pp": round(pre_n / pre) if pre > 0 else None,
            "out": sum(x["out"] for x in steps), "in": steps[-1]["in"] if steps else 0,
            "cached": steps[-1]["cached"] if steps else 0, "wait": round(sum(x.get("wait") or 0 for x in steps), 1),
            "steps": len(steps)}


def said_done(text):
    """Its answer to that nudge was only that it is done (or nothing)."""
    t = (text or "").strip()
    return not t or (len(t) <= 40 and re.search(r"\bdone\b", t, re.I) is not None)


# DeepSeek V4.1 writes its calls in its own markup (DSML), and the engine turns them into tool calls only when they are
# exactly in its format: a newline more or less, or words after them, and the whole block comes back as the reply's
# words, so the answer ended on a call that never ran.
# It also writes it with plain bars ("<|DSML|"), or falls back on its older models' tool tokens ("<｜tool▁call▁begin｜>")
# or another family's forms (<tool_call>{json}</tool_call>, <invoke name=..>, <function=..>, a JSON object naming the
# tool): none of those reach the engine as calls, so the reply ended on a call-shaped block and the turn just stopped
MARKUP = "<｜"  # where DeepSeek's own markup starts (a full-width bar): the page never shows words from here
_CALL_MARK = re.compile(r"<[ \t]*[｜|][ \t]*(?:DSML|tool[▁_ ])", re.I)
_BARS = re.compile(r"<(/?)[ \t]*[｜|][ \t]*DSML[ \t]*[｜|][ \t]*", re.I)  # plain or spaced bars, as the engine's own markup
_INVOKE = re.compile(r"<｜DSML｜\s*invoke\s+name=\"([^\"]+)\"\s*>(.*?)(?=</｜DSML｜\s*invoke|<｜DSML｜\s*invoke\s|\Z)",
                     re.S)
_PARAM = re.compile(r"<｜DSML｜\s*parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?\s*>(.*?)"
                    r"</｜DSML｜\s*parameter\s*>", re.S)
_TOK = r"<[ \t]*[｜|][ \t]*tool[▁_ ]{}[｜|][ \t]*>"  # DeepSeek V3's tool tokens, with either bar
_V3_CALL = re.compile(_TOK.format(r"call[▁_ ]begin") + r"(.*?)(?=" + _TOK.format(r"call[▁_ ]end") + "|"
                      + _TOK.format(r"call[▁_ ]begin") + r"|" + _TOK.format(r"calls[▁_ ]end") + r"|\Z)", re.S | re.I)
_V3_SEP = re.compile(_TOK.format("sep"), re.I)
_FENCED = re.compile(r"^\s*```[\w-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)
# where a streamed reply's words wait until it ends (calls_in_text decides): call markup of any of those forms; a JSON
# call is shown as it comes and taken back if it was one ("retract")
_HOLD = re.compile(r"<[ \t]*/?[ \t]*[｜|]|<(?:tool_calls?\b|function_calls\b|function=|invoke\s)", re.I)
_OTHER = re.compile(r"(?m)^[ \t]*(?:<tool_calls?>|<function_calls>|<invoke\s+name=|<function=)", re.I)  # a tag that
# starts a line (gate review: "models emit `<invoke name=...>` blocks" in prose was cut as a garbled call)


def held_from(text):
    """Where a streamed reply's words stop being shown for now: the start of call markup (to its end, when
    calls_in_text decides), or a tag still being written at its very end that could become one."""
    m = _HOLD.search(text)
    if m:
        return m.start()
    lt = text.rfind("<", max(0, len(text) - 24))
    return lt if lt >= 0 and ">" not in text[lt:] and "\n" not in text[lt:] else len(text)


def _args(raw):
    """A call's arguments as a dict: a JSON object or the JSON text of one (None when it is neither)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def _json_calls(obj):
    """[(name, args)] from a JSON call or a list of them (OpenAI's, Hermes', most models'), or None for other JSON: a
    name and arguments that are an object, and no keys a call doesn't have."""
    out = []
    for o in obj if isinstance(obj, list) and obj else [obj]:
        if not isinstance(o, dict):
            return None
        f = o.get("function")
        if isinstance(f, dict) and set(o) <= {"function", "type", "id"}:
            o = f
        keys = set(o) - {"type", "id"}
        name = next((o[k] for k in ("name", "tool", "tool_name", "function") if isinstance(o.get(k), str)), None)
        arg = next((k for k in ("arguments", "parameters", "args", "input") if k in o), None)
        args = _args(o[arg]) if arg else {}
        if not name or not arg or args is None or len(keys) != 2 or not keys <= {"name", "tool", "tool_name", "function", arg}:
            return None
        out.append((name, args))
    return out


def _xml_calls(s):
    """[(name, args)] from <invoke name="X"><parameter name="k">v</parameter></invoke> or Qwen's <function=X>
    <parameter=k>v</parameter></function>, back to back and nothing else, or None."""
    out = []
    pat = re.compile(r"\s*<invoke\s+name=\"([^\"]+)\"\s*>(.*?)</invoke>\s*|\s*<function=([^>\s]+)\s*>(.*?)</function>\s*", re.S)
    at = 0
    while at < len(s):
        m = pat.match(s, at)
        if not m:
            return None
        args = {}
        if m.group(1) is not None:
            for k, string, v in re.findall(r"<parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?\s*>(.*?)</parameter>",
                                           m.group(2), re.S):
                args[k] = v
                if string == "false":
                    try:
                        args[k] = json.loads(v)
                    except ValueError:
                        pass
        else:
            for k, v in re.findall(r"<parameter=([^>\s]+)\s*>\n?(.*?)\n?</parameter>", m.group(4), re.S):
                args[k] = v
        out.append(((m.group(1) or m.group(3)).strip(), args))
        at = m.end()
    return out or None


def _block_calls(s):
    """[(name, args)] when `s` is a call block and nothing else (one of the other families' forms), else None."""
    s = s.strip()
    f = _FENCED.match(s)
    if f:
        s = f.group(1).strip()
    for _ in range(2):  # a wrapper: <function_calls> ... </function_calls>, <tool_calls> ... </tool_calls>
        w = re.fullmatch(r"<(function_calls|tool_calls|calls)>\s*(.*?)\s*(?:</\1>)?", s, re.S)
        s = w.group(2) if w else s
    if s.startswith("<tool_call>"):
        out = []
        for one in re.split(r"\s*</tool_call>\s*", s):
            if not one.strip():
                continue
            if not one.startswith("<tool_call>"):
                return None
            got = _block_calls(one[len("<tool_call>"):])
            if not got:
                return None
            out += got
        return out or None
    if s.startswith("<"):
        return _xml_calls(s)
    if s[:1] in "{[":
        try:
            return _json_calls(json.loads(s))
        except ValueError:
            return None
    return None


def _end_block(t):
    """(where, [(name, args)]) of a call block in another family's form that ends the reply, or (None, None)."""
    tail = t[-20_000:]
    starts = sorted({m.start() for m in re.finditer(r"(?m)^[ \t]*(?:```|[{\[])|<tool_calls?>|<function_calls>|<invoke\s|<function=",
                                                    tail)})
    for at in starts:
        got = _block_calls(tail[at:])
        if got:
            return len(t) - len(tail) + at, got
    return None, None


def stray_call(text, names):
    """True when a reply ends with a well-formed call in another family's form that doesn't run: to a tool it wasn't
    offered ("bash", "execute_command"), or after words of its own (perhaps an example it quoted; gate review: a
    quoted example ran). Nothing ran, and the turn would end on a call-shaped block: the loop sends it back once, its
    words standing."""
    t = text or ""
    at, got = _end_block(t)
    return bool(got) and (not all(n in names for n, _ in got) or bool(t[:at].strip()))


def _other_calls(t, names):
    """(words, calls, garbled) for a call in another family's form that IS the whole reply and names offered tools (with
    words before it, it may be an example it quoted: stray_call sends it back instead); one that names an offered tool
    but can't be read is garbled. None when there is none (an example, a tool that isn't offered, ordinary JSON or
    XML)."""
    at, got = _end_block(t)
    if got:
        if all(n in names for n, _ in got) and not t[:at].strip():
            return "", [{"name": n, "args": json.dumps(a, ensure_ascii=False)} for n, a in got], False
        return None
    tail = t[-20_000:]
    off = len(t) - len(tail)
    # a call to one of its tools it couldn't write out right: the reply ends right after that markup closes, or inside a
    # short one (an unclosed tag that prose goes on after is an example)
    opens = [m for m in _OTHER.finditer(tail) if len(tail) - m.start() <= 4000]
    if opens:
        after = tail[opens[0].start():]
        closes = list(re.finditer(r"</(?:tool_calls?|invoke|function|function_calls)>", after))
        ends_on_it = (not after[closes[-1].end():].strip(" \t\r\n`") if closes
                      else len(after) <= 1500 and "\n\n" not in after.strip())
        if ends_on_it and any(re.search(r"(?:name=\"|\"name\"\s*:\s*\"|<function=)" + re.escape(n) + r"\b", after) for n in names):
            return t[:off + opens[0].start()].rstrip(), [], True
    return None


def _v3_calls(body):
    """[{name, args}] from DeepSeek V3's tool tokens: function<sep>NAME ```json {..}``` or NAME<sep>{..}."""
    calls = []
    for one in _V3_CALL.findall(body):
        parts = _V3_SEP.split(one, 1)
        if len(parts) != 2:
            continue
        left, right = parts[0].strip(), parts[1].strip()
        if left == "function" or not left:
            name, _, right = right.partition("\n")
        else:
            name = left
        f = _FENCED.match(right)
        args = _args((f.group(1) if f else right).strip())
        if name.strip() and args is not None:
            calls.append({"name": name.strip(), "args": json.dumps(args, ensure_ascii=False)})
    return calls


def calls_in_text(text, names=()):
    """A reply's calls written out as words: (its words before the markup, [{name, args (JSON text)}], garbled: call
    markup that holds no call it can read). DeepSeek's own markup (DSML with either bar, V3's tool tokens) counts
    wherever it starts; another family's form only when it is the whole reply and every call names a tool offered
    (`names`), so an example in an answer never runs. Words without any of them come back as they are."""
    t = text or ""
    m = _CALL_MARK.search(t)
    if m:
        at, body = m.start(), _BARS.sub(lambda b: f"<{b.group(1)}｜DSML｜ ", t[m.start():])
        calls = []
        for name, inv in _INVOKE.findall(body):
            args = {}
            for key, string, value in _PARAM.findall(inv):
                if string == "false":  # a number, a list, true/false: as JSON (the engine's own reading)
                    try:
                        args[key] = json.loads(value)
                        continue
                    except ValueError:
                        pass
                args[key] = value
            calls.append({"name": name.strip(), "args": json.dumps(args, ensure_ascii=False)})
        calls = calls or _v3_calls(body)
        return t[:at].rstrip(), calls, not calls
    got = _other_calls(t, set(names)) if names else None
    return got or (t, [], False)


def call_shape(text):
    """What kind of call markup a reply holds, for the app's log (never its words): "dsml", "v3", "tool_call",
    "xml", "json" or None."""
    t = (text or "")[-20_000:]
    if re.search(r"<[ \t]*[｜|][ \t]*DSML", t, re.I):
        return "dsml"
    if _CALL_MARK.search(t):
        return "v3"
    if "<tool_call>" in t:
        return "tool_call"
    if re.search(r"<invoke\s+name=|<function=|<function_calls>", t):
        return "xml"
    if re.search(r"\"(?:name|tool)\"\s*:\s*\"[^\"]+\"\s*,\s*\"(?:arguments|parameters|args|input)\"\s*:", t):
        return "json"
    return None


def result(text, line, status="ok", decision="ran", exit=None, secs=None, out="", changed=None, refused=None, **audit):
    """One call's outcome. text: what the model reads (the toolset wraps untrusted data); line: the thread's one line
    ("Ran: npm test · exit 0 · 4.2 s"); out: the output for the page (cut to OUT_PAGE); changed: files the call
    changed (None: couldn't tell); refused: network refusals ("the filter refused 192.168.1.1:443: ..."); any other
    keyword goes to the audit line (a Private chat's audit keeps only the metadata keys, see audit())."""
    if status not in STATUSES:
        raise ValueError(f"status: {', '.join(STATUSES)}")
    if decision not in DECISIONS:
        raise ValueError(f"decision: {', '.join(DECISIONS)}")
    return {"text": str(text), "line": str(line), "status": status, "decision": decision, "exit": exit,
            "secs": None if secs is None else round(float(secs), 2), "out": str(out or "")[-OUT_PAGE:],
            "changed": changed, "refused": list(refused or []), "audit": audit}


def call_key(name, args):
    """A call as the repeat check compares it: the tool and its arguments, case and spacing aside. A repeat counts only
    when its result is also the same, so running the tests again after an edit is never one."""
    return f"{name} " + " ".join(json.dumps(args or {}, sort_keys=True, ensure_ascii=False).lower().split())


# the web tools' leak guard (agents_web.exfil), as the chat uses it (a job search's "Search refused" over and over):
# what counts as the user's private text is what the turn read from the user's files (and, with Full access, its
# commands' output), never a public page (a page's job title, "chief operations officer", is exactly the guard's 24
# characters); words the user wrote anywhere in the chat may be searched for; an address from a search result or a
# page's links skips the copy check (written by a third party, Agents' rule)
OWNER_MESSAGES = 20


def owners_words(chat):
    """What the user wrote in this chat (the user's last OWNER_MESSAGES messages): the guard's `question`, never
    counted as copied."""
    msgs = [m for m in (chat or {}).get("messages") or [] if m.get("role") == "user" and not m.get("hidden")]
    return "\n".join(str(m.get("content") or "") for m in msgs[-OWNER_MESSAGES:])


def guard_words(d, what="search"):
    """Why the guard stopped a search or an address, said to the model so it can rephrase rather than try again."""
    rule = getattr(d, "rule", "")
    if rule == "notes_copy":
        return (f"the {what} repeats words from the user's files that you read in this turn. Use your own general words "
                "(a job title, a place, a topic) instead of copying phrases from their files")
    return ({"email": f"the {what} contains an email address", "phone": f"the {what} contains a phone number",
             "encoded_value": f"the {what} carries a long encoded value", "long_query": f"the {what} is too long",
             "long_params": "the address has too many long parameters"}.get(rule)
            or str(getattr(d, "reason", "") or "it isn't allowed").rstrip("."))


def links_of(page):
    """The addresses a page links to (agents_web's {"text", "url"} links)."""
    return {str(x.get("url") or "") for x in (page or {}).get("links") or [] if isinstance(x, dict)} - {""}


def coerce(args, spec):
    """A call's arguments read by its tool's schema: "true"/"false" as booleans, numbers written as text as numbers,
    JSON text as its array or object. DeepSeek marks each value as text or JSON, and one marked as text reaches the
    tool as a string whatever the schema says (review: a "false" pressed Enter in the browser, a "true" replace_all
    replaced once)."""
    fn = (spec or {}).get("function") or spec or {}
    props = (fn.get("parameters") or {}).get("properties") or {}
    out = dict(args)
    for k, v in args.items():
        want = (props.get(k) or {}).get("type")
        if not isinstance(v, str) or want in (None, "string"):
            continue
        s = v.strip()
        try:
            if want == "boolean" and s.lower() in ("true", "false"):
                out[k] = s.lower() == "true"
            elif want == "integer" and re.fullmatch(r"-?\d+", s):
                out[k] = int(s)
            elif want == "number" and re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", s):
                out[k] = float(s)
            elif want in ("array", "object") and s[:1] in "[{":
                j = json.loads(s)
                if isinstance(j, list if want == "array" else dict):
                    out[k] = j
        except ValueError:
            pass
    return out

def level_of(chat):
    p = (chat or {}).get("perm")
    return p if p in LEVELS else DEFAULT


def opts_of(chat):
    try:
        o = json.loads((chat or {}).get("perm_opts") or "{}")
    except (TypeError, ValueError):
        o = {}
    return o if isinstance(o, dict) else {}


def _plugin(name):
    try:
        return __import__(name)
    except ImportError:
        return None


def toolsets_for(ctx):
    """The toolsets that apply to this turn, in PLUGINS order (each module decides from ctx)."""
    out = []
    for name in PLUGINS:
        mod = _plugin(name)
        t = mod.toolset(ctx) if mod is not None and hasattr(mod, "toolset") else None
        if t is not None:
            out.append(t)
    return out if not all(getattr(t, "companion", False) for t in out) else []  # a plan alone is no reason for tools


def offered(level, model_id):
    """(offered, why not) for a level and the chat's model: Full asks chat_full (Kyojin: not offered, J3); the
    container levels ask chat_box (built yet?)."""
    if level == "none":
        return True, None
    if level == "direct":  # a computer to run on, else not offered
        mod = _plugin("chat_direct")
        return mod.offered(model_id) if mod is not None else (False, "Not built yet")
    mod = _plugin("chat_full" if level == "full" else "chat_box")
    if mod is None or not hasattr(mod, "offered"):
        return False, "Not built yet"
    return mod.offered(level, model_id) if level != "full" else mod.offered(model_id)


def check_perm(chat, body, model_id, **full):
    """The PUT's perm change, checked: (perm, perm_opts dict) to store, or ValueError with plain words. Full asks
    chat_full.check_turn_on (confirm: true, its scope; J4: no passcode), with the route's `full` keywords (fake, store)."""
    want = body.get("perm")
    if want not in LEVELS:
        raise ValueError("perm: " + ", ".join(LEVELS))
    if want in ("full", "direct") and chat and chat.get("private"):  # before Full makes its folder, named for the chat (P2)
        raise ValueError(PRIVATE_NO_FULL)
    ok, why = offered(want, model_id)
    if not ok:
        raise ValueError(f"{WORDS[want]['name']}: {why}")
    if want == "direct":
        return want, _plugin("chat_direct").check_turn_on(body, chat, model_id, **full)
    if want == "full":
        return want, _plugin("chat_full").check_turn_on(body, chat, model_id, **full)
    return want, {}


def view(chat, model_id):
    """The chip's sheet for a chat (or a new chat: chat None): each level with its words, offered or why not, and Full's
    own part from chat_full.view(chat, model_id) ({"confirm": [lines], "scopes": [{"id", "name", "desc", "residual"}],
    "folder"}) when it is built."""
    cur = level_of(chat)
    priv = bool(chat and chat.get("private"))
    rows = []
    for lv in LEVELS:
        ok, why = (False, "Not in a Private chat: its files would sit unsealed on this Mac") if priv and lv in ("full", "direct") else offered(lv, model_id)
        rows.append(dict(WORDS[lv], id=lv, offered=ok, why=why, on=lv == cur))
    full = _plugin("chat_full")
    return {"levels": rows, "level": cur, "opts": opts_of(chat), "private": priv, "computers": _plugin("chat_direct").targets(),
            "full": full.view(chat, model_id) if full is not None and hasattr(full, "view") and not priv else None}


# ---------------------------------------------------------------- asks (taps)
class Asks:
    """Pending taps, by chat: the loop waits on one; the answer route (POST /studio/api/chats/<id>/answer) or Stop ends
    it. One per chat at a time (a turn asks one call at a time)."""

    def __init__(self):
        self.mu = threading.Lock()
        self.pending = {}  # chat id -> {"id", "req", "ev", "decision"}

    def wait(self, chat_id, req, stop, timeout_s=ASK_TIMEOUT_S, on_open=None, auto_allow=None, on_allow_all=None):
        """Open an ask and block until it's answered, Stop, or timeout_s. -> ("allow" | "deny", ask id, how) with how
        in "owner", "stop", "timeout"."""
        a = {"id": "a" + secrets.token_hex(6), "req": dict(req), "ev": threading.Event(), "decision": None,
             "on_allow_all": on_allow_all, "how": "owner"}
        with self.mu:
            if stop.is_set():
                return "deny", a["id"], "stop"
            if auto_allow and auto_allow():
                return "allow", a["id"], "chat_auto"
            self.pending[chat_id] = a
        try:
            if on_open:
                on_open(a["id"])
            t0 = time.time()
            while not a["ev"].is_set():
                if stop.is_set():
                    return "deny", a["id"], "stop"
                if time.time() - t0 > timeout_s:
                    return "deny", a["id"], "timeout"
                a["ev"].wait(0.25)
            if stop.is_set():
                return "deny", a["id"], "stop"
            return a["decision"], a["id"], a["how"]
        finally:
            with self.mu:
                if self.pending.get(chat_id) is a:
                    del self.pending[chat_id]

    def answer(self, chat_id, ask_id, decision):
        """the user's tap. False when there is no such open ask (answered already, timed out, another one)."""
        if decision not in ("allow", "allow_all", "deny"):
            raise ValueError("decision: allow, allow_all or deny")
        with self.mu:
            a = self.pending.get(chat_id)
            if not a or a["id"] != ask_id or a["ev"].is_set():
                return False
            if decision == "allow_all":
                if not a["on_allow_all"]:
                    raise ValueError("This chat does not support Allow all")
                a["on_allow_all"]()
                a["how"] = "chat_all"
            a["decision"] = "allow" if decision == "allow_all" else decision
            a["ev"].set()
            return True

    def open(self, chat_id):
        with self.mu:
            a = self.pending.get(chat_id)
            return {"id": a["id"], **a["req"]} if a and not a["ev"].is_set() else None


ASKS = Asks()  # the app's one broker: the tool loop waits on it, the answer route answers it


# ---------------------------------------------------------------- the audit file
AUDIT_META = ("t", "chat", "msg", "level", "model", "tool", "status", "decision", "exit", "secs", "how", "repeat")
AUDIT_ARGS = 2048  # a call's arguments in the audit, at most (a write's whole content is not kept)


def audit(store, chat_id, rec, private=False):
    """Append one line to <store>/chat-tools/<chat>.jsonl (600, append-only). A Private chat's line keeps only
    AUDIT_META (no arguments, output, paths or refusals). Never raises: the turn goes on."""
    try:
        d = store.path("chat-tools")
        os.makedirs(d, mode=0o700, exist_ok=True)
        line = {k: v for k, v in rec.items() if not private or k in AUDIT_META}
        if "args" in line and len(json.dumps(line["args"], ensure_ascii=False)) > AUDIT_ARGS:
            line["args"] = {"_cut": json.dumps(line["args"], ensure_ascii=False)[:AUDIT_ARGS]}
        line.setdefault("t", round(time.time(), 3))
        fd = os.open(os.path.join(d, f"{chat_id}.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def remove_audit(store, chat_id):
    try:
        os.unlink(store.path("chat-tools", f"{chat_id}.jsonl"))
    except OSError:
        pass


def remove_chat_files(store, chat_id):
    """A deleted chat's tool files on this Mac: its audit file and its web tools' folder (fetched pages, search
    results: <store>/chat-tools/web/<chat id>). Private chats too. Never raises."""
    import shutil
    remove_audit(store, chat_id)
    if len(str(chat_id)) == 16 and all(c in "0123456789abcdef" for c in str(chat_id)):
        shutil.rmtree(store.path("chat-tools", "web", str(chat_id)), ignore_errors=True)
