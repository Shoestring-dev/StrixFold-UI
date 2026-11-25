"""What is left of Agents' routes: /studio/api/agents/... behind Studio's session, on both listeners (studio_http.py
hands them over after its own checks: the session cookie on every request; for writes also this Mac's Host, a
same-origin Origin and the page token). The Agents page (research runs, definitions, approvals and swarms) is gone;
the runner stays for the Command Centre's leads (halo_agents.Runs.start_lead).

  POST   agents/typing                the composer's typing heartbeat (the user first: no agent request for 30 s)
Nothing a request carries is logged.
"""

DEFAULTS = {"research_beta": True}


def settings(S):
    return dict(DEFAULTS, **(S["store"].kv("agents_settings", {}) or {}))


def get(h, S, parts, q):
    from studio_http import send_json
    return send_json(h, 404, {"error": "not found"})


def write(h, S, method, parts, b):
    from studio_http import send_json
    A = S.get("agents")
    if A is None:
        return send_json(h, 503, {"error": "Agents is not set up on this dashboard"})
    if parts == ["typing"] and method == "POST":
        A.owner.typed()
        return send_json(h, 200, {"ok": True})
    return send_json(h, 404, {"error": "not found"})
