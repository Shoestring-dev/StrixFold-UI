"""The conductor.

One live picture of every model request this Mac sends to the pairs (chats with their leads, team agents) and the
one place that decides where each runs. Before it, chat routing, the team scheduler and the pairs' holds each decided
alone: the scheduler counted only agents, so the lead and the user's chats came on top of full pairs and the engines
queued them blindly (waits of 25-63 s), and all three agents and the lead shared one pair while the other idled.

- Capacity: a pair runs its profile's streams at once. An agent takes a slot only when one is free beyond RESERVE (kept
  for the user's chats); otherwise it waits here, between its steps, where priority holds. A chat is never held back: it
  goes to its best pair at once and counts there.
- Placement: a chat (and the lead working in it) goes to the main boxes while no other chat is answering there, else to
  its own pair while that has room, else to the pair with the most room. Agents never keep a chat off the main boxes: a
  chat there waits at most for the agent step in flight. While no chat or lead is on the main boxes, an agent goes there
  first (their one stream was kept for chats, so a lead waiting on its agents left them idle), then to its own extra
  pair while that has room, else the extra pair with the most room (a tie: the later one). Moving costs one full read of
  its prompt there, said in the log. Lent to one agent at a time (the tenant, its cache there): no other agent takes
  them while it used them in the last TENANT_S, and they are free again as soon as it ends. A chat's turn keeps them
  between its steps (its tools run there, then its next step comes) for up to CHAT_HOLD_S after each step, until the
  turn ends or its lead waits for its agents (turn_over): lent then, an agent's step was in the way of every step of the
  chat's, up to 8192 tokens each (review).
- Failures: a pair whose request failed to connect is set aside for AWAY_S; its conversations move.
- Setup: pairs the team started are held while agents need them and let go RELEASE_AFTER_S after the last one (their
  idle stop applies again; chat_team asks for the start, the conductor ends the hold).
- What it decided and why (view): each pair's slots, who holds them, and the recent moves.
"""
import collections
import threading
import time

RESERVE = 1               # slots per pair an agent never takes (the user's chats)
AWAY_S = 60               # a pair whose request failed to connect is set aside this long
RELEASE_AFTER_S = 600     # a pair held for agents is let go this long after agents last needed it
TENANT_S = 120            # the agent the main boxes are lent to keeps them while it used them this recently (its cache is there)
CHAT_HOLD_S = 60          # a chat's turn keeps the lent main boxes this long after each of its steps there (its tools run)
KINDS = ("chat", "agent")


def window(st):
    return int((((st or {}).get("profile") or {}).get("settings") or {}).get("context") or 0)


def streams(st):
    return max(1, int((((st or {}).get("profile") or {}).get("settings") or {}).get("streams") or 1))


class Conductor:
    def __init__(self, app):
        self.app = app
        self.cv = threading.Condition()
        self.live = {}            # lease id -> {"pair", "kind", "key", "label", "since"}
        self.where = {}           # conversation key -> the pair holding its prompt cache
        self.away = {}            # pair -> set aside until
        self.held = {}            # pair -> when agents last needed it (we hold it on unc-swarm)
        self.tenant = None        # (agent key, last used): the one agent the main boxes are lent to
        self.between = {}         # chat key -> when its last request on the main boxes ended (its turn goes on: turn_over)
        self.log = collections.deque(maxlen=60)
        self.n = 0

    # -- the pairs
    def states(self):
        """Every pair's state, from the app: read OUTSIDE the lock (a pair whose tunnel hangs takes up to 5 s to say
        so, and every chat's request and release would wait behind it)."""
        return list(self.app.pair_states()) if hasattr(self.app, "pair_states") else []

    def pairs(self, model_id, states=None):
        """[(name, label, state)] of the pairs ready on model_id now, in the app's order (the main boxes first, then the
        extra pairs), those set aside left out."""
        now, out = time.time(), []
        for name, label, st in (self.states() if states is None else states):
            if not st or st.get("state") != "ready" or (st.get("profile") or {}).get("model_id") != model_id:
                continue
            if self.away.get(name, 0) > now:
                continue
            out.append((name, label, st))
        return out

    def busy(self, name, kind=None):
        return sum(1 for x in self.live.values() if x["pair"] == name and (kind is None or x["kind"] == kind))

    def room(self, name, st, kind):
        if kind == "agent" and self.lent(name, st) and self.chat_between():
            return 0  # a chat's turn is between its steps there
        return streams(st) - self.busy(name) - (RESERVE if kind == "agent" and not self.lent(name, st) else 0)

    def chat_between(self):
        """A chat's turn on the main boxes is between its steps (CHAT_HOLD_S after its last one at most)."""
        now = time.time()
        for k in [k for k, t in self.between.items() if now - t >= CHAT_HOLD_S]:
            self.between.pop(k, None)
        return bool(self.between)

    @staticmethod
    def lent(name, st):
        """The main boxes on one stream: lent to an agent while no chat or lead is there, no slot kept (a chat there waits at
        most for the agent's step in flight: halos_free)."""
        return name == "halos" and streams(st) == 1

    def halos_free(self, st, key=None):
        """The main boxes (their state st) have a stream for this chat now; the chat's own requests there don't count, nor do
        agents' (lent the stream, they give it back after the step in flight)."""
        if not st:
            return False
        with self.cv:
            used = sum(1 for x in self.live.values() if x["pair"] == "halos" and x["key"] != key and x["kind"] == "chat")
        return streams(st) - used > 0

    def agent_slots(self, model_id):
        """How many agents can hold a step at once over every pair (the team's admission)."""
        states = self.states()
        with self.cv:
            return sum(max(0, streams(st) - (self.busy(n, "chat") + bool(self.chat_between()) if self.lent(n, st) else RESERVE))
                       for n, _, st in self.pairs(model_id, states))

    # -- placement
    def choose(self, model_id, kind, key, want=0, avoid=(), states=None):
        """(name, label, state) for this request now; None while every pair is full for an agent (a chat always gets
        its best pair). The main boxes carry the chats and their leads, and an agent while no chat is there; else agents go to
        the extra pairs."""
        cands = [c for c in self.pairs(model_id, states) if window(c[2]) >= want and c[0] not in avoid]
        if not cands:
            return None
        rooms = {c[0]: self.room(c[0], c[2], kind) for c in cands}
        prev = self.where.get(key)
        order = [c[0] for c in cands]
        if kind == "agent":
            halos = next((c for c in cands if c[0] == "halos" and rooms[c[0]] > 0), None)
            t = self.tenant if halos and self.lent("halos", halos[2]) else None
            recent = bool(t) and time.time() - t[1] <= TENANT_S
            if recent and t[0] == key:
                return halos  # the agent they are lent to: its cache is there
            other = recent and t[0] != key  # lent to another agent now: never taken from it, even by one that ran there
            if prev in rooms and rooms[prev] > 0 and not (prev == "halos" and other):
                return next(c for c in cands if c[0] == prev)
            if halos and not other:
                return halos  # the main boxes first while they have room (lent: one agent at a time)
            pool = [c for c in cands if c[0] != "halos" and rooms[c[0]] > 0]
            return max(pool, key=lambda c: (rooms[c[0]], order.index(c[0]))) if pool else None
        if "halos" in rooms and self.halos_room(rooms, cands):
            return next(c for c in cands if c[0] == "halos")
        if prev in rooms and rooms[prev] > 0:
            return next(c for c in cands if c[0] == prev)
        return max(cands, key=lambda c: (rooms[c[0]] > 0, rooms[c[0]], -order.index(c[0])))

    def halos_room(self, rooms, cands):
        """A chat's room on the main boxes: their streams less the other chats there (agents give theirs back)."""
        st = next(c[2] for c in cands if c[0] == "halos")
        return streams(st) - self.busy("halos", "chat") > 0

    def acquire(self, model_id, kind, key, want=0, stop=None, label=None, on_wait=None, avoid=()):
        """A lease on a slot for one request: the pair to send it to. An agent waits for room (None if stop is set);
        a chat never waits. The pairs' states are read outside the lock (states). model_id may be a function, asked again
        each time round (a connected server's model, which can change while an agent waits: chat_team.Team.model_id)."""
        waited = False
        ask = model_id if callable(model_id) else (lambda: model_id)
        while True:
            model_id = ask()
            states = self.states()
            with self.cv:
                pick = self.choose(model_id, kind, key, want, avoid, states)
                if pick is not None:
                    name, plabel, st = pick
                    prev = self.where.get(key)
                    if prev and prev != name:
                        why = ("its pair is set aside" if self.away.get(prev, 0) > time.time() else
                               "the main boxes come first while they are free" if kind == "agent" and name == "halos" else
                               "its pair is full" if prev in [c[0] for c in self.pairs(model_id, states)] else
                               "its pair no longer serves the model")
                        self._note(f"{label or key} moved to {plabel}: {why} (its prompt is read again there once)", key=key)
                    self.where[key] = name
                    self.n += 1
                    lease = {"id": self.n, "pair": name, "label": plabel, "state": st, "kind": kind, "key": key,
                             "who": label or key, "since": time.time(), "prev": prev}
                    self.live[lease["id"]] = lease
                    if kind == "agent" and name != "halos":
                        self.held.setdefault(name, time.time())
                    if kind == "agent" and name == "halos":
                        self.tenant = (key, time.time())
                    return lease
                if kind != "agent" or (stop is not None and stop.is_set()):
                    return None
            if not waited and on_wait:
                on_wait()
            waited = True
            with self.cv:
                self.cv.wait(1.0)

    def release(self, lease, ok=True):
        if not lease:
            return
        with self.cv:
            self.live.pop(lease["id"], None)
            if lease["kind"] == "chat" and lease["pair"] == "halos" and lease["key"]:
                self.between[lease["key"]] = time.time()  # its turn goes on: its tools run, then its next step
            if lease["kind"] == "agent" and lease["pair"] == "halos" and self.tenant and self.tenant[0] == lease["key"]:
                self.tenant = (lease["key"], time.time())
            if not ok and self.away.get(lease["pair"], 0) <= time.time():
                self.away[lease["pair"]] = time.time() + AWAY_S
                self._note(f"{lease['label']} did not answer: set aside for {AWAY_S} s; its conversations move")
            self.cv.notify_all()

    def turn_over(self, key):
        """A chat's turn ended, or its lead waits for its agents (wait_agents): the main boxes are lent again at once."""
        with self.cv:
            if self.between.pop(key, None) is not None:
                self.cv.notify_all()

    def done(self, key):
        """An agent ended (done, stopped, failed): the main boxes are no longer lent to it (its pair stays its own, for a
        follow-up's cache)."""
        with self.cv:
            if self.tenant and self.tenant[0] == key:
                self.tenant = None
                self.cv.notify_all()

    def track(self, endpoint, kind, key, label=None):
        """A lease for a request whose pair was chosen already (a chat's turn): it counts, and its pair becomes the
        conversation's (affinity)."""
        for name, plabel, st in (self.app.pair_states() if hasattr(self.app, "pair_states") else []):
            if st and self.app.local_endpoint(st).rstrip("/") == str(endpoint or "").rstrip("/"):
                with self.cv:
                    self.where[key] = name
                    self.n += 1
                    lease = {"id": self.n, "pair": name, "label": plabel, "state": st, "kind": kind, "key": key,
                             "who": label or key, "since": time.time(), "prev": None}
                    self.live[lease["id"]] = lease
                    return lease
        return None

    def prefer(self, key, states):
        """The pair a chat's next message should go to among those that fit it: its own while it has room, else the
        one with the most room (a tie: the first)."""
        if not states:
            return None
        with self.cv:
            names = [s.get("pair") for s in states]
            prev = self.where.get(key)
            room = {s.get("pair"): streams(s) - self.busy(s.get("pair")) for s in states}
            if prev in names and room[prev] > 0:
                return states[names.index(prev)]
            return max(states, key=lambda s: (room[s.get("pair")], -names.index(s.get("pair"))))

    # -- holds
    def tick(self, needed):
        """Every scheduler tick: agents need the pairs (queued or working) or not. A pair held for them is let go
        RELEASE_AFTER_S after the last need."""
        now = time.time()
        with self.cv:
            if needed:
                for name in list(self.held):
                    self.held[name] = now
                return
            done = [n for n, t in self.held.items() if now - t > RELEASE_AFTER_S]
            for n in done:
                self.held.pop(n, None)
        for n in done:
            ok, _ = self.app.pair_post(n, "/api/hold", {"on": False}) if hasattr(self.app, "pair_post") else (False, None)
            self._note(f"{n}: no agents for {RELEASE_AFTER_S // 60} min, so its hold is off (its idle stop applies)"
                       if ok else f"{n}: couldn't take its hold off")

    def hold(self, name):
        with self.cv:
            self.held[name] = time.time()

    # -- what it decided
    def _note(self, text, key=None):
        self.log.appendleft({"t": round(time.time(), 1), "text": text, "key": key})

    def moves(self, key):
        return [x for x in self.log if x.get("key") == key]

    def view(self, model_id=None):
        with self.cv:
            live = list(self.live.values())
            away = dict(self.away)
            held = dict(self.held)
        now, out = time.time(), []
        for name, label, st in (self.app.pair_states() if hasattr(self.app, "pair_states") else []):
            p = (st or {}).get("profile") or {}
            mine = [x for x in live if x["pair"] == name]
            out.append({"pair": name, "label": label, "state": (st or {}).get("state") or "unreachable",
                        "model_id": p.get("model_id"), "profile": p.get("id"), "slots": streams(st) if st else 0,
                        "busy": len(mine), "chats": sum(1 for x in mine if x["kind"] == "chat"),
                        "agents": [x["who"] for x in mine if x["kind"] == "agent"], "away": away.get(name, 0) > now,
                        "held": name in held, "reserve": RESERVE})
        return {"pairs": out, "log": list(self.log)[:20]}


C = None  # the app's conductor (halo_control sets it)
