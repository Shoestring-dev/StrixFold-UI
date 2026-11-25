"""The chats' waiting messages (Outbox): what the user typed while the model was away, sent once it serves."""
import threading
import time

import halo_chat
import studio


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + "studio: " + msg, flush=True)


def _later(w):
    """A waiting message that isn't the user's: a team update, or a goal's next turn."""
    return bool((w.get("meta") or {}).get("team") or (w.get("meta") or {}).get("goal"))


class Outbox:
    """Messages typed while the model was away (another one served, or none ran): stored with their chat
    (Store.enqueue), shown in the thread as waiting, and sent here, oldest first and one turn at a time per chat, once
    the chat's model serves. They survive a reload and a dashboard restart. When nothing serves, the oldest one's model
    is started for them; when another model serves, they wait for the user's Switch."""
    route = None  # App.chat_state: the pair (the main boxes or an extra pair) that serves a chat's model
    goals = None  # chat_goal.Goals: what a chat's goal does next once its turn has ended

    def __init__(self, store, agent, engine, halod_state, endpoint, start_profile, vision=lambda: False, chat_agent=None, verify=None,
                 relay=None):
        # agent and vision: an add-on's own turns (None: there is none)
        self.s, self.agent, self.engine, self.chat_agent = store, agent, engine, chat_agent
        self.relay = relay or agent.relay  # the chats' claims (chats.Relay), shared with the chat agent
        self.verify = verify or (lambda st: None)  # App.model_check: the tunnel answers with the model halod started
        self.halod_state, self.endpoint, self.start_profile, self.vision = halod_state, endpoint, start_profile, vision
        self.pick = None  # App.endpoint_for: a queued Chat message goes to its chat's copy (a model with one per box)
        self.started_at = 0.0
        self.mu = threading.Lock()
        self.running = set()   # chats with a turn from the outbox in flight

    def why_not(self, st, model_id="dsv41-unc"):
        """None when a waiting message for model_id can go now; otherwise why not (for the page)."""
        if not st or st.get("state") != "ready":
            return (st or {}).get("state") or "unreachable"
        if ((st.get("profile") or {}).get("model_id")) != model_id:
            return "other"
        if self.verify(st):
            return "unverified"  # held: never sent to a model halod did not start
        return None

    def chat_turn(self, cid, w=None):
        """Whether the chat's own model answers its waiting message w."""
        return bool(self.chat_agent)

    def need(self, cid, w=None):
        """(profile, model id) a chat's waiting message w needs: its chat's own model."""
        return halo_chat.need(self.s.one("SELECT kind, model FROM chats WHERE id=?", (cid,)) or {})

    def tick(self):
        # a Private chat's waiting message is read only while Private is unlocked (the outbox then holds the grant, as
        # the page that queued it did); while locked it waits and is never opened
        import contextlib
        import private_store as _private
        if self.goals is not None:
            try:
                self.goals.tick(self)
            except Exception as e:  # noqa: BLE001 - a goal never holds the waiting messages up
                log(f"outbox: the goals' tick failed ({type(e).__name__})")
        with _private.granted() if self.s.vault.unlocked() else contextlib.nullcontext():
            waiting = [w for w in self.s.waiting() if not w["meta"].get("locked_private") and not w["meta"].get("unreadable")]
        if not waiting:
            return
        st = self.halod_state(True)
        first = {}
        for w in waiting:  # oldest first, but the user's own messages before a team update (chat_team._wake), which waits on
            cur = first.get(w["chat_id"])  # (review: after Send now the update went first and the user's message waited)
            if cur is None or (_later(cur) and not _later(w)):  # ... and before a goal's next turn (chat_goal)
                first[w["chat_id"]] = w
        start = None
        for cid, w in first.items():
            pid, model_id = self.need(cid, w)
            stw = st
            if self.route and self.chat_turn(cid, w):  # a chat's message: the extra pairs when they serve its model
                import halo_chat
                stw = self.route(model_id, st, self.s.is_private(cid), want=halo_chat.context_of(pid), key=cid)
            why = self.why_not(stw, model_id)
            if why in ("idle", "failed"):
                start = start or pid  # the oldest waiting message's model
                continue
            if why:
                continue
            with self.mu:
                if cid in self.running:
                    continue
                entry = self.relay.claim(cid)  # held from here, so a send in between is told it is answering
                if entry is None:
                    continue
                # the row stays queued until the turn writes the message (Store.add_message's from_outbox, one
                # transaction): a Lock, an error or a crash before that loses nothing; `running` and the claim keep a
                # second tick off it
                entry["from_outbox"] = w["id"]
                self.running.add(cid)
            threading.Thread(target=self.send, args=(cid, w, stw, entry), daemon=True).start()
        if start and time.time() - self.started_at > 300:
            self.started_at = time.time()  # nothing serves: start the model they wait for (never switch another away)
            self.start_profile(start)

    def send(self, cid, w, st, entry=None):
        import contextlib
        import private_store as _private
        with _private.granted() if self.s.is_private(cid) and self.s.vault.unlocked() else contextlib.nullcontext():
            self._send(cid, w, st, entry)

    def _failed(self, cid, w, e):
        """A waiting message that can't be sent goes into the thread (out of its row, in one step) with the reason, so it
        is never lost; if its turn wrote it already, only the reason."""
        import private_store as _private
        try:
            try:  # its meta whole, so Retry sends it the way it was meant
                self.s.add_message(cid, "user", w["content"], dict(w["meta"] or {}), from_outbox=w["id"])
            except studio.OutboxGone:
                pass
            self.s.add_message(cid, "assistant", "", {"error": f"Your waiting message could not be sent: {e}"})
        except (KeyError, _private.Locked):
            pass  # the chat was deleted, or Private locked meanwhile (a row still there waits)

    def _send(self, cid, w, st, entry=None):
        import private_store as _private
        try:
            if w["meta"].get("team"):  # a team update may have grown since the tick read it (chat_team._wake)
                w = next((x for x in self.s.waiting(cid) if x["id"] == w["id"]), w)
            ep = self.pick(st, ("chat", cid), None) if self.pick else self.endpoint(st)
            if self.s.is_private(cid):  # the same gate as a send: the words never go elsewhere
                import studio_http
                got = studio_http.private_endpoint({"private_gate": getattr(self, "private_gate", None)},
                                                   halo_chat.need(self.s.chat(cid))[1], st)
                if got and got[1]:
                    raise ValueError(got[1])
                ep = (got[0] if got else None) or ep
            gen = self.chat_agent.turn(cid, ep, st.get("served_model") or "dsv41", (st.get("profile") or {}).get("id"),
                                       w["content"], w["meta"].get("attachments") or [], "send", w["meta"].get("effort"),
                                       served_context=((st.get("profile") or {}).get("settings") or {}).get("context"), claim=entry,
                                       tools=not w["meta"].get("no_tools"),
                                       user_meta={k: w["meta"][k] for k in ("team", "goal") if w["meta"].get(k)} or None)
            for _ in gen:  # stored as it streams; the page follows the chat while it answers
                pass
        except studio.OutboxGone:
            pass  # cancelled while it was being taken: nothing to send
        except _private.Locked:
            if not self.s.is_private(cid):  # an ordinary chat's message that names a Private chat's file
                self._failed(cid, w, "a file in it belongs to a Private chat")
            # else Private locked between the take and the send: the row stays queued, sealed, for the next unlock
        except (ValueError, KeyError) as e:
            self._failed(cid, w, e)
        except Exception as e:  # noqa: BLE001 - never loop on one message every tick: it goes into the thread, with why
            log(f"outbox: a waiting message failed ({type(e).__name__})")
            self._failed(cid, w, f"something went wrong on this Mac ({type(e).__name__})")
        finally:
            if entry is not None:
                self.relay.release(cid, entry)  # only its own: a later claim is never removed
            with self.mu:
                self.running.discard(cid)
