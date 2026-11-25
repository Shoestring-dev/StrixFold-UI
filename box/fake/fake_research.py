"""The research loop's stand-in model (Agents, mac/halo_agents.py): answers each kind of request the loop makes
(plan JSON, notes with exact quotes from the part it was given, a tool call, merged notes, an outline, sections with
citations, a summary) from the request alone. fake_serve.py uses it on the fake backend; tests/agents_fake.py wraps it
in a recording server. Modes: "normal"; "exfil" (after reading a page that names a .../collect... address it asks to read that address
with text from its notes in the query, as a model that followed injected instructions would);
"follow" (it reads the address an injected instruction named, word for word); "secret" (it searches for the email
address in the question); "search" (it searches again before
reading)."""
import json
import re

import fake_lead

MARK = "You are a careful research assistant that runs privately"


def wants(body):
    msgs = body.get("messages") or []
    return bool(msgs) and (MARK in str(msgs[0].get("content") or "") or fake_lead.wants(body))


def call(name, args, n=0):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{n}", "type": "function",
                                                                 "function": {"name": name, "arguments": json.dumps(args)}}]}


def answer(body, mode="normal"):
    if fake_lead.wants(body):  # a project lead's tool turn (mac/lead_tools.py)
        return fake_lead.answer(body)
    msgs = body.get("messages") or []
    user = msgs[-1].get("content") if msgs else ""
    if body.get("max_tokens") == 1:
        return {"role": "assistant", "content": "O"}
    if "Write a short research plan" in user:
        return {"role": "assistant", "content": 'Here is the plan:\n```json\n{"subquestions": ["Which cards fit?", "What do they cost?"], '
                                                '"queries": ["x400 pcie 4.0 x4 100gbe", "x400 price used"]}\n```'}
    if "Write notes for the question from this part only" in user:
        m = re.search(r'<untrusted source="[^"]*">\n(.*?)\n</untrusted>', user, re.S)
        text = m.group(1) if m else ""
        all_ = [s.strip() for s in re.split(r"(?<=[.])\s+", text) if len(s.split()) >= 6]
        sents = all_[:3] + [s for s in all_[3:] if "collect" in s][:2]  # an injected address makes it into the notes
        if not sents:
            return {"role": "assistant", "content": "NOTHING RELEVANT"}
        out = []
        for s in sents:
            words = s.split()
            out.append(f"- {s[:120]} \"{' '.join(words[:10])}\"")
        if "Ignore all previous instructions" in text:
            out.append("- INSTRUCTIONS FOUND (not followed)")
        return {"role": "assistant", "content": "\n".join(out)}
    if "Reply with one tool call" in user:
        read_n = int((re.search(r"You have read (\d+)", user) or [0, 0])[1])
        hit = re.search(r"https?://[^\s\"'<>)\]]*collect[^\s\"'<>)\]]*", user)  # an address an injected instruction named
        if mode == "exfil" and hit and "the user did not approve" not in user:
            notes = re.search(r"## Findings[^\n]*\n- (.{40})", user)
            frag = (notes.group(1) if notes else "notes").replace(" ", "+")
            return call("web_read", {"url": hit.group(0).split("?")[0] + "?d=" + frag})
        if mode == "follow" and hit and "the user did not approve" not in user:
            return call("web_read", {"url": hit.group(0)})
        if mode == "secret" and "Searched for: contact" not in user and "skipped" not in user:  # a model that leaks the question
            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", user)
            if m:
                return call("web_search", {"query": "contact " + m.group(0)})
        if mode == "search" and read_n == 0 and "Searched for: more" not in user:
            return call("web_search", {"query": "more about x400"})
        ids = re.findall(r'source="[^"]*">\n(R\d+): ', user)
        if read_n >= 3 or not ids:
            return call("finish", {"why": "enough"})
        return call("web_read", {"url": ids[0]})
    if "Merge these findings" in user:
        lines = [l for l in user.splitlines() if l.startswith("- ")][:6]
        return {"role": "assistant", "content": "\n".join(lines)}
    if "Plan the report as JSON" in user:
        ids = sorted(set(re.findall(r"\[(S\d+)\]", user)))
        return {"role": "assistant", "content": json.dumps({"title": "Network cards at PCIe 4.0 x4", "sections": [
            {"heading": "Cards and slots", "covers": "which cards fit", "sources": ids[:2]},
            {"heading": "Prices", "covers": "prices new and used", "sources": ids[-2:] + ["S99"]}]})}
    if "Write the section" in user:
        ids = re.findall(r"\n\[(S\d+)\] ", user) or ["S1"]
        pids = re.findall(r"\[(S\d+\.\d+)\] \"", user)
        first = pids[0] if pids else ids[0]
        quote = re.search(r"\[S\d+\.\d+\] \"([^\"]{20,80}?)[.\"]", user)
        q = f' The maker says "{quote.group(1)}" [{first}].' if quote else ""
        return {"role": "assistant", "content": f"The X400 uses a PCIe 4.0 x4 slot with two 100GbE ports [{first}]. "
                                                f"New cards cost $649 according to the maker [{ids[-1]}, S42].{q} "
                                                "Prices for used cards change from week to week on auction sites. "
                                                f"The pages read give no used prices for the X400 [{first}]. "
                                                'One reviewer called it "the fastest card ever sold anywhere" [S1].'}
    if "judge it against the source text above only" in user:  # the report check: every sentence supported, unless
        # the stand-in finds the word "contested" (then it narrows it) or "fastest card" (then it is unsupported)
        out = []
        for i, sent, block in re.findall(r"Sentence (\d+) \([a-z]+\): ([^\n]*)\n(.*?)(?=\n\nSentence \d+ \(|$)", user, re.S):
            v = "unsupported" if "fastest card" in sent else "supported"
            src = re.search(r"^(?:Cited passage \[[^\]]+\]: |- (?:\[from a part not read before writing\] )?)(.+)$", block, re.M)
            span = " ".join(re.sub(r"^[…\s]+", "", src.group(1)).split()[:8]) if src and v == "supported" else ""
            out.append({"i": int(i), "verdict": v, "span": span, "corrected": ""})
        return {"role": "assistant", "content": json.dumps(out)}
    if "Write a summary" in user:
        return {"role": "assistant", "content": "The X400 fits a PCIe 4.0 x4 slot and costs $649 new [S1]. "
                                                "Used cards are cheaper, though the evidence here is thin."}
    return {"role": "assistant", "content": "OK: a short answer from the stand-in model, so a chat turn has words to time."}
