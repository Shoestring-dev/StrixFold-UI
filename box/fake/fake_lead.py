"""A project lead's stand-in model for its tool turns (mac/lead_tools.py): it does what the user's message spells out, one
line per tool call, in order, then answers. Lines it reads:
  /list PATH  /read PATH  /search TEXT  /git status|log|diff [ARGS]  /write PATH TEXT  /edit PATH OLD => NEW
  /run COMMAND  /web QUERY  /page URL  /loop (list_dir forever: budgets, steps and Stop)  /pdf PATH [OUT]  /readpdf PATH
  /call TOOL {JSON} (any tool by name, with its arguments)
  /said LINE (writes "(I used <tool>.)" for LINE's tool instead of calling it, until the Command Centre says no tool ran; then calls it)
  /sayonly TOOL (writes "(I used TOOL.)" every time, never calling it)
A message without such lines gets a short plain answer. fake_research.answer hands it the lead's requests."""
import json
import re

MARK = "You run on the user's local model with tools."


def wants(body):
    msgs = body.get("messages") or []
    return bool(msgs) and MARK in str(msgs[0].get("content") or "")


def _call(name, args, n):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"l{n}", "type": "function",
                                                                 "function": {"name": name, "arguments": json.dumps(args)}}]}


def directive(line):
    m = re.match(r"^/(\w+)\s*(.*)$", line.strip())
    if not m:
        return None
    verb, rest = m.group(1), m.group(2).strip()
    if verb == "list":
        return "list_dir", {"path": rest or "."}
    if verb == "read":
        return "read_file", {"path": rest}
    if verb == "search":
        return "search", {"pattern": rest}
    if verb == "git":
        w = rest.split()
        return "git", {"command": w[0] if w else "status", "args": w[1:]}
    if verb == "write":
        path, _, text = rest.partition(" ")
        return "write_file", {"path": path, "content": text}
    if verb == "edit":
        path, _, ch = rest.partition(" ")
        old, _, new = ch.partition(" => ")
        return "edit_file", {"path": path, "old": old, "new": new}
    if verb == "run":
        return "run_command", {"command": rest}
    if verb == "loop":
        return "list_dir", {"path": "."}
    if verb == "web":
        return "web_search", {"query": rest}
    if verb == "spawn":  # /spawn KIND MODE: TASK (the task's own /lines are the helper's)
        head, _, task = rest.partition(":")
        w = head.split()
        return "spawn_agents", {"agents": [{"task": task.strip().replace(" ;; ", "\n"), "kind": w[0] if w else "local",
                                           "mode": w[1] if len(w) > 1 else "plan", "why": "the fake picks"}]}
    if verb == "websee":  # a search with the first line the last tool read (a model that leaks what it read)
        return "web_search", {"query": "$LAST"}
    if verb == "page":
        return "web_read", {"url": rest}
    if verb == "pdf":
        w = rest.split()
        return "render_pdf", dict({"path": w[0] if w else ""}, **({"out": w[1]} if len(w) > 1 else {}))
    if verb == "readpdf":
        return "read_pdf", {"path": rest}
    if verb == "call":
        name, _, args = rest.partition(" ")
        return name, json.loads(args or "{}")
    return None


def answer(body):
    msgs = body.get("messages") or []
    i = max((k for k, m in enumerate(msgs) if m.get("role") == "user" and not str(m.get("content") or "").startswith(
        ("Result of ", "You have used the steps", "[Command Centre: no tool ran"))), default=None)
    owner = str(msgs[i]["content"]) if i is not None else ""
    done = [m for m in msgs[(i or 0) + 1:] if m.get("role") in ("user", "tool") and str(m.get("content") or "").startswith("Result of ")]
    noted = any(m.get("role") == "user" and str(m.get("content") or "").startswith("[Command Centre: no tool ran") for m in msgs[(i or 0) + 1:])
    for x in owner.splitlines():  # a model that says it used a tool instead of calling it
        if x.strip().startswith("/sayonly "):
            return {"role": "assistant", "content": f"Rendering it now.\n(I used {x.strip()[9:].strip()}.)"}
        if x.strip().startswith("/said ") and not noted:
            d = directive(x.strip()[6:])
            return {"role": "assistant", "content": f"Rendering it now.\n(I used {d[0] if d else 'a tool'}.)"}
    owner = "\n".join(x.strip()[6:] if x.strip().startswith("/said ") else x for x in owner.splitlines())
    todo = []
    for d in (directive(x) for x in owner.splitlines()):  # consecutive /spawn lines: one spawn_agents call
        if d and d[0] == "spawn_agents" and todo and todo[-1][0] == "spawn_agents":
            todo[-1][1]["agents"] += d[1]["agents"]
        elif d:
            todo.append(d)
    tools = {t.get("function", {}).get("name") for t in body.get("tools") or []}
    if any(x.strip() == "/loop" for x in owner.splitlines()) and tools:
        return _call("list_dir", {"path": "."}, len(done))
    if todo and len(done) < len(todo) and tools:
        name, args = todo[len(done)]
        if args.get("query") == "$LAST" and done:
            got = re.search(r"<untrusted[^>]*>\n(.*?)\n</untrusted>", str(done[-1]["content"]), re.S)
            args = dict(args, query=re.sub(r"^\d+\t", "", (got.group(1) if got else "").splitlines()[0]) if got else "")
        return _call(name, args, len(done))
    if todo:
        firsts = []
        for m in done:
            body_ = re.search(r"<untrusted[^>]*>\n(.*?)\n</untrusted>", str(m["content"]), re.S)
            firsts.append((body_.group(1) if body_ else "").splitlines()[0][:80] if body_ else "")
        return {"role": "assistant", "content": f"Done: {len(done)} step{'s' if len(done) != 1 else ''}. " + " | ".join(firsts)}
    return {"role": "assistant", "content": "Here is how I would start: list the open work, pick the smallest next step, and "
                                            "check it with you before anything goes out."}
