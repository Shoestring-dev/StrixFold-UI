"""What a project's agents may do: its lead when the user sends it a message, and the helpers the lead starts (the
"Sends can" chip and the Agent rules panel). Rules are data, not code: the DEFAULTS below, with a project's own
`lead_rules` from the Command Centre's registry on top (check() keeps only known keys in range), give one plain dict
that the runner (mac/lead_tools.py, through Agents' runner) reads. The panel (Agent rules, in a project's menu and its
full-screen More) writes the same keys.

The mode is the project's send_mode, the same one its Claude Code and Codex sends use:
  plan               Plan only: read-only tools in the project's folders (list, read, search, git status/log/diff)
  acceptEdits        Edit files: adds write and edit, inside the project's folders only
  bypassPermissions  Run anything: adds running commands in the project's folder (sandboxed)
  default            Ask for each step: the local lead has no asks yet, so it reads only, as Plan only
The rules' tool switches narrow the mode, never widen it (web is its own switch, off by default).

Keys: model, groups (read / edit / run / web), folders (beyond the project's own), network (Run anything only, through
the filter), net_ports (the ports the filter lets commands reach: 80 and 443),
budget (minutes and tokens; tool calls), max_at_once, pause_for_owner, helpers (frontier / mixed / local), helper_cap
(1 to 5), depth (1: a helper can't start helpers).
"""
import copy

TOOL_GROUPS = ("read", "edit", "run", "web")
MODES = {
    "plan": {"name": "Plan only", "groups": ("read",)},
    "default": {"name": "Ask for each step", "groups": ("read",)},
    "acceptEdits": {"name": "Edit files", "groups": ("read", "edit")},
    "bypassPermissions": {"name": "Run anything", "groups": ("read", "edit", "run")},
}
MODE_RANK = {"plan": 0, "default": 0, "acceptEdits": 1, "bypassPermissions": 2}
DEFAULT_MODE = "acceptEdits"  # the project's send_mode when it has none (as Claude Code sends)
# the tools in each group (lead_tools.SPECS names them)
GROUP_TOOLS = {"read": ("list_dir", "read_file", "search", "git", "read_pdf"), "edit": ("write_file", "edit_file", "render_pdf"),
               "run": ("run_command",),
               "web": ("web_search", "web_read")}
# the Model rule: what the lead's turns and its GPU helpers must run on. Any takes whichever accepted chat
# model is loaded; the others wait for theirs, and the column offers Switch the boxes (the user's click; never by
# itself). Kyojin is never offered for tool runs.
MODELS = {"loaded": "Any", "dsv41": "DeepSeek", "dsv41-unc": "DeepSeek uncensored", "qwen": "Qwen 3.8 agents"}
MODEL_NOTES = {"loaded": "Whichever chat model is on the boxes (the default).", "dsv41": "DeepSeek V4.1, official weights.",
               "dsv41-unc": "DeepSeek V4.1 with uncensored weights.", "qwen": "Qwen 3.8, up to 16 at once."}
HELPERS = {"frontier": "Frontier only", "mixed": "Mixed", "local": "Local only"}
HELPER_NOTES = {"frontier": "Claude or ChatGPT (Codex) sessions, on the project's chosen accounts.",
                "mixed": "The lead picks per task: local for bulk reading, summarising and private message data (DeepSeek Harness on Toolbox for self-contained code); frontier for hard code or reasoning.",
                "local": "Your GPU agents only: DeepSeek or Qwen on the boxes, or DeepSeek Harness on Toolbox."}
DEFAULTS = {
    "model": "loaded",            # the chat model that is loaded (official or uncensored DeepSeek, Qwen 3.8); never Kyojin
    "groups": ["read", "edit", "run"],  # the switches: each also needs the mode; web is off unless the user turns it on
    "folders": [],                # folders beyond the project's own (under ~/Code), with the same rules
    "network": True,              # commands' network in Run anything: through the filter (lead_proxy), never the user's home network, the tailnet or this Mac
    "net_ports": [80, 443],       # the ports the filter lets commands reach
    "budget": {"max_active_minutes": 10, "max_wall_minutes": 30, "max_tool_calls": 40, "max_model_tokens": 300000},
    "max_steps": 24,              # model requests in one turn
    "max_at_once": 1,             # lead turns running at once for one project
    "pause_for_owner": True,        # the user first: the agents wait while the user is using the boxes
    "run_timeout_s": 120,         # one command, at most
    "helpers": "mixed",           # what the lead may start: frontier / mixed / local
    "helper_cap": 5,              # helpers running at once for one project
    "depth": 1,                   # a helper can't start helpers
}
LIMITS = {"max_active_minutes": (1, 60), "max_wall_minutes": (1, 240), "max_tool_calls": (1, 200),
          "max_model_tokens": (1000, 2_000_000)}
INTS = {"max_steps": (1, 60), "max_at_once": (1, 4), "run_timeout_s": (5, 600), "helper_cap": (1, 5), "depth": (1, 1)}


def check(o):
    """A project's own rules, checked: (the clean rules, problems in plain words). Unknown keys are refused."""
    clean, errs = {}, []
    if o is None:
        return clean, errs
    if not isinstance(o, dict):
        return clean, ["the rules are a set of key: value"]
    extra = sorted(set(o) - set(DEFAULTS))
    if extra:
        errs.append("unknown: " + ", ".join(extra))
    if "model" in o:
        if o["model"] in MODELS:
            clean["model"] = o["model"]
        else:
            errs.append("model: " + ", ".join(MODELS))
    if "groups" in o:
        if isinstance(o["groups"], list) and all(g in TOOL_GROUPS for g in o["groups"]):
            clean["groups"] = [g for g in TOOL_GROUPS if g in o["groups"]]
        else:
            errs.append("tools: read, edit, run, web")
    if "folders" in o:
        if isinstance(o["folders"], list) and len(o["folders"]) <= 8 and all(isinstance(f, str) and 0 < len(f) <= 300 for f in o["folders"]):
            clean["folders"] = list(dict.fromkeys(f.strip() for f in o["folders"] if f.strip()))
        else:
            errs.append("folders: up to 8 folders under ~/Code")
    for k in ("network", "pause_for_owner"):
        if k in o:
            if isinstance(o[k], bool):
                clean[k] = o[k]
            else:
                errs.append(f"{k}: on or off")
    if "net_ports" in o:
        v = o["net_ports"]
        if isinstance(v, list) and 0 < len(v) <= 8 and all(isinstance(x, int) and not isinstance(x, bool) and 0 < x < 65536 for x in v):
            clean["net_ports"] = sorted(set(v))
        else:
            errs.append("net_ports: 1 to 8 port numbers")
    if "budget" in o:
        b = o["budget"]
        if not isinstance(b, dict) or set(b) - set(LIMITS):
            errs.append("budget: " + ", ".join(LIMITS))
        else:
            good = {}
            for k, v in b.items():
                lo, hi = LIMITS[k]
                if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                    good[k] = v
                else:
                    errs.append(f"budget.{k}: a whole number {lo:,} to {hi:,}")
            if good:
                clean["budget"] = good
    for k, (lo, hi) in INTS.items():
        if k in o:
            v = o[k]
            if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                clean[k] = v
            else:
                errs.append(f"{k}: a whole number {lo} to {hi}")
    if "helpers" in o:
        if o["helpers"] in HELPERS:
            clean["helpers"] = o["helpers"]
        else:
            errs.append("helpers: frontier, mixed or local")
    return clean, errs


def merge(overrides=None):
    """DEFAULTS with a project's own rules on top (what check() keeps; the rest is dropped)."""
    out = copy.deepcopy(DEFAULTS)
    clean, _ = check(overrides if isinstance(overrides, dict) else None)
    budget = clean.pop("budget", {})
    out.update(clean)
    out["budget"].update(budget)
    return out


def helpers_for(rules, cls):
    """What the project's class allows of the rules' helpers: Local only forces local (why, in plain words)."""
    if cls == "local":
        return "local", "This project is Local only, so its helpers run on your boxes."
    return rules["helpers"], None


def rules_for(mode, overrides=None, cls=None):
    """The rules for one turn: the mode's tool groups (narrowed by the switches, never widened; web by its own switch),
    the tools they allow, the helpers the class allows, and everything else from merge()."""
    m = mode if mode in MODES else DEFAULT_MODE
    r = merge(overrides)
    on = set(r["groups"])
    groups = [g for g in MODES[m]["groups"] if g in on] + (["web"] if "web" in on else [])
    helpers, why = helpers_for(r, cls)
    r.update(mode=m, mode_name=MODES[m]["name"], switches=r["groups"], groups=groups,
             tools=[t for g in groups for t in GROUP_TOOLS[g]], helpers=helpers, helpers_why=why,
             network=bool(r["network"]) and "run" in groups)
    return r


WRITES_FILES = ("fill_pdf_form", "copy_attachment")  # lead_files' writes: offered with write_file, so Edit files


def needs_group(tool):
    if tool in WRITES_FILES:
        return "edit"
    return next((g for g, ts in GROUP_TOOLS.items() if tool in ts), None)


def mode_at_most(want, lead_mode):
    """A helper's mode: what the lead asked for, never above the lead's own."""
    w = want if want in MODES else lead_mode
    return w if MODE_RANK[w] <= MODE_RANK.get(lead_mode, 0) else lead_mode
