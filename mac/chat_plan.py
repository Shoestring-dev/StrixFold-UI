"""A plan the user can watch (next to Claude Code; its to-do list shows a long task's steps and which are done): update_plan keeps this answer's steps, and the
page shows them as a checklist, the newest whole and the earlier ones folded to their one line. Offered with the other
tools (any level but none), to a team's agents as well."""
import chat_tools as T

STATES = ("pending", "in_progress", "done")
MAX_STEPS, STEP_CHARS = 20, 160
SPEC = {"type": "function", "function": {
    "name": "update_plan",
    "description": ("Keep a short checklist of this task's steps that the user sees in the chat. For work of three or more steps, "
                    "call it first with every step pending, then again as each step starts (in_progress) and is done, "
                    "sending the whole list each time. Skip it for a quick question."),
    "parameters": {"type": "object", "properties": {"steps": {"type": "array", "items": {
        "type": "object", "properties": {"step": {"type": "string"}, "status": {"type": "string", "enum": list(STATES)}},
        "required": ["step", "status"]}}}, "required": ["steps"]}}}


def toolset(ctx):
    if (ctx.chat or {}).get("cousteau") or ctx.level == "none":  # this turn's level (Send without tools: none)
        return None
    return Plan()


def clean(steps):
    """The plan as given, checked: [{"step", "status"}], at most MAX_STEPS, or ValueError in words for the model."""
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a list of {step, status}")
    out = []
    for s in steps[:MAX_STEPS]:
        if not isinstance(s, dict) or not str(s.get("step") or "").strip():
            raise ValueError("each step needs its words in step")
        st = str(s.get("status") or "pending")
        out.append({"step": " ".join(str(s["step"]).split())[:STEP_CHARS], "status": st if st in STATES else "pending"})
    return out


class Plan(T.Toolset):
    level = "none"
    companion = True  # offered only beside other tools (chat_tools.toolsets_for): alone it would put a plain chat on the tool path
    repeats_ok = True  # the same plan sent again is no loop (halo_chat's repeat check leaves it out)

    def specs(self):
        return [SPEC]

    def call(self, name, args, ctx):
        try:
            steps = clean((args or {}).get("steps"))
        except ValueError as e:
            return T.result(f"Not updated: {e}.", "Plan not updated", "failed", "refused")
        done = sum(s["status"] == "done" for s in steps)
        now = next((s["step"] for s in steps if s["status"] == "in_progress"), None)
        line = f"Plan: {done} of {len(steps)} done" + (f" · {now}" if now else "")
        text = "Plan updated (the user sees it):\n" + "\n".join(f"- [{s['status']}] {s['step']}" for s in steps)
        return T.result(text, line, plan=steps)
