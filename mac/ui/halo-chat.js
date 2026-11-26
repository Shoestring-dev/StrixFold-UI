// StrixFold UI's chat (halo_chat.py): a conversation with one of the text models on the boxes, behind the passcode. The thread, a
// model sheet (bottom sheet on phones, popover on desktop) with the effort, text attachments, versions of the last turn,
// and Projects (folders of chats with shared instructions). It uses studio.js's poll, list, lock and toast
// (window.HaloStudioKit) and calls /studio/ only. Model output is rendered by md.js.
(() => {
  "use strict";
  const K = window.HaloStudioKit, MD = window.HaloMD;
  if (!K || !document.getElementById("page-talk")) return;
  const { call, el, svg, copy } = K;
  const $ = (id) => document.getElementById(id);
  const store = { get: (k) => { try { return JSON.parse(localStorage.getItem("halo-chat-" + k)); } catch (e) { return null; } },
    set: (k, v) => { try { localStorage.setItem("halo-chat-" + k, JSON.stringify(v)); } catch (e) { /* private mode */ } } };
  const IC = {
    copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
    retry: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
    edit: '<path d="M14.5 5.5l4 4L9 19H5v-4z"/><path d="M12.5 7.5l4 4"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    chev: '<path d="M9.5 6l6 6-6 6"/>',
    back: '<path d="M14.5 6l-6 6 6 6"/>',
    file: '<path d="M7 3.5h7l4.5 4.5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-15.5a1 1 0 0 1 1-1z"/><path d="M14 3.5V8h4.5"/>',
    folder: '<path d="M4 9.5h16v9a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5z"/><path d="M6 6.5h12M8 3.5h8"/>',
    search: '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/>',
  };
  const C = { page: null, id: null, chat: null, models: null, streaming: null, atts: [], menu: false, sheet: null, view: "main",
    newProject: null, projects: null, follow: 0, reloading: false };
  const DEFAULT_MODEL = "quick-chat";
  // a model by any of its window sizes' profiles (an older "long files" chat: its model's 64K)
  const modelOf = (pid) => {
    const all = (C.models && C.models.models) || [], legacy = (C.models && C.models.legacy) || {};
    pid = legacy[pid] || pid;
    return all.find((m) => m.profile === pid || (m.contexts || []).some((c) => c.profile === pid));
  };
  const sizeOf = (pid) => { const m = modelOf(pid), legacy = (C.models && C.models.legacy) || {}; pid = legacy[pid] || pid; return m && (m.contexts || []).find((c) => c.profile === pid); };
  const kOf = (tokens) => `${Math.max(1, Math.round(tokens / 1000))}K`;
  const effortOf = (id) => ((C.models && C.models.efforts) || []).find((e) => e.id === id);
  const curModel = () => (C.chat && C.chat.model) || store.get("model") || DEFAULT_MODEL;
  const curEffort = () => (C.chat && C.chat.effort) || store.get("effort") || "low";
  // ---------------------------------------------------------------- the view
  // V.stick: the view follows the end of the thread while the reader is there; scrolling up lets go, and nothing new
  // pulls it down until they come back (the ↓ button, or scrolling to the end). V.anchor: the message at the top of the
  // view and where it sits in the thread; when what is above it changes size (a redraw, a photo loading) the view moves
  // by as much, so it stays where they left it: Safari has no scroll anchoring of its own.
  // Nothing that streams moves the view: a message just sent goes near the top (V.pinK; the room under the thread gives
  // the room and shrinks as the answer grows), the answer fills the screen below it, and what runs past the bar waits
  // behind the ↓ button. Only a chat just opened keeps its end in view for a moment (photos still loading).
  const V = { stick: true, anchor: null, last: 0, h: 0, ours: null, up: 0, compact: false, until: 0, openT: 0, held: false, input: 0, moved: 0,
    dragY: null, pinK: null };
  const scroller = () => $("main");
  const talkShown = () => C.page === "talk" && !$("page-talk").hidden;
  const gap = () => { const m = scroller(); return m.scrollHeight - m.scrollTop - m.clientHeight; };
  function setTop(y) { const m = scroller(); m.scrollTop = y; V.ours = m.scrollTop; V.last = m.scrollTop; V.h = m.scrollHeight; }
  // to the end, followed from now on: a chat opened, the user's own message sent
  function toBottom() { V.stick = true; V.until = Date.now() + 2500; setTop(scroller().scrollHeight); $("t-down").classList.remove("new"); syncDown(); }
  // something new at the end (an answer's words, a step, a message read from the outbox): followed only while the user
  // is there, else the ↓ button gets its dot
  // a finger (or a wheel or key) just now wins over following: nothing moves the view under it (while an answer grows
  // the view was pulled back to the end before the reader could scroll away from it)
  const touching = () => V.held || Date.now() - V.input < 500;
  function follow() {
    if (V.stick && !touching() && Date.now() < V.until && !V.pinK) { setTop(scroller().scrollHeight); return; }
    if (below() > 120) $("t-down").classList.add("new");
    syncDown();
  }
  // how far the thread's end is below the bar, in px (what ↓ brings into view)
  const below = () => $("t-thread").getBoundingClientRect().bottom - $("t-composer-wrap").getBoundingClientRect().top;
  // a node by its key; a waiting message ("w…") that the model has read is the user's last message now (its key
  // changed)
  function byKey(k) {
    const t = $("t-thread"), n = k ? t.querySelector(`[data-k="${CSS.escape(k)}"]`) : null;
    if (n || !k || k[0] !== "w") return n;
    return [...t.querySelectorAll(".t-user:not(.waiting)")].pop() || null;
  }
  function pinNode() {
    const n = byKey(V.pinK);
    if (n && n.dataset.k !== V.pinK) V.pinK = n.dataset.k;
    return n;
  }
  const inScroller = (n) => n.getBoundingClientRect().top - scroller().getBoundingClientRect().top + scroller().scrollTop;
  const pinGap = () => ($("t-head") ? $("t-head").offsetHeight : 0) + 12;
  // room under the thread so the user's pinned message can sit near the top; it shrinks as the answer grows, to
  // nothing once the answer is longer than the screen
  function sizeSpacer() {
    const sp = $("t-room"), n = pinNode(), m = scroller();
    if (!sp) return;
    if (!n) { if (sp.style.height) sp.style.height = ""; return; }
    const rest = m.scrollHeight - sp.offsetHeight;
    const need = Math.max(0, Math.ceil(inScroller(n) - pinGap() + m.clientHeight - rest));
    if (Math.abs(need - sp.offsetHeight) >= 1) sp.style.height = need + "px";
  }
  // the user's own message just sent: near the top of the view, the answer below it
  function pinTurn(n) {
    if (!n || !n.dataset.k) { toBottom(); return; }
    V.pinK = n.dataset.k; V.stick = false; V.until = 0;
    sizeSpacer();
    setTop(Math.max(0, inScroller(n) - pinGap()));
    V.anchor = anchorNow();
    $("t-down").classList.remove("new"); syncDown();
  }
  // a node the user just added: brought above the bar if it is below it, nothing else moves
  function reveal(n) {
    const d = n.getBoundingClientRect().bottom - $("t-composer-wrap").getBoundingClientRect().top + 12;
    if (d > 0) setTop(scroller().scrollTop + d);
  }
  function anchorNow() {
    const top = scroller().getBoundingClientRect().top, t0 = $("t-thread").getBoundingClientRect().top;
    for (const n of $("t-thread").children) {
      if (!n.dataset.k) continue;
      const r = n.getBoundingClientRect();
      if (r.bottom > top) return { k: n.dataset.k, at: r.top - t0 };
    }
    return null;
  }
  function restore() {
    const a = V.anchor, t = $("t-thread");
    if (!a) return;
    const n = byKey(a.k) || (a.k === "live" ? [...t.querySelectorAll(".t-asst")].pop() : null);
    if (!n) return;
    const at = n.getBoundingClientRect().top - t.getBoundingClientRect().top, d = at - a.at;
    a.at = at;
    if (Math.abs(d) >= 1) setTop(scroller().scrollTop + d);
  }
  function syncDown() {
    const b = $("t-down"), show = talkShown() && !!C.chat && below() > 120;
    if (b.hidden === show) b.hidden = !show;
  }
  // the bar while reading back on a phone: one line, the words typed and Send (Stop while it answers);
  // whole again at the end, on a tap, or whenever it is in use
  const small = window.matchMedia("(max-width: 860px), (pointer: coarse)");
  function setCompact(on) {
    on = !!on && small.matches && !C.sheet && $("t-sheet-wrap").hidden
      && document.activeElement !== $("t-prompt") && !$("t-mic").classList.contains("rec") && !$("t-mic").classList.contains("busy");
    if (on === V.compact) return;
    V.compact = on;
    const w = $("t-composer-wrap");
    w.classList.toggle("compact", on);
    clearTimeout(V.openT);
    w.classList.toggle("opening", !on);
    if (!on) V.openT = setTimeout(() => w.classList.remove("opening"), 300);
  }
  scroller().addEventListener("scroll", () => {
    if (!talkShown()) return;
    const m = scroller(), y = m.scrollTop, g = gap(), dy = y - V.last;
    const ours = V.ours !== null && Math.abs(y - V.ours) < 2;  // setTop's
    // the user's: a finger, a mouse or a key on it just now, or the thread the same size (a scroll the layout made,
    // under a still view, is not the user's)
    const own = !ours && (V.held || Date.now() - V.input < 800 || m.scrollHeight === V.h);
    V.ours = null; V.last = y; V.h = m.scrollHeight; V.moved = Date.now();
    if (g < 48) { V.stick = true; $("t-down").classList.remove("new"); }
    if (own) {
      if (dy < 0 && (g >= 48 || V.held)) V.stick = false;  // a finger moving the thread up lets go at once
      V.up = dy < 0 ? V.up - dy : 0;
      if (V.up > 40 && g > 160) setCompact(true);
    }
    if (!ours) V.anchor = anchorNow();
    if (y < EARLIER_PX) nearTop();
    if (g < 80) setCompact(false);
    syncDown();
  }, { passive: true });
  // the thread or the bar changed size: at the end, the end stays in view (an answer growing, a picture loading just
  // after a chat opened, the bar growing a line); read back, the message at the top stays where it was
  const sized = new ResizeObserver((es) => {
    if (!talkShown()) return;
    sizeSpacer();
    if (V.stick && !V.pinK && !touching() && (Date.now() < V.until || es.some((e) => e.target.id === "t-composer-wrap"))) setTop(scroller().scrollHeight);
    else restore();
    syncDown();
  });
  sized.observe($("t-thread")); sized.observe($("t-composer-wrap"));
  ["load", "loadedmetadata"].forEach((ev) => $("t-thread").addEventListener(ev, () => { if (V.stick) V.until = Math.max(V.until, Date.now() + 500); }, true));
  $("t-down").addEventListener("click", () => {  // the thread's end just above the bar (not the blank room below)
    $("t-down").classList.remove("new");
    const was = V.compact;
    setCompact(false);  // the whole bar first (its opening takes 300 ms): measured small, the end would land under it
    setTimeout(() => scroller().scrollTo({ top: scroller().scrollTop + below() + 16,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }), was ? 320 : 0);
  });
  // a tap on the small bar opens it; using it keeps it open
  $("t-composer").addEventListener("click", (e) => { if (V.compact && !e.target.closest("button, textarea")) $("t-prompt").focus(); });
  // the words only: Send, Stop and the ↓ button work where they are (the bar growing would move them from under the
  // user's finger)
  $("t-prompt").addEventListener("focus", () => setCompact(false));
  // a drag on the thread puts the keyboard away; a tap doesn't
  scroller().addEventListener("touchstart", (e) => {
    V.dragY = talkShown() && !$("t-composer-wrap").contains(e.target) ? e.touches[0].clientY : null;
  }, { passive: true });
  scroller().addEventListener("touchmove", (e) => {
    if (V.dragY === null || Math.abs(e.touches[0].clientY - V.dragY) < 12) return;
    V.dragY = null;
    if (document.activeElement === $("t-prompt")) $("t-prompt").blur();
  }, { passive: true });
  ["touchstart", "pointerdown"].forEach((ev) => scroller().addEventListener(ev, () => { V.held = true; }, { passive: true }));
  ["touchend", "touchcancel", "pointerup", "pointercancel"].forEach((ev) => window.addEventListener(ev, () => { V.held = false; V.input = Date.now(); }, { passive: true }));
  ["wheel", "keydown"].forEach((ev) => window.addEventListener(ev, () => { V.input = Date.now(); }, { passive: true, capture: true }));
  // typing with nothing in focus goes to the bar; Space still scrolls
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.isComposing || e.key.length !== 1 || e.key === " " || !talkShown()) return;
    const a = document.activeElement;
    if (a && a !== document.body && a.closest("input, textarea, select, [contenteditable], [role='dialog'], [role='menu'], .side, .viewer")) return;
    if (C.sheet || C.menu || document.body.classList.contains("idle-locked") || $("app").classList.contains("side-open")
      || !$("viewer").hidden || ($("s-lock") && !$("s-lock").hidden)) return;
    $("t-prompt").focus();  // the key's character lands there
  });
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const chars = (n) => (n < 1000 ? plural(n, "character") : `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k characters`);

  async function loadModels() {
    try { C.models = await call("GET", "/studio/api/chat/models"); } catch (e) { /* the chip keeps its words */ }
    syncInput();
    redrawHello();
  }

  // ---------------------------------------------------------------- routing (studio.js calls this after the lock)
  async function route(page, id, force) {
    C.page = page;
    closeSheet(); closeMenu();
    if (!C.models) loadModels();
    if (page === "talk") {
      V.last = scroller().scrollTop; V.h = scroller().scrollHeight;
      if (force || id !== C.id || (id && !C.chat)) await open(id); else { render(); syncComposer(); syncDown(); }
    }
    if (page === "projects") renderProjects(id);
  }
  async function open(id) {
    if (C.streaming && C.streaming.id !== id) { C.streaming.detach = true; C.streaming = null; }
    swapDraft(id);
    C.id = id; C.chat = null; C.lockedId = null; C.from = null; clearTimeout(C.follow);
    hideOffline(); setCompact(false); V.stick = true; V.anchor = null; V.pinK = null; sizeSpacer(); syncDown();
    if (!id) {
      C.newPerm = null; C.newPermBody = null; C.newTeam = null;
      keepAtts();
      render(); syncInput(); setTitle(); syncPerm(); K.renderList();
      return;
    }
    try { C.chat = await call("GET", chatUrl(id)); C.lockedId = null; }
    catch (e) {
      if (e.status === 404 && e.data && e.data.error === "locked") { C.lockedId = id; keepAtts(); renderLocked(id); setTitle(); syncPerm(); return; }
      if (e.status !== 401) { K.toast(e.status === 404 ? "That chat was deleted." : e.message, true); location.hash = "#/talk"; }
      return;
    }
    keepAtts();
    render(); syncInput(); setTitle(); setCompact(false); toBottom();
    nearTop();
    K.renderList();
    if (C.chat.streaming) followSoon();
  }
  async function reload() {
    if (!C.id || C.reloading || C.streaming) return;
    const t = $("t-thread"), a = document.activeElement;
    if (a && a.tagName === "TEXTAREA" && t.contains(a)) return;  // an edit being typed: the next poll tries again
    const id = C.id, count = (x) => ((x && x.messages) || []).length + ((x && x.waiting) || []).length;
    C.reloading = true;
    let c;
    try { c = await call("GET", chatUrl(id)); } catch (e) { return; } finally { C.reloading = false; }
    if (C.id !== id) return;
    const more = count(c) !== count(C.chat) || !!c.streaming;
    C.chat = c;
    render(); syncComposer(); setTitle();
    sizeSpacer(); restore(); if (more) follow(); else syncDown();  // a redraw from the poll: the view stays where the user reads
    clearTimeout(C.follow);
    if (c.streaming && !C.streaming) followSoon();  // an answer the Mac is writing (sent from the outbox, or before a lock)
  }
  function followSoon() { clearTimeout(C.follow); C.follow = setTimeout(reload, 700); }
  function onState(st, rev) {
    if (C.page === "talk") {
      if (C.chat && !C.streaming && (
        (rev && rev.chat && rev.chat !== C.chat.rev))) reload();
      syncBar();
      syncChip();
    }
  }
  async function refresh() {
    if (C.page === "talk") await reload();
    if (C.page === "projects") await renderProjects(C.projectId || null);
  }
  // a Private chat, open or locked (C.lockedId: the chat shown is Private and this page has no token for it)
  const isPriv = () => !!((C.chat && C.chat.private) || (!C.chat && C.lockedId));
  const chatTitle = () => (C.chat && C.chat.title) || (!C.chat && C.lockedId ? "Private chat" : "New chat");
  function setTitle() {
    const t = chatTitle();
    $("t-title").textContent = t;
    $("t-more").hidden = !C.chat;
    // a Private chat's title never goes into the tab's title (the browser's history keeps it on disk); the idle lock
    // keeps its own title
    if (C.page === "talk" && !document.body.classList.contains("idle-locked")) document.title = isPriv() ? "Private chat · StrixFold UI" : `${t} · StrixFold UI`;
    if (window.HaloShellPaint) window.HaloShellPaint();
  }

  // ---------------------------------------------------------------- the thread
  function versionsOf(msgs, lastUser) {
    if (!lastUser || lastUser.tgroup === null || lastUser.tgroup === undefined) return null;
    const vs = [...new Set(msgs.filter((m) => m.tgroup === lastUser.tgroup && m.role === "user").map((m) => m.tversion))].sort((a, b) => a - b);
    return vs.length > 1 ? { group: lastUser.tgroup, all: vs, cur: lastUser.tversion } : null;
  }
  function redrawHello() { const hi = document.querySelector("#t-thread .t-hello"); if (hi) hi.replaceWith(hello()); }  //
  function hello() {
    const box = el("div", "t-hello"), m = modelOf(curModel());
    let title = "How can I help?", priv = !C.id;
    // its own name before the models load ("The model runs…"), and no promise the web level breaks
    let words = `${m ? m.name : "DeepSeek V4.1"} runs on your own boxes. ${curPerm() === "box_web" ? "Web searches and pages it opens go to the internet; the rest stays on your machines." : "Nothing you write here leaves your machines."}`;
    box.append(el("h1", "", title), el("p", "", words));
    if (C.newProject && C.projects) {
      const p = C.projects.find((x) => x.id === C.newProject);
      if (p) box.appendChild(el("p", "t-hello-proj", `In the project ${p.name}`));
    }
    if (priv && !((K.state() || {}).llm || {}).connected) {  // (connect.py: no Private chats)
      const b = el("button", "t-priv-new"); b.type = "button";
      b.append(svg(LOCK_IC, "t-priv-ic"), el("span", "", "Make this a Private chat"));
      b.addEventListener("click", () => (K.hasPrivate() ? newPrivate() : openUnlock(newPrivate)));
      box.appendChild(b);
      box.appendChild(el("p", "t-priv-what", "Sealed on this Mac with your passcode: only your unlocked page can read it. Container tools work, with their folder sealed on Toolbox. No Full access, audio or Office files."));
    }
    return box;
  }
  function render() {
    const t = $("t-thread");
    t.replaceChildren();
    const all = (C.chat && C.chat.messages) || [];
    const msgs = all.filter((m) => !m.hidden);
    const waiting = (C.chat && C.chat.waiting) || [];
    if (!msgs.length && !waiting.length && !(C.streaming && C.streaming.id === C.id)) { t.appendChild(hello()); return; }
    const i = firstShown(msgs);
    if (i > 0) t.appendChild(earlierBtn());
    drawMsgs(t, msgs.slice(i), all, msgs, waiting);
    for (const w of waiting) { const n = waitingMsg(w); n.dataset.k = `w${w.id}`; t.appendChild(n); }
  }
  // the messages `part` (of the visible `msgs`) into `into`, each with its key: the view's anchor finds it after a redraw
  function drawMsgs(into, part, all, msgs, waiting) {
    const lastUser = [...msgs].reverse().find((m) => m.role === "user"
    );
    const lastAsst = [...msgs].reverse().find((m) => m.role === "assistant");
    const after = new Map(msgs.filter((m) => m.role === "summary").map((m) => [m.meta.upto, m]));
    const keyed = (n, k) => { n.dataset.k = k; into.appendChild(n); };
    for (const m of part) {
      if (m.role === "summary") continue;
      if (m.role === "note") { if (m.meta.perm_to) keyed(permDivider(m), `m${m.id}`); continue; }
      keyed(m.role === "user" ? userMsg(m, m === lastUser && !waiting.length)
        : asstMsg(m, m === lastAsst && !waiting.length, m === lastAsst ? versionsOf(all, lastUser) : null), `m${m.id}`);
      if (after.has(m.id)) keyed(compactDivider(after.get(m.id)), `s${after.get(m.id).id}`);
    }
  }
  // the thread draws its newest messages (SHOW when a chat opens), and SHOW more each time the user reads back near
  // the top (on a slower computer: a 300-message chat drew ~38,000 elements on every open and redraw, and each key and
  // scroll waited on them). C.from: the first message drawn
  const SHOW = 30, EARLIER_PX = 1500;
  const chatUrl = (id) => `/studio/api/chats/${id}?light=1`;  // light(): steps' output and thinking come when opened
  function firstShown(msgs) {
    let i = C.from == null ? -1 : msgs.findIndex((m) => m.id >= C.from);
    if (i < 0) i = Math.max(0, msgs.length - SHOW);
    for (let n = 0; i > 0 && n < 10 && msgs[i].role !== "user"; n++) i--;  // from the user's question, not mid-answer
    C.from = msgs.length ? msgs[i].id : null;
    return i;
  }
  function earlierBtn() {
    const b = el("button", "textbtn t-earlier", "Show earlier messages"); b.type = "button";
    b.addEventListener("click", earlier);
    return b;
  }
  // the next SHOW above the ones drawn, put in above them: nothing drawn is drawn again, and the message at the top of
  // the view stays where it is on the screen (Chrome's own scroll anchoring may have kept it there already; Safari has
  // none, so restore's thread-relative move would count twice in Chrome)
  function earlier() {
    const all = (C.chat && C.chat.messages) || [], msgs = all.filter((m) => !m.hidden), i = msgs.findIndex((m) => m.id >= C.from);
    const b = $("t-thread").querySelector(".t-earlier");
    if (i <= 0 || !b) return;
    const a = anchorNow(), n = a && byKey(a.k), y = n && n.getBoundingClientRect().top;
    C.from = msgs[Math.max(0, i - SHOW)].id;
    const j = firstShown(msgs), f = document.createDocumentFragment();
    if (j > 0) f.appendChild(earlierBtn());
    drawMsgs(f, msgs.slice(j, i), all, msgs, (C.chat && C.chat.waiting) || []);
    const had = document.activeElement === b;
    b.replaceWith(f);
    if (n) { const d = n.getBoundingClientRect().top - y; if (Math.abs(d) >= 1) setTop(scroller().scrollTop + d); }
    if (had) { const nb = $("t-thread").querySelector(".t-earlier"); if (nb) nb.focus({ preventScroll: true }); }  // the keyboard's place
    V.anchor = anchorNow();  // where it is now, so the ResizeObserver's restore() moves nothing
    sizeSpacer(); syncDown();
  }
  function nearTop() {  // once the view is still or at the top: never under the user's finger or mid-glide (iOS would stop it)
    clearTimeout(V.earlierT);
    V.earlierT = setTimeout(() => {
      if (!talkShown() || !C.chat || !$("t-thread").querySelector(".t-earlier")) return;
      if (V.held || (Date.now() - V.moved < 150 && scroller().scrollTop > 50)) { nearTop(); return; }
      if (scroller().scrollTop < EARLIER_PX) earlier();
    }, 150);
  }
  // what light() left on the Mac for a message, fetched once when the user opens a step or its thinking; OUTS: each
  // step's output this page has, so a step the user keeps open shows it again at once after a redraw
  const FULL = new Map(), OUTS = new Map();
  function fullOf(m) {
    const k = `${C.id}:${m.id}`;
    if (!FULL.has(k)) {
      const p = call("GET", `/studio/api/chats/${C.id}/messages/${m.id}`).then((r) => r.meta || {});
      FULL.set(k, p); p.catch(() => FULL.delete(k));
      if (FULL.size > 40) FULL.delete(FULL.keys().next().value);
    }
    return FULL.get(k);
  }
  function fillOnOpen(d, node, load) {
    const go = () => {
      if (!d.open || d.dataset.filled) return;
      d.dataset.filled = "1"; node.textContent = "Loading\u2026";
      load().then((s) => { node.textContent = s; })
        .catch((e) => { delete d.dataset.filled; node.textContent = `Couldn't load this: ${e.message}`; });
    };
    d.addEventListener("toggle", go);
    if (d.open) queueMicrotask(go);  // opened before it was drawn (a step the user keeps open)
  }
  // a compaction: the turns above it stay, the model reads this summary in their place
  function compactDivider(s, pending) {
    const d = el("details", "t-compact" + (pending ? " pending" : ""));
    const sm = el("summary", "");
    sm.appendChild(el("span", "t-compact-t", pending ? "Compacting the conversation…" : "Conversation compacted"));
    if (s && s.meta && s.meta.covered) sm.appendChild(el("span", "t-compact-n", plural(s.meta.covered, "earlier message")));
    d.appendChild(sm);
    if (s && s.content) {
      const body = el("div", "t-compact-body");
      body.appendChild(el("p", "t-compact-note", "The model reads this summary in place of the messages above. They stay here, unchanged."));
      const md = el("div", "msg-body"); MD.render(md, s.content, { onCopy: copy }); body.appendChild(md);
      d.appendChild(body);
    }
    return d;
  }
  function attRow(atts) {
    const row = el("div", "t-att-row");
    let files = atts || [];
    const photos = files.filter((x) => x.kind === "image");
    if (photos.length) {  // a message's photos: their thumbnails
      const r = el("div", "att-row");
      photos.forEach((a) => r.appendChild(C.chat && C.chat.private ? privThumb(a) : K.attThumb(K.refOf(a.id, C.chat) || a, null)));
      row.appendChild(r);
    }
    for (const a of files.filter((x) => x.kind !== "image")) {
      const f = el(a.doc ? "a" : "span", "t-file");
      if (a.doc) docLink(f, { id: a.id, name: a.name, kind: a.doc }, a.doc !== "pdf");  // kept whole: it opens (chat_docs)
      f.appendChild(svg(a.kind === "text" ? IC.file : K.ICON[a.kind] || K.ICON.image));
      f.appendChild(el("span", "t-file-n", a.name || (a.kind === "text" ? "file" : a.kind)));
      if (a.detail || (a.kind === "text" && a.chars)) f.appendChild(el("small", "", a.detail || chars(a.chars)));
      row.appendChild(f);
    }
    return row;
  }
  function teamMsg(m, waiting) {  // a team update from the chat's agents (chat_team.py): it wakes the lead
    const d = el("details", "msg t-team-msg" + (waiting ? " waiting" : "")); d.dataset.id = m.id;
    const tm = m.meta.team || {}, all = [...new Set([...(tm.agents || []), ...(tm.checkins || [])])], ids = all.join(", ");
    const sm = el("summary", "t-team-msg-h");
    sm.append(svg('<circle cx="9" cy="8" r="3"/><circle cx="17" cy="9" r="2.4"/><path d="M3.5 19c.6-3 2.8-5 5.5-5s4.9 2 5.5 5"/>', "t-team-ic"),
      el("span", "", waiting ? `Team update for the lead (${ids}) · waiting` : `Team update (${ids})`));
    d.appendChild(sm);
    d.appendChild(el("div", "t-team-msg-b", String(m.content || "").replace(/^Team update \(from your agents, not from the user\):\s*/, "")));
    const open = el("button", "textbtn t-team-msg-open", "Open agents"); open.type = "button";
    open.addEventListener("click", () => openAgents(all.length === 1 ? all[0] : null));
    d.appendChild(open);
    return d;
  }
  function goalMsg(m, waiting) {  // a goal's own message (chat_goal.py): it starts the chat's next turn by itself
    const d = el("details", "msg t-team-msg" + (waiting ? " waiting" : "")); d.dataset.id = m.id;
    const sm = el("summary", "t-team-msg-h");
    sm.append(svg('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>', "t-team-ic"),
      el("span", "", (m.meta.goal === "start" ? "Goal set" : "Goal: not met yet, working on") + (waiting ? " · waiting" : "")));
    d.appendChild(sm);
    const c = String(m.content || "");
    d.appendChild(el("div", "t-team-msg-b", m.meta.goal === "start" ? c.split("\n\n(This is the goal for this chat")[0] : c));
    if (m.meta.goal === "start") d.open = true;
    return d;
  }
  function userMsg(m, last) {
    if (m.meta.team) return teamMsg(m);
    if (m.meta.goal) return goalMsg(m);
    const w = el("div", "msg msg-user t-user"); w.dataset.id = m.id;
    if ((m.meta.attachments || []).length) w.appendChild(attRow(m.meta.attachments));
    if (m.content) w.appendChild(el("div", "bubble", m.content));
    if (last && !C.streaming && !(C.chat && C.chat.streaming)) {
      const row = el("div", "t-acts t-acts-user");
      row.appendChild(act(IC.edit, "Edit and resend", () => editLast(w, m)));
      row.appendChild(act(IC.copy, "Copy", (b) => copy(m.content || "", b)));
      w.appendChild(row);
    }
    return w;
  }
  function act(paths, label, fn) {
    const b = el("button", "t-act"); b.type = "button"; b.setAttribute("aria-label", label); b.title = label;
    b.appendChild(svg(paths));
    b.addEventListener("click", () => fn(b));
    return b;
  }
  function thinkBlock(text, secs, live, more) {
    const d = el("details", "t-think");
    if (live) d.open = true;
    d.appendChild(el("summary", "", live ? "Thinking" : secs ? `Thought for ${secs < 1 ? "under a second" : plural(Math.round(secs), "second")}` : "Thoughts"));
    const b = el("div", "t-think-body", text);
    d.appendChild(b);
    if (more) fillOnOpen(d, b, more);
    return d;
  }
  function asstMsg(m, last, versions) {
    const w = el("div", "msg msg-asst t-asst"); w.dataset.id = m.id;
    const meta = m.meta || {};
    // the cursor only on the answer being written now; meta.partial without a running turn is an answer a restart cut
    // off before it could be closed
    const partial = !!(last && C.chat && C.chat.streaming), cut = !!meta.partial && !partial;
    const lazy = !!meta.light;  // light(): its steps' output and its thinking come from the Mac when opened
    if (meta.reasoning || meta.reasoning_n) w.appendChild(thinkBlock(meta.reasoning, meta.ttft_s, partial && !m.content, lazy ? () => fullOf(m).then((f) => f.reasoning || "") : null));
    const body = el("div", "msg-body t-body");
    if ((meta.parts || []).length) {  // a turn with tools (chat_tools): its text, steps, notes and taps in the order they came
      for (const [i, p] of meta.parts.entries()) {
        if (p.text) { const b = el("div", "msg-body t-body"); MD.render(b, p.text, { onCopy: copy }); w.appendChild(b); }
        else if (p.tool) w.appendChild(stepBlock(p.tool, lazy ? () => fullOf(m).then((f) => ((f.parts || [])[i] || {}).tool || {}) : null));
        else if (p.note) w.appendChild(el("p", "t-note t-step-note", p.note));
        else if (p.ask) w.appendChild(tapCard(Object.assign({}, p.ask, { open: p.ask.open && !!(C.chat && C.chat.ask && C.chat.ask.id === p.ask.id) })));
      }
      if (partial) w.appendChild(el("span", "caret"));
    } else {
      if (m.content) MD.render(body, m.content, { onCopy: copy });
      if (partial) body.appendChild(el("span", "caret"));
      w.appendChild(body);
    }
    if (cut && !meta.error) w.appendChild(el("p", "t-note", "This answer was cut off."));
    if (meta.error) w.appendChild(el("p", "t-err", meta.error));
    if (meta.stopped && !meta.error) w.appendChild(el("p", "t-note", "Stopped."));
    if (meta.dropped) w.appendChild(el("p", "t-note", `The oldest ${plural(meta.dropped, "message")} did not fit the model's context and were left out.`));
    if (!partial) {
      const row = el("div", "t-acts");
      if (m.content) row.appendChild(act(IC.copy, "Copy", (b) => copy(m.content, b)));
      let retry = last && !C.streaming;
      if (retry) row.appendChild(act(IC.retry, "Retry", () => send("regenerate")));
      if (versions) row.appendChild(versionRow(versions));
      if (meta.speed && (meta.speed.tps || meta.speed.pp)) {  // its decode and prompt speeds (the Speed sheet has the rest)
        const sb = el("button", "t-speed", speedLine(meta.speed)); sb.type = "button";
        sb.setAttribute("aria-label", `Speed: ${speedLine(meta.speed)}. Details`);
        sb.addEventListener("click", () => openSpeed(m));
        row.appendChild(sb);
      }
      const who = [meta.model, effortOf(meta.effort) ? effortOf(meta.effort).name : null, photoWords(meta)].filter(Boolean).join(" · ");
      if (who) row.appendChild(el("span", "t-who", who));
      w.appendChild(row);
    }
    return w;
  }
  function versionRow(v) {
    const box = el("span", "t-vers");
    const i = v.all.indexOf(v.cur);
    const go = async (d) => {
      const n = v.all[i + d];
      if (n === undefined) return;
      try { await call("PUT", `/studio/api/chats/${C.id}/version`, { group: v.group, version: n }); } catch (e) { K.toast(e.message, true); }
      reload();
    };
    const prev = el("button", "t-act t-ver", "‹"); prev.type = "button"; prev.disabled = i <= 0; prev.setAttribute("aria-label", "Previous version");
    const next = el("button", "t-act t-ver", "›"); next.type = "button"; next.disabled = i >= v.all.length - 1; next.setAttribute("aria-label", "Next version");
    prev.addEventListener("click", () => go(-1)); next.addEventListener("click", () => go(1));
    box.append(prev, el("span", "t-ver-n", `${i + 1}/${v.all.length}`), next);
    return box;
  }
  function waitingMsg(w) {
    if (w.meta.team) return teamMsg(w, true);
    if (w.meta.goal) return goalMsg(w, true);
    const box = el("div", "msg msg-user t-user waiting");
    box.dataset.wait = w.id;
    if ((w.meta.attachments || []).length) box.appendChild(attRow(w.meta.attachments.map((a) => (
      K.refOf(a.id) || a))));
    if (w.content) box.appendChild(el("div", "bubble", w.content));
    const meta = el("div", "t-acts t-acts-user");
    const tw = el("span", "t-wait", w.meta.steer && busyNow() ? steerWords(w) : `Waiting: ${whyWords().short}`);
    if (!(w.meta.steer && busyNow())) tw.dataset.why = "1";  // syncBar keeps it current (it said "sends when you switch" while loading)
    meta.appendChild(tw);
    const x = el("button", "textbtn t-cancel", "Cancel"); x.type = "button"; x.setAttribute("aria-label", "Cancel this waiting message");
    x.addEventListener("click", async () => {
      try { await call("DELETE", `/studio/api/waiting/${w.id}`); } catch (e) { K.toast(e.message, true); return; }
      box.remove(); if (C.chat) C.chat.waiting = (C.chat.waiting || []).filter((y) => y.id !== w.id);
      // the user's words come back to the composer (Cancel deleted them)
      const t = $("t-prompt"); if (w.content && !t.value.trim()) { t.value = w.content; autosize(); syncComposer(); t.focus(); }
      reload();
    });
    meta.appendChild(x);
    if (w.meta.steer && busyNow()) {  // Send now: the answer stops where it
      // is, what it did stays, and this message goes at once on its own (the outbox sends it once the chat is free); one a
      // step took in just before is being answered already, so then nothing stops
      const now = el("button", "textbtn t-sendnow", "Send now"); now.type = "button";
      now.setAttribute("aria-label", "Stop the answer and send this now");
      now.addEventListener("click", async () => {
        now.disabled = true;
        try { await call("POST", `/studio/api/waiting/${w.id}/now`); } catch (e) { now.disabled = false; K.toast(e.message, true); }
      });
      meta.appendChild(now);
    }
    box.appendChild(meta);
    return box;
  }
  // where a message sent while it answers goes in: after the step it is on when the chat has tools and it is words only
  function steerWords(w) {
    const tools = C.chat && (C.chat.perm || "none") !== "none";
    const words = !(w.meta.attachments || []).length && !w.meta.no_tools;
    return tools && words ? "Waiting: it reads this after its current step" : "Waiting: sends when this answer ends";
  }
  function editLast(w, m) {
    const box = el("div", "edit-box t-edit");
    const ta = el("textarea"); ta.value = m.content; ta.setAttribute("aria-label", "Edit your message");
    const row = el("div", "row");
    const cancel = el("button", "textbtn", "Cancel"); cancel.type = "button";
    const go = el("button", "go solid", "Send"); go.type = "button";
    cancel.addEventListener("click", render);
    go.addEventListener("click", () => send("edit", ta.value));
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send("edit", ta.value); } if (e.key === "Escape") render(); });
    row.append(cancel, go); box.append(ta, row);
    w.replaceChildren(box); ta.focus();
  }

  // ---------------------------------------------------------------- why a message waits (the same words in the bar and the thread)
  // the model the chat's next message needs
  function wantOf() {
    const m = modelOf(curModel());
    return { name: m ? m.name : "the chat's model", id: m && m.model_id, profile: curModel() };
  }
  function whyWords() {
    const st = K.state() || {}, l = st.llm || {}, W = wantOf();
    const want = W.name, wantId = W.id;
    if (l.model_id === wantId && ["waiting", "loading", "linking"].includes(l.state)) return { short: `${want} is loading`, long: `Loading ${want} for your message.` };
    if (l.model_id === wantId && l.state === "ready") return { short: "sending", long: "" };
    if (l.connected && l.state === "failed") return { short: `StrixFold isn't answering on ${l.connected}`, long: `${l.reason} Your message goes when it answers.` };
    // a load that failed says so, with Try again (it kept saying "Loading … about a minute")
    if (l.state === "failed") return { short: `${want} didn't load`, long: `${want} didn't load${l.reason ? `: ${l.reason}` : ""}. Try again, or check Boxes.`, retry: true };
    if (l.state && ["ready", "waiting", "loading", "linking"].includes(l.state)) {
      // another model is loaded: what a switch would stop, or what stands in its way (GET chat/switch, the server's check)
      const loaded = l.model_name || "Another model", sw = C.sw && C.sw.profile === W.profile ? C.sw : null;
      loadSwitch();
      if (sw && sw.blockers.length) return { short: `sends after you switch to ${want}`, long: `Not yet: ${sw.blockers.join("; ")}. Switch to ${want} once that is done.` };
      const more = sw && sw.waits.length ? ` ${sw.waits.join("; ")}.` : "";
      return { short: `sends when you switch to ${want}`, long: `${loaded} is loaded. Switch to ${want}? That stops ${loaded} until something switches back.${more}`, switch: true };
    }
    return { short: `${want} is loading`, long: `Loading ${want} for your message. The first answer takes about a minute.` };
  }
  async function loadSwitch() {  // true when it asked the server (the sheet re-renders only then: R1-01, a loop froze the tab)
    const pid = wantOf().profile;
    if (C.swBusy || (C.sw && C.sw.profile === pid && Date.now() - C.swT < 2500)) return false;
    // a failed check waits before the next, longer each time (with the Mac away, syncBar → whyWords →
    // loadSwitch ran ~6,000 checks a second, and any error status would have done the same against the live server)
    if (C.swFailT && Date.now() - C.swFailT < Math.min(30000, 2000 * 2 ** Math.min(4, (C.swFails || 1) - 1))) return false;
    C.swBusy = true;
    let asked = false;
    try {
      C.sw = Object.assign(await call("GET", `/studio/api/chat/switch?profile=${encodeURIComponent(pid)}`), { profile: pid });
      C.swT = Date.now(); C.swFails = 0; C.swFailT = 0; asked = true;
    } catch (e) { C.swFails = (C.swFails || 0) + 1; C.swFailT = Date.now(); /* the bar keeps its words */ }
    C.swBusy = false;
    syncBar();
    return asked;
  }
  function syncBar() {
    const bar = $("t-bar"), go = $("t-bar-go");
    const waits = ((C.chat && C.chat.waiting) || []).length;
    const w = whyWords();
    const rb = C.retryBar && C.retryBar.chat === C.id && w.short !== "sending" ? C.retryBar : null;  // Retry's own Switch
    if (!rb) C.retryBar = null;
    document.querySelectorAll("#t-thread .t-wait[data-why]").forEach((x) => { const t = `Waiting: ${w.short}`; if (x.textContent !== t) x.textContent = t; });
    if ((!waits && !rb) || !w.long) { bar.hidden = true; return; }
    bar.hidden = false;
    $("t-bar-text").textContent = rb ? `${w.long} Then press ${rb.again} again.` : w.long;
    go.hidden = !w.switch && !w.retry;
    $("t-bar-label").textContent = w.retry ? "Try again" : "Switch";
  }
  $("t-bar-go").addEventListener("click", async () => {
    const W = wantOf();
    try {
      await call("POST", "/studio/api/chat/model/start", { profile: W.profile, switch: true });
      K.toast(`Loading ${W.name}`);
    } catch (e) { K.toast(e.message, true); }
    K.poll();
  });

  // ---------------------------------------------------------------- the composer
  const busyNow = () => !!(C.streaming || (C.chat && C.chat.streaming));
  // a message typed while the chat answers: it waits on the Mac and
  // the model reads it after the step it is on (a chat with tools), or it goes when the answer ends
  const steerable = () =>
    busyNow() && !!($("t-prompt").value.trim() || C.atts.length);
  function syncComposer() {
    const busy = busyNow(), steer = steerable(), asStop = busy && !steer;
    const b = $("t-send");
    let working = busy;
    b.classList.toggle("stop", asStop);
    b.setAttribute("aria-label", asStop ? "Stop" : steer ? "Send now: it reads this after its current step" : "Send");
    b.disabled = !asStop && !$("t-prompt").value.trim() && !C.atts.length;
    $("t-stop").hidden = !steer;
    $("t-composer").classList.toggle("steering", steer);
    const mic = $("t-mic");
    if (!mic.classList.contains("rec") && !mic.classList.contains("busy")) $("t-prompt").placeholder = working ? "Message it while it works" : idleHolder();
    syncChip(); syncBar();
  }
  function syncChip() {
    const m = modelOf(curModel()), e = effortOf(curEffort());
    $("t-model-name").textContent = m ? m.name : "DeepSeek V4.1";
    $("t-model-short").textContent = ({ dsv41: "V4.1", "dsv41-unc": "Unc.", qwen38: "Qwen 3.8", "qwen38-kyojin": "Qwen 3.8", "qwen38-gufo21": "Qwen 3.8", "qwen38-halogen": "Qwen 3.8" })[m && m.model_id] || (m ? m.name : "V4.1");  // a phone's row: the context chip beside it
    $("t-model-effort").textContent = e ? e.name : "Low";
    $("t-model").setAttribute("aria-label", `Model: ${m ? m.name : ""}, effort ${e ? e.name : ""}. Change`);
    const z = sizeOf(curModel());
    $("t-ctx-k").textContent = z ? z.k : "64K";
    $("t-ctx").setAttribute("aria-label", `Context window: ${z ? z.k : "64K"}. Change`);
    syncRing(z);
    syncPerm();
  }
  // attention: while this page is out of sight, the tab's title says an answer is
  // ready or a tap waits, and a notification says so where the browser allows one (asked once from the ⋯ menu). A tap
  // nobody sees holds the turn for up to 10 minutes and looks like an answer that died after a tool call
  const ATTN = { base: null };
  function attention(what) {
    if (!document.hidden) return;
    const words = what === "tap" ? "Waiting for your tap" : "Answer ready";
    if (ATTN.base === null) ATTN.base = document.title;
    document.title = `${what === "tap" ? "●" : "✓"} ${words} · StrixFold UI`;
    try {
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification(words, { body: isPriv() ? "A private chat" : (C.chat && C.chat.title) || "Chat", tag: `halo-chat-${C.id || ""}` });
      }
    } catch (e) { /* this browser shows them only from a push (an iPhone's installed app): the title still says it */ }
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && ATTN.base !== null) { document.title = ATTN.base; ATTN.base = null; }
  });
  // the context tracker: a ring in the context chip that fills with what this chat takes of its window (its last
  // answer's own count, else an estimate: C.chat.window), and turns warm, then clay, as it nears COMPACT_AT
  // (halo_chat.py, 75%), where older turns become a summary
  const RING_C = 2 * Math.PI * 7.5;
  function syncRing(z) {
    const r = $("t-ring"), w = C.id && C.chat ? C.chat.window : null;
    const top = $("t-ctx-top");  // in both pages' top bars (index.html, studio-shell.html); never assumed
    if (!w || !w.context) {
      r.toggleAttribute("hidden", true); $("t-ctx").removeAttribute("title"); $("t-ctxwarn").hidden = true;
      $("t-ctx-pct").textContent = ""; $("t-ctx").classList.add("no-use"); if (top) top.hidden = true; return;
    }
    const f = Math.max(0, Math.min(1, (w.used || 0) / w.context)), pct = Math.round(f * 100);
    r.toggleAttribute("hidden", false);
    // a phone has no hover for the title, and no room for the window's size: the chip shows how full it is; while it
    // answers, the same on every screen
    $("t-ctx-pct").textContent = `${pct}%`; $("t-ctx").classList.remove("no-use");
    $("t-ring-fg").style.strokeDasharray = `${(f * RING_C).toFixed(2)} ${RING_C.toFixed(2)}`;
    r.classList.toggle("warm", f >= 0.5 && f < 0.65); r.classList.toggle("full", f >= 0.65 && f < 0.9); r.classList.toggle("over", f >= 0.9);
    if (top) {  // the phone's: the same ring in the top bar (the composer's bar has no room there)
      $("t-ring-top-fg").style.strokeDasharray = $("t-ring-fg").style.strokeDasharray;
      ["warm", "full", "over"].forEach((k) => $("t-ring-top").classList.toggle(k, r.classList.contains(k)));
      $("t-ctx-top-pct").textContent = `${pct}%`; top.hidden = C.page !== "talk";
    }
    const of = w.loaded && w.loaded !== w.chosen ? kOf(w.loaded) : (z ? z.k : kOf(w.context));
    const words = `${kOf(w.used || 0)} of ${of} used (${pct}%), ${kOf(Math.max(0, w.context - (w.used || 0)))} left${w.measured ? "" : ", estimated"}${w.compacting ? ", compacting now" : ""}. Older turns are summarised near 75%.`;
    $("t-ctx").title = `Context: ${words}`;
    if (top) top.setAttribute("aria-label", `Context: ${words} Change`);
    $("t-ctx").setAttribute("aria-label", `Context window ${of}: ${words} Change`);
    // 90% or more: a warning above the bar, with Compact now while nothing runs
    const warn = $("t-ctxwarn"), on = f >= 0.9 && !w.compacting;
    if (warn.hidden === on) warn.hidden = !on;
    if (on) {
      $("t-ctxwarn-t").textContent = `This chat is ${pct}% full. ${busyNow() ? "StrixFold UI compacts earlier steps as it works." : "StrixFold UI summarises the older turns by itself when you send."}`;
      $("t-ctxwarn-go").hidden = busyNow();
    }
  }
  function autosize() {
    const t = $("t-prompt");
    t.style.height = "auto";
    if (!t.value || !t.scrollHeight) { t.style.height = ""; return; }
    t.style.height = Math.min(t.scrollHeight, window.innerHeight * 0.35) + "px";
  }
  $("t-prompt").addEventListener("input", () => { autosize(); syncComposer(); });
  $("t-prompt").addEventListener("keydown", (e) => {
    const touch = window.matchMedia("(hover: none)").matches;  // on a phone, Return is a new line; the button sends
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !touch) { e.preventDefault(); if (steerable()) sendWhileBusy(); else if (!C.streaming) send("send"); }
  });
  $("t-composer").addEventListener("submit", (e) => { e.preventDefault(); if (steerable()) sendWhileBusy(); else if (busyNow()) stop(); else send("send"); });
  $("t-stop").addEventListener("click", () => stop());
  // dictation: tap to record, tap to stop; the Mac turns it into text (mlx-whisper, studio_transcribe.py) and the text
  // lands in the composer to edit. It is never sent by itself, and the recording is deleted on the Mac once read.
  const MIC = { rec: null, chunks: [], t0: 0, timer: 0, busy: false };
  function micUI(state) {
    const b = $("t-mic"), t = $("t-prompt");
    b.classList.toggle("rec", state === "rec"); b.classList.toggle("busy", state === "busy");
    b.setAttribute("aria-pressed", String(state === "rec"));
    b.setAttribute("aria-label", state === "rec" ? "Stop recording" : state === "busy" ? "Transcribing" : "Dictate");
    if (state === "rec") { const s = Math.floor((Date.now() - MIC.t0) / 1000); t.placeholder = `Listening… ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}. Tap the microphone to stop.`; }
    else t.placeholder = state === "busy" ? "Turning your words into text…" : idleHolder();
    $("t-status").textContent = state === "rec" ? "Recording" : state === "busy" ? "Transcribing" : "";
  }
  async function micStart() {
    if (!window.isSecureContext) { K.toast("The microphone works over StrixFold UI's secure link (https) or on the Mac itself.", true); return; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      K.toast("This browser cannot record here. Use the keyboard's dictation instead.", true); return;
    }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) {
      K.toast(e && e.name === "NotAllowedError"
        ? "StrixFold UI needs the microphone. Allow it for this site (Safari: aA › Website Settings › Microphone), then tap the microphone again."
        : "No microphone could be opened.", true);
      return;
    }
    const type = ["audio/mp4", "audio/aac", "audio/webm;codecs=opus", "audio/webm"].find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
    let rec;
    try { rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined); }
    catch (e) { stream.getTracks().forEach((tr) => tr.stop()); K.toast("This browser cannot record here. Use the keyboard's dictation instead.", true); return; }
    MIC.rec = rec; MIC.chunks = []; MIC.t0 = Date.now();
    rec.ondataavailable = (e) => { if (e.data && e.data.size) MIC.chunks.push(e.data); };
    rec.onstop = () => { stream.getTracks().forEach((tr) => tr.stop()); micFinish(rec.mimeType || type || "audio/mp4"); };
    rec.start(1000);
    micUI("rec");
    MIC.timer = setInterval(() => { if (Date.now() - MIC.t0 > 120000) micStop(); else micUI("rec"); }, 500);
    call("POST", "/studio/api/transcribe/warm", {}).catch(() => { /* the transcription itself says what is wrong */ });
  }
  function micStop() { clearInterval(MIC.timer); if (MIC.rec && MIC.rec.state !== "inactive") MIC.rec.stop(); }
  // the browser decodes its own recording (Safari's AAC, Chrome's Opus) and sends 16 kHz mono 16-bit WAV: the Mac reads
  // that whatever the browser recorded in
  async function toWav16k(buf) {
    const AC = window.AudioContext || window.webkitAudioContext, OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    const ac = new AC();
    let audio;
    try { audio = await new Promise((res, rej) => { const p = ac.decodeAudioData(buf, res, rej); if (p && p.then) p.then(res, rej); }); }
    finally { if (ac.close) ac.close(); }
    const n = Math.max(1, Math.ceil(audio.duration * 16000));
    const off = new OAC(1, n, 16000);
    const src = off.createBufferSource(); src.buffer = audio; src.connect(off.destination); src.start(0);
    const out = await new Promise((res, rej) => { off.oncomplete = (e) => res(e.renderedBuffer); const p = off.startRendering(); if (p && p.then) p.then(res, rej); });
    const pcm = out.getChannelData(0), wav = new DataView(new ArrayBuffer(44 + pcm.length * 2));
    const str = (o, t) => { for (let i = 0; i < t.length; i++) wav.setUint8(o + i, t.charCodeAt(i)); };
    str(0, "RIFF"); wav.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE"); str(12, "fmt "); wav.setUint32(16, 16, true);
    wav.setUint16(20, 1, true); wav.setUint16(22, 1, true); wav.setUint32(24, 16000, true); wav.setUint32(28, 32000, true);
    wav.setUint16(32, 2, true); wav.setUint16(34, 16, true); str(36, "data"); wav.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) wav.setInt16(44 + i * 2, Math.max(-1, Math.min(1, pcm[i])) * 0x7fff, true);
    return new Blob([wav.buffer], { type: "audio/wav" });
  }
  async function micFinish(mime) {
    MIC.rec = null; MIC.busy = true; micUI("busy");
    let blob = new Blob(MIC.chunks, { type: mime.split(";")[0] }); MIC.chunks = [];
    try { blob = await toWav16k(await blob.arrayBuffer()); } catch (e) { /* sent as recorded; the Mac tries afconvert */ }
    try {
      const r = await fetch("/studio/api/transcribe", { method: "POST", body: blob, credentials: "same-origin",
        headers: { "Content-Type": blob.type || "audio/mp4", "X-Halo-UI-Token": K.TOKEN } });
      const d = await r.json().catch(() => ({}));
      if (r.status === 401) { K.locked(); return; }
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      const words = (d.text || "").trim();
      if (!words) { K.toast("Didn't catch anything. Try again a little closer to the microphone."); return; }
      const t = $("t-prompt");
      t.value = t.value ? t.value.replace(/\s*$/, " ") + words : words;
      autosize(); syncComposer();
      t.focus(); t.setSelectionRange(t.value.length, t.value.length);
    } catch (e) { K.toast(`Dictation: ${e.message}`, true); }
    finally { MIC.busy = false; micUI(""); }
  }
  $("t-mic").addEventListener("click", () => { if (MIC.busy) return; if (MIC.rec) micStop(); else micStart(); });

  /* attsend: pure (tests/test_studio.py runs this block in node) */
  // what goes with a message: every attachment that finished uploading (text files, photos, sounds and videos; the
  // Mac gives the model what it can take of each: Kyojin's Qwen looks at a photo, the others read its text)
  function sendable(atts) { return (atts || []).filter((a) => a && a.id && !a.uploading).map((a) => ({ id: a.id })); }
  // the composer's line under photos, sounds or videos, for the chat's model (its "vision" from /studio/api/chat/models)
  function attNote(model, media) {
    const name = (model && model.name) || "This model", photos = (media || []).filter((a) => a.kind === "image");
    const heard = (media || []).filter((a) => a.kind !== "image"), one = photos.length === 1;
    const out = [];
    const blank = photos.length && photos.every((a) => /no text found/.test(a.detail || ""));
    const looked = photos.length && photos.every((a) => a.look === "seen" || a.look === "looking");  // described on the Mac (look.py)
    if (photos.length && model && model.vision) out.push(`${name} looks at ${one ? "the photo" : "the photos"} itself.`);
    else if (photos.length && model && model.model_id === "dsv41-unc") out.push(`A vision assistant examines ${one ? "your photo" : "your photos"} for your question and shares its findings with DeepSeek.`);
    else if (looked) out.push(`${name} reads a description of ${one ? "this photo" : "these photos"} made on your Mac.`);
    else if (blank) out.push(`${name} can't see photos, and no text was found in ${one ? "this one" : "these"}. Qwen 3.8 (Kyojin) can look at photos.`);
    else if (photos.length) out.push(`${name} can't see photos: it gets the text read from ${one ? "it" : "them"}. Qwen 3.8 (Kyojin) can look at photos.`);
    const who = photos.length ? "It" : name, rec = heard.length === 1 ? "the recording" : "the recordings";
    if (heard.length) out.push(heard.every((a) => /words/.test(a.detail || "")) ? `${who} gets what was said in ${rec}.`
      : `${who} gets only the details of ${rec}: no speech was read.`);
    return out.join(" ");
  }
  // an answer's line beside the model: whether it saw the photos or only their text (the server decides by the engine
  // that served it: halo_chat.sees_images)
  function photoWords(meta) {
    const n = (meta && meta.pictures) || 0, t = (meta && meta.photo_text) || 0, d = (meta && meta.described) || 0;
    if (n) return `saw ${n === 1 ? "1 photo" : n + " photos"}`;
    if (d) return `read ${d === 1 ? "a description of the photo" : `descriptions of ${d} photos`}` + (t ? `; ${t === 1 ? "one photo's" : t + " photos'"} text only` : "");
    return t ? `read ${t === 1 ? "the photo's" : "the photos'"} text only` : null;
  }
  // a photo's chip while the Mac's vision model describes it, and once it has
  function lookWords(a) { return a && a.look === "looking" ? "Looking…" : a && a.look === "seen" ? "Seen on your Mac" : null; }
  /* attsend: end */
  // attachments: any file; the Mac reads it as text for the model (filetext.py: a workbook as tables, a PDF by its text
  // or OCR, a picture by OCR, anything else by its details)
  $("t-file").addEventListener("change", (e) => { addAny([...e.target.files]); e.target.value = ""; });
  $("t-prompt").addEventListener("paste", (e) => { const f = [...(e.clipboardData && e.clipboardData.files) || []]; if (f.length) { e.preventDefault(); addAny(f); } });
  const addAny = (files) => (
    addFiles(files));
  async function addFiles(files) {
    for (const f of files) {
      const media = /^(image|audio|video)\//.test(f.type);
      // a Private chat's file belongs to that chat alone (it is saved sealed with it): `priv` is the chat it went to
      const a = { name: f.name, kind: media ? f.type.split("/")[0] : "text", uploading: true, priv: C.chat && C.chat.private ? C.id : null };
      C.atts.push(a); renderAtts(); syncComposer();
      try {
        const r = await fetch("/studio/api/uploads", { method: "POST", body: f, credentials: "same-origin",
          headers: { ...K.privHeaders(), ...(C.chat && C.chat.private ? { "X-Private": "1" } : {}),  // never saved without its Private chat
            "Content-Type": f.type || "application/octet-stream", "X-Halo-UI-Token": K.TOKEN, "X-Filename": encodeURIComponent(f.name),
            "X-Chat": C.id || "", "X-Upload-For": "chat", "X-Model": curModel() } });
        const d = await r.json();
        if (r.status === 401) { K.locked(); C.atts = C.atts.filter((x) => x !== a); renderAtts(); syncComposer(); return; }  // add it again after unlocking
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        Object.assign(a, d, { uploading: false, name: f.name });
        if (a.look === "looking") watchLook(a);
      } catch (err) { K.toast(`${f.name}: ${err.message}`, true); C.atts = C.atts.filter((x) => x !== a); }
      renderAtts(); syncComposer();
    }
  }
  // a photo being described on the Mac (look.py, ~10 s): its chip follows until the description is ready (4 min at most)
  async function watchLook(a) {
    for (let i = 0; i < 160 && a.look === "looking" && C.atts.includes(a); i++) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        const r = await fetch(`/studio/api/uploads/${encodeURIComponent(a.id)}/look`, { credentials: "same-origin" });
        if (!r.ok) return;
        a.look = (await r.json()).look;
      } catch (e) { return; }
      renderAtts();
    }
  }
  function renderAtts() {
    const box = $("t-atts");
    box.replaceChildren();
    C.atts.forEach((a, i) => {
      const chip = el("div", "s-chip t-chip" + (a.uploading ? " up" : ""));
      chip.appendChild(svg(a.kind === "text" ? IC.file : K.ICON[a.kind] || K.ICON.image, "t-chip-ic"));
      const body = el("span", "s-chip-b");
      body.appendChild(el("span", "s-chip-n", a.uploading ? "Uploading…" : a.name || "file"));
      if (!a.uploading) body.appendChild(el("small", "t-chip-k", [lookWords(a), a.detail || (a.kind === "text" ? chars(a.chars || 0) : "Text models cannot see this")].filter(Boolean).join(" · ")));
      const x = el("button", "s-chip-x", "×"); x.type = "button"; x.setAttribute("aria-label", `Remove ${a.name || "file"}`);
      x.addEventListener("click", () => { C.atts.splice(i, 1); renderAtts(); syncComposer(); $("t-prompt").focus(); });
      chip.append(body, x);
      box.appendChild(chip);
    });
    const media = C.atts.filter((a) => a.kind !== "text" && !a.uploading && a.id);
    if (media.length) {  // a picture, sound or video: what the chat's model gets from it
      const n = el("p", "s-att-note t-att-note");
      n.appendChild(document.createTextNode(attNote(modelOf(curModel()), media) + " "));
      box.appendChild(n);
    }
  }

  const idleHolder = () => (
    "Message StrixFold UI");
  function syncInput() {
    if (!MIC.rec && !MIC.busy) $("t-prompt").placeholder = idleHolder();
    renderAtts(); syncComposer();
  }

  // ---------------------------------------------------------------- sending and streaming
  const blank = () => ({ waiting: [], messages: [], refs: {},
  });
  async function ensureChat() {
    if (C.id) return C.id;
    let c;
    try {
      c = await call("POST", "/studio/api/chats", Object.assign({ kind: "chat", model: curModel(), effort: curEffort(), project_id: C.newProject || null,
      }, C.newPermBody || {}));
    } catch (e) {  // Full refused after the chat was made (a folder refused): the chat stays, at No tools; the message waits
      if (e.data && e.data.chat) { C.id = e.data.chat.id; C.chat = Object.assign(e.data.chat, blank());
        C.newPerm = null; C.newPermBody = null; history.replaceState(null, "", `#/talk/${C.id}`); syncPerm(); K.loadList(); }
      throw e;
    }
    C.id = c.id; C.chat = Object.assign(c, blank()); C.newProject = null;
    C.newPerm = null; C.newPermBody = null;
    if (C.newTeam) {  // the Team chip set before the chat existed (chat_team.py)
      try { C.chat.team = await call("PUT", `/studio/api/chats/${c.id}/team`, { cap: C.newTeam }); } catch (e) { K.toast(e.message, true); }
      C.newTeam = null;
    }
    history.replaceState(null, "", `#/talk/${c.id}`);
    return c.id;
  }
  async function send(mode, text) {
    mode = mode || "send";
    if (C.streaming) return;
    if (!C.chat && C.lockedId) { const id = C.lockedId; openUnlock(() => open(id)); return; }  // a locked Private chat: Unlock first (the words stay)
    const content = mode === "send" ? $("t-prompt").value : text;
    if (mode === "send" && GOAL_RE.test(content)) return goalCommand(content);
    if (mode === "send" && !String(content || "").trim() && !C.atts.length) return;
    if (C.atts.some((a) => a.uploading)) { K.toast("Wait for the uploads to finish."); return; }
    const atts = mode === "send" ? sendable(C.atts) : undefined;  // edit and regenerate keep the turn's own
    let id;
    try { id = await ensureChat(); } catch (e) { K.toast(e.message, true); return; }
    const body = { mode, content, attachments: atts, effort: curEffort() }, keep = C.atts;
    if (mode === "send") { $("t-prompt").value = ""; autosize(); C.atts = []; renderAtts(); }
    // refused (too long, being compacted, a file gone…): the user's words and files come back, unless the user typed
    // again meanwhile
    if (await stream(id, body) === "refused" && mode === "send" && C.id === id && !$("t-prompt").value && !C.atts.length) {
      $("t-prompt").value = content; C.atts = keep; renderAtts(); autosize(); syncComposer();
    }
    K.loadList();
  }
  // /goal (chat_goal.py): what should be true when it is done; the chat works on by itself until it is
  const GOAL_RE = /^\s*\/goal(\s|$)/i;
  const GOAL_PAUSE = { stop: "you pressed Stop", idle: "three turns used no tools", loop: "it kept repeating itself", error: "a turn failed", judge: "the check could not be read" };
  async function goalCommand(content, typed = true) {  // typed: from the box (its words go once it is done); else the Clear button
    const sets = !/^\s*\/goal(\s+(clear|stop|off|reset|none|cancel))?\s*$/i.test(content);
    if (sets && C.atts.length) { K.toast("Send your files in their own message first, then set the goal."); return; }
    if (isPriv()) { K.toast("Goals don't work in Private chats yet.", true); return; }
    if (!sets && !C.id) { K.toast("No goal set. Type /goal and what should be true when it is done."); return; }  // (no chat is made for it)
    let id;
    try { id = await ensureChat(); } catch (e) { K.toast(e.message, true); return; }
    try {
      const r = await call("POST", `/studio/api/chats/${id}/goal`, { text: content, effort: curEffort() });
      if (typed && C.id === id && $("t-prompt").value === content) { $("t-prompt").value = ""; autosize(); }
      if (C.id === id && C.chat) C.chat.goal = r.goal;
      syncGoal(); syncComposer(); K.toast(String(r.status || "").slice(0, 300));
      followSoon(); K.loadList();
    } catch (e) { K.toast(e.message, true); }
  }
  function syncGoal() {
    const line = $("t-goal-line"), g = C.id && C.chat ? C.chat.goal : null, live = g && (g.state === "active" || g.state === "paused");
    const hide =
      !g || !(live || ((g.state === "met" || g.state === "impossible") && Date.now() / 1000 - g.since < 3600));
    const sig = hide ? "" : JSON.stringify([C.id, g.state, g.pause, g.turns, g.reason, g.text]);
    line.hidden = hide;
    if (line.dataset.sig === sig) return;
    line.dataset.sig = sig; line.replaceChildren();
    if (hide) return;
    const short = g.text.length > 90 ? g.text.slice(0, 89) + "…" : g.text;
    const head = g.state === "active" ? "◎ Goal active" : g.state === "paused" ? `◎ Goal paused: ${GOAL_PAUSE[g.pause] || "paused"}` : g.state === "met" ? "◎ Goal met" : "◎ Goal given up as impossible";
    line.append(document.createTextNode(`${head} · ${g.turns} turn${g.turns === 1 ? "" : "s"} · ${short}${g.reason ? ` · Last check: ${g.reason}` : ""}`));
    if (!live) return;
    line.append(document.createTextNode(" · "));
    const x = el("button", "textbtn", "Clear goal"); x.type = "button";
    x.addEventListener("click", () => goalCommand("/goal clear", false));
    line.appendChild(x);
  }
  async function sendWhileBusy() {
    if (!C.streaming) return send("send");  // following an answer from elsewhere: the usual send comes back queued
    const id = C.id, content = $("t-prompt").value;
    if (GOAL_RE.test(content)) return goalCommand(content);
    if (C.atts.some((a) => a.uploading)) { K.toast("Wait for the uploads to finish."); return; }
    const atts = sendable(C.atts), keep = C.atts;
    $("t-prompt").value = ""; autosize(); C.atts = []; renderAtts(); syncComposer();
    const ctrl = new AbortController();
    let resp, d = {};
    try {
      resp = await fetch(`/studio/api/chats/${id}/send`, { method: "POST", signal: ctrl.signal, cache: "no-store", credentials: "same-origin",
        headers: { ...K.privHeaders(), "Content-Type": "application/json", "X-Halo-UI-Token": K.TOKEN },
        body: JSON.stringify({ mode: "send", content, attachments: atts, effort: curEffort() }) });
    } catch (e) { $("t-prompt").value = content; C.atts = keep; renderAtts(); autosize(); syncComposer(); K.toast("Not sent: can't reach StrixFold UI on the Mac. Your message is still in the composer.", true); return; }
    if (resp.status === 200) { ctrl.abort(); followSoon(); return; }  // the answer had just ended: it is answering this one, on the Mac
    try { d = await resp.json(); } catch (e) { /* not JSON */ }
    if (resp.status === 401) { K.locked(); return; }
    if (resp.status !== 202) { $("t-prompt").value = content; C.atts = keep; renderAtts(); autosize(); syncComposer(); K.toast(d.error || `HTTP ${resp.status}`, true); return; }
    if (C.id !== id || !C.chat) return;
    const w = { id: d.id, chat_id: id, content, meta: { attachments: atts, effort: curEffort(), steer: true } };
    (C.chat.waiting = C.chat.waiting || []).push(w);
    const wn = waitingMsg(w); wn.dataset.k = `w${w.id}`;
    $("t-thread").appendChild(wn); syncBar(); reveal(wn);
  }
  async function stream(id, body) {
    hideOffline();
    const ctrl = new AbortController();
    const T = { id, ctrl, text: "", reasoning: "", node: null, body: null, think: null, raf: 0, started: Date.now() };
    C.streaming = T; syncComposer();
    let resp;
    try {
      resp = await fetch(`/studio/api/chats/${id}/send`, { method: "POST", signal: ctrl.signal, cache: "no-store", credentials: "same-origin",
        headers: { ...K.privHeaders(), "Content-Type": "application/json", "X-Halo-UI-Token": K.TOKEN }, body: JSON.stringify(body) });
    } catch (e) {  // the network itself: plain words, not "Failed to fetch"
      C.streaming = null; syncComposer();
      K.toast(e.name === "AbortError" ? "Stopped." : "Can't reach StrixFold UI on the Mac. Your message is still in the composer; send it again once it answers.", true);
      return "refused";
    }
    if (resp.status === 202 || !resp.ok) {
      let d = {}; try { d = await resp.json(); } catch (e) { /* not JSON */ }
      C.streaming = null;
      if (resp.status === 401) { K.locked(); return; }
      if (resp.status === 409 && d.why === "toolbox") { syncComposer(); showOffline(id, body, d.error); return; }
      if (resp.status === 409 && body.mode !== "send"
      ) {  // Retry or Edit with the model away: load it
        const W = wantOf(), l = (K.state() || {}).llm || {};
        if (l.state && !["idle", "stopped", "failed"].includes(l.state) && l.model_id !== W.id) {  // another model on: the Switch
          C.retryBar = { chat: id, again: body.mode === "regenerate" ? "Retry" : "Send" };  // syncBar keeps it up until the model is in
          syncBar(); syncComposer(); return "refused";
        }
        if (!l.state || ["idle", "stopped", "failed"].includes(l.state)) {
          try { await call("POST", "/studio/api/chat/model/start", { profile: W.profile, switch: false }); K.toast(`Loading ${W.name}. Press ${body.mode === "regenerate" ? "Retry" : "Send"} again once it's ready.`); }
          catch (e) { K.toast(e.message, true); }
          syncComposer(); K.poll(); return "refused";
        }
      }
      if (!resp.ok && resp.status !== 202) { syncComposer(); K.toast(d.error || `HTTP ${resp.status}`, true); return "refused"; }
      await reload(); pinTurn([...$("t-thread").querySelectorAll(".t-user")].pop()); K.poll(); return;  // it waits for the model, shown in the thread
    }
    if (body.mode !== "send") { try { C.chat = await call("GET", chatUrl(id)); } catch (e) { /* the events still draw */ } render(); }
    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let k;
        while ((k = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, k); buf = buf.slice(k + 2);
          const line = block.split("\n").find((l) => l.startsWith("data: "));
          if (line) onEvent(T, JSON.parse(line.slice(6)));
        }
      }
    } catch (e) { if (e.name !== "AbortError") K.toast(`The reply broke off on this page; it goes on on the Mac.`, true); }
    finally {
      if (C.streaming === T) C.streaming = null;
      cancelAnimationFrame(T.raf);
      if (!T.detach) { await reload(); }
      syncComposer();
    }
  }
  function onEvent(T, ev) {
    if (T.detach || C.id !== T.id) return;
    // the reply so far, drawn whole once a frame; a long one waits four times its last drawing's cost, so a phone
    // spends at most ~a fifth of its time redrawing it (the same handle, so every cancelAnimationFrame(T.raf) stops it)
    const paintSoon = (caret) => {
      if (T.raf) return;
      T.raf = requestAnimationFrame(function frame(now) {
        if (now - (T.painted || 0) < 4 * (T.cost || 0)) { T.raf = requestAnimationFrame(frame); return; }
        T.raf = 0;
        const t0 = performance.now();
        MD.render(T.body, T.text, { onCopy: copy });
        if (caret) T.body.appendChild(el("span", "caret"));
        follow();
        T.painted = performance.now(); T.cost = T.painted - t0;
      });
    };
    const t = $("t-thread");
    const before = t.querySelector(".waiting");  // what waits stays below what the model has read
    if (ev.type === "user") {
      if (t.querySelector(".t-hello")) t.replaceChildren();
      if (ev.from_outbox) {  // a message the user sent while it answered, read now
        const row = t.querySelector(`.waiting[data-wait="${ev.from_outbox}"]`);
        if (row) row.remove();
        if (C.chat) C.chat.waiting = (C.chat.waiting || []).filter((w) => w.id !== ev.from_outbox);
      }
      const u = userMsg({ id: ev.id, role: "user", content: ev.content, meta: ev.meta || {} }, false);
      u.dataset.k = `m${ev.id}`;
      t.insertBefore(u, t.querySelector(".waiting"));
      if (ev.from_outbox && V.pinK === `w${ev.from_outbox}`) { V.pinK = u.dataset.k; sizeSpacer(); }  // the pin goes with it
      if (ev.from_outbox) follow(); else pinTurn(u);  // the user's own message just sent: near the top, the answer below
    } else if (ev.type === "looking") {  // a text-only model waits for its photos' descriptions on the Mac (chat_look)
      $("t-status").textContent = ev.photos === 1 ? "Looking at the photo on your Mac" : "Looking at the photos on your Mac";
    } else if (ev.type === "compacting") {
      T.compact = compactDivider(null, true); t.appendChild(T.compact); follow();
      $("t-status").textContent = "Compacting the conversation";
    } else if (ev.type === "compacted") {
      if (T.compact) { T.compact.classList.remove("pending"); T.compact.querySelector(".t-compact-t").textContent = ev.failed ? "Couldn't summarise: the oldest turns were left out" : "Conversation compacted"; }
    } else if (ev.type === "window") {  // each step's own count: the context tracker follows the answer as it grows
      if (C.chat) { C.chat.window = Object.assign({}, C.chat.window || {}, { used: ev.used, context: ev.context, measured: true }); syncChip(); }
    } else if (ev.type === "start") {
      if (T.node) {  // the answer before the user's message, as it ended
        cancelAnimationFrame(T.raf); T.raf = 0;
        if (T.text) MD.render(T.body, T.text, { onCopy: copy }); else T.body.remove();
        Object.assign(T, { text: "", reasoning: "", think: null, steps: null, asks: null, started: Date.now() });
      }
      T.node = el("div", "msg msg-asst t-asst"); T.node.dataset.k = "live";
      T.body = el("div", "msg-body t-body"); T.body.appendChild(el("span", "caret"));
      T.node.appendChild(T.body);
      t.insertBefore(T.node, before); follow();
      $("t-status").textContent =
        "Answering";
    } else if (ev.type === "reasoning") {
      T.reasoning += ev.text;
      if (!T.think) { T.think = thinkBlock("", 0, true); T.node.insertBefore(T.think, T.body); }
      T.think.querySelector(".t-think-body").textContent = T.reasoning;
      follow();
    } else if (ev.type === "delta") {
      if (T.think && T.think.open && !T.text) {
        T.think.open = false;
        T.think.querySelector("summary").textContent = `Thought for ${plural(Math.max(1, Math.round((Date.now() - T.started) / 1000)), "second")}`;
      }
      T.text += ev.text;
      paintSoon(true);
    } else if (ev.type === "retract") {  // words that were a tool call written out (chat_tools.calls_in_text): taken back
      T.text = T.text.slice(0, Math.max(0, T.text.length - (ev.chars || 0)));
      paintSoon(false);
    } else if (ev.type === "tool") {  // a tool call (chat_tools): the text so far, then its step, then a new text part
      if (!T.node) return;
      T.steps = T.steps || {};
      segment(T, T.steps[ev.id] = stepBlock({ id: ev.id, line: ev.line, status: "running" }));
      $("t-status").textContent = ev.line;
    } else if (ev.type === "tool_out") {
      const s = T.steps && T.steps[ev.id];
      if (s) { const pre = s.querySelector(".t-step-out"); pre.textContent = (pre.textContent + ev.text).slice(-4000); }
    } else if (ev.type === "tool_done") {
      const s = T.steps && T.steps[ev.id];
      if (ev.replaces) document.querySelectorAll(".t-doc").forEach((d) => { if (d.dataset.file === ev.replaces) d.remove(); });  // made again
      if (s) s.replaceWith(T.steps[ev.id] = stepBlock(ev));
    } else if (ev.type === "note") {
      if (T.node) segment(T, el("p", "t-note t-step-note", ev.text));
    } else if (ev.type === "ask") {  // a tap (Full access): the command waits for the user
      if (!T.node) return;
      T.asks = T.asks || {};
      segment(T, T.asks[ev.id] = tapCard(Object.assign({ open: true }, ev)));
      $("t-status").textContent = "Waiting for your tap";
      attention("tap");
      follow();
    } else if (ev.type === "ask_done") {
      const c = T.asks && T.asks[ev.id];
      if (c) c.replaceWith(T.asks[ev.id] = tapCard(Object.assign({}, ev, { open: false })));
    } else if (ev.type === "error") {
      T.node && T.node.appendChild(el("p", "t-err", ev.error));
    } else if (ev.type === "done") {
      $("t-status").textContent =
        "Answer complete";
      attention("done");
    }
  }
  async function stop() {
    const id = C.id; if (!id) return;
    try { await call("POST", `/studio/api/chats/${id}/stop`); } catch (e) { if (C.streaming) C.streaming.ctrl.abort(); }
  }

  // ---------------------------------------------------------------- Private chats
  // the composer's files stay on their side of Private: a Private chat's file only in that chat, and a Private
  // chat takes none of another's; the others are dropped when another chat opens (the server refuses them too)
  // each chat keeps its own unsent words while this page is open: in memory only, never
  // stored; a Private chat's go when Private locks. Before, the words followed the user into whichever chat the user
  // opened.
  const DRAFTS = new Map();
  function swapDraft(to) {
    const t = $("t-prompt"), from = C.id || "";
    to = to || "";
    if (from === to) return;
    // the files go with the words (attached files followed the user into other chats while the words stayed)
    const atts = C.atts.filter((a) => !a.uploading);
    if (t.value.trim() || atts.length) DRAFTS.set(from, { text: t.value, priv: isPriv(), atts }); else DRAFTS.delete(from);
    const d = DRAFTS.get(to);
    DRAFTS.delete(to);
    t.value = d ? d.text : ""; autosize();
    if (!C.atts.some((a) => a.uploading)) { C.atts = d && d.atts ? d.atts : []; renderAtts(); }
    syncComposer();
  }
  function keepAtts() {
    const priv = C.chat && C.chat.private ? C.id : null;
    const kept = C.lockedId && !C.chat ? [] : C.atts.filter((a) => (a.priv || null) === priv);
    if (kept.length !== C.atts.length) { C.atts = kept; renderAtts(); syncComposer(); }
  }
  const LOCK_IC = '<rect x="5.5" y="10.5" width="13" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>';
  function privThumb(a) {
    const w = el("figure", "att-thumb att-icon");
    w.appendChild(svg(K.ICON.image));
    fetch(`/studio/ref/${a.id}`, { headers: K.privHeaders(), credentials: "same-origin", cache: "no-store" })
      .then((r) => (r.ok ? r.blob() : null)).then((b) => {
        if (!b) return;
        const i = el("img"); i.alt = ""; i.src = URL.createObjectURL(b);
        i.addEventListener("load", () => URL.revokeObjectURL(i.src), { once: true });
        w.classList.remove("att-icon"); w.replaceChildren(i);
      }).catch(() => { /* the icon stays */ });
    return w;
  }
  function renderLocked(id) {
    const t = $("t-thread"); t.replaceChildren();
    const box = el("div", "t-hello t-locked");
    box.append(svg(LOCK_IC, "t-priv-big"), el("h1", "", "This chat is Private"),
      el("p", "", "Unlock with your passcode to read it. Private stays open until you lock it, 30 minutes without use, a reload of this page or a restart of StrixFold UI."));
    const b = el("button", "t-priv-go", "Unlock"); b.type = "button";
    b.addEventListener("click", () => openUnlock(() => open(id)));
    box.appendChild(b);
    t.appendChild(box);
  }
  async function newPrivate() {
    if (C.atts.length) {  // already saved as ordinary uploads: they can't become Private
      K.toast("These files were saved before the chat was Private. Remove them, make the chat Private, then attach them again.", true);
      return;
    }
    try {
      const c = await call("POST", "/studio/api/chats", { kind: "chat", model: curModel(), effort: curEffort(), private: true });
      C.id = c.id; C.chat = Object.assign(c, { waiting: [], messages: [], jobs: {}, items: {}, refs: {} });
      history.replaceState(null, "", `#/talk/${c.id}`);
      render(); setTitle(); syncPerm(); K.loadList(); $("t-prompt").focus();
    } catch (e) { K.toast(e.message, true); }
  }
  async function lockPrivate() {
    if (C.streaming && C.chat && C.chat.private) { C.streaming.detach = true; C.streaming = null; syncComposer(); }
    try { await call("POST", "/studio/api/private/lock", {}); } catch (e) { /* locked either way on this page */ }
    K.setPrivate(null);
    if (K.forgetPrivate) K.forgetPrivate();  // its document leaves the viewer, its bytes the Save cache
    privateGone();
    K.loadList();
  }
  // the open Private chat leaves the page: its thread, its unsent words and files, an open Unlock sheet (Lock, the page's
  // lock, the idle lock: studio.js sends halo:private-locked)
  function privateGone() {
    if (!(C.chat && C.chat.private)) return;
    if (C.streaming) { C.streaming.detach = true; C.streaming = null; }
    const id = C.id; C.chat = null; C.lockedId = id;
    $("t-prompt").value = ""; autosize(); C.atts = []; renderAtts();
    renderLocked(id); setTitle(); syncPerm(); syncComposer();
  }
  window.addEventListener("halo:private-locked", () => {
    for (const [k, d] of DRAFTS) if (d.priv) DRAFTS.delete(k);
    FULL.clear(); OUTS.clear();
    privateGone();
    if (C.view === "unlock" && !$("t-sheet-wrap").hidden) closeSheet();
  });
  // the Unlock sheet: the passcode; the code that runs is shown; the token stays in this page's memory only
  function openUnlock(after) {
    C.afterUnlock = after;
    if ($("t-sheet-wrap").hidden) openSheet();
    C.view = "unlock"; C.unlockErr = null; renderSheet();
  }
  function renderUnlock(box) {
    const f = el("form", "t-unlock"); f.noValidate = true;
    const lab = el("label", "t-unlock-l", "passcode");
    const inp = el("input"); inp.type = "password"; inp.id = "t-unlock-pass"; inp.autocomplete = "current-password"; inp.required = true;
    lab.htmlFor = "t-unlock-pass";
    const go = el("button", "t-priv-go", "Unlock"); go.type = "submit";
    f.append(lab, inp, go);
    if (C.unlockErr) {
      f.appendChild(el("p", "t-err", C.unlockErr.error));
      for (const x of ((C.unlockErr.code || {}).problems || [])) f.appendChild(el("p", "t-note", x));
    }
    f.appendChild(el("p", "t-sheet-note", "Private chats are sealed on this Mac. Unlocking checks that the code running is the landed code, then opens them on this page only, until you lock it, 30 minutes without use, a reload or a restart."));
    f.appendChild(el("p", "t-sheet-note", "It can't catch a change made before StrixFold UI started to how StrixFold UI starts: its launcher, Python's startup files (.pth files or usercustomize in your user site-packages), or this check itself."));
    f.addEventListener("submit", async (e) => {
      e.preventDefault(); go.disabled = true;
      try {
        const d = await call("POST", "/studio/api/private/unlock", { passcode: inp.value });
        inp.value = "";
        K.setPrivate(d.token);
        closeSheet();
        if (d.code && d.code.line) K.toast(d.code.line);
        K.loadList();
        if (C.afterUnlock) { const a = C.afterUnlock; C.afterUnlock = null; a(); }
      } catch (err) {
        inp.value = ""; go.disabled = false;
        C.unlockErr = { error: err.message, code: err.data && err.data.code };
        renderSheet();
      }
    });
    box.appendChild(f);
    requestAnimationFrame(() => inp.focus());
  }

  // ---------------------------------------------------------------- permissions (mac/chat_tools.py)
  const PERM_IC = {
    none: '<circle cx="12" cy="12" r="8"/><path d="M6.5 17.5l11-11"/>',
    box: '<path d="M12 3.5l7.5 4.2v8.6L12 20.5l-7.5-4.2V7.7z"/><path d="M4.5 7.7L12 12l7.5-4.3M12 12v8.5"/>',
    box_web: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.6 2.6 14.4 0 17M12 3.5c-2.6 2.6-2.6 14.4 0 17"/>',
    full: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M7.5 10l3 2.5-3 2.5M12.5 15.5h4"/>',
  };
  const PERM_SHORT = { none: "Off", box: "Box", box_web: "Box+web", full: "Full", direct: "Direct" };
  const PERM_NAME = { none: "No tools", box: "Container", box_web: "Container + web", full: "Full access", direct: "Direct access" };
  const curPerm = () => (C.id && C.chat ? C.chat.perm : C.newPerm) || "none";
  const permOpts = () => { try { return JSON.parse((C.chat && C.chat.perm_opts) || "{}") || {}; } catch (e) { return {}; } };
  // ---------------------------------------------------------------- Speed: each answer's decode and prompt speeds in
  // its footer
  // (halo_chat: meta.speed, from the engine's own clock), and a sheet with that answer's steps and every pair's last
  // hour (studio_http.speed_view, from each halod's request log). tok/s: tokens written a second; pp: new (uncached)
  // prompt tokens read a second; wait: queued for a slot.
  const kTok = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K` : String(Math.round(n || 0)));
  const speedLine = (sp) => [sp.tps ? `${sp.tps} tok/s` : null, sp.pp ? `pp ${kTok(sp.pp)}/s` : null].filter(Boolean).join(" \u00b7 ");
  function openSpeed(m) {
    C.speedMsg = m || null; C.speedView = null;
    if ($("t-sheet-wrap").hidden) openSheet();
    C.view = "speed"; $("t-model").setAttribute("aria-expanded", "false"); renderSheet();
    call("GET", "/studio/api/speed").then((v) => { C.speedView = v; }).catch((e) => { C.speedView = { error: e.message }; })
      .then(() => { if (C.view === "speed" && !$("t-sheet-wrap").hidden) renderSheet(); });
  }
  function speedTable(heads, rows) {
    const wrap = el("div", "t-sp-tw"), t = el("table", "t-sp-t"), tr = el("tr");
    for (const h of heads) tr.appendChild(el("th", "", h));
    const th = el("thead"); th.appendChild(tr); t.appendChild(th);
    const tb = el("tbody");
    for (const r of rows) { const row = el("tr"); for (const c of r) row.appendChild(el("td", "", c == null || c === "" ? "\u2014" : String(c))); tb.appendChild(row); }
    t.appendChild(tb); wrap.appendChild(t);
    return wrap;
  }
  function renderSpeed(box) {
    const m = C.speedMsg, sp = m && (m.meta || {}).speed;
    if (sp) {
      box.appendChild(el("p", "t-sp-h", "This answer"));
      box.appendChild(el("p", "t-sp-big", speedLine(sp) || "\u2014"));
      box.appendChild(el("p", "t-sheet-note t-sp-note", [`read ${kTok(sp.in)} tokens${sp.cached ? ` (${kTok(sp.cached)} cached)` : ""}`,
        `wrote ${num(sp.out)}`, `${sp.steps} step${sp.steps === 1 ? "" : "s"}`, sp.wait ? `waited ${sp.wait} s for a slot` : null].filter(Boolean).join(" \u00b7 ")));
      const steps = (m.meta || {}).speed_steps || [];
      if (steps.length > 1) box.appendChild(speedTable(["#", "Read", "Cached", "pp/s", "Wrote", "tok/s", "Wait"],
        steps.map((x, i) => [i + 1, kTok(x.in), kTok(x.cached), x.pp ? num(x.pp) : null, num(x.out), x.tps, x.wait ? `${x.wait} s` : null])));
    }
    box.appendChild(el("p", "t-sp-h", "Your boxes \u00b7 last hour"));
    const v = C.speedView;
    if (!v) { box.appendChild(el("p", "t-sheet-note", "Reading the boxes\u2026")); return; }
    if (v.error) { box.appendChild(el("p", "t-sheet-note", `Couldn't read the boxes: ${v.error}`)); return; }
    for (const p of v.pairs || []) {
      const card = el("div", "t-sp-pair");
      const h = el("p", "t-sp-ph");
      h.append(el("span", "t-sp-pn", p.label), el("span", "t-sp-pp", `${p.profile_name || p.profile || ""}${p.now ? "" : " \u00b7 earlier"}`));
      card.appendChild(h);
      if (!p.n) { card.appendChild(el("p", "t-sheet-note t-sp-note", `No requests in the last hour${p.state && p.state !== "ready" ? ` \u00b7 ${p.state}` : ""}.`)); box.appendChild(card); continue; }
      const nums = el("div", "t-sp-nums");
      const put = (k, val, sub) => { const d = el("div"); d.append(el("span", "t-sp-k", k), el("span", "t-sp-v", val), el("span", "t-sp-s", sub || "")); nums.appendChild(d); };
      put("Decode", p.tps ? `${p.tps} tok/s` : "\u2014", p.tps_best ? `best ${p.tps_best}` : "");
      put("Prompt", p.pp ? `${kTok(p.pp)} tok/s` : "\u2014", p.pp_best ? `best ${kTok(p.pp_best)}` : "");
      put("Wait", p.wait != null ? `${p.wait} s` : "\u2014", `${p.n} request${p.n === 1 ? "" : "s"}`);
      card.appendChild(nums);
      if ((p.recent || []).length) {
        const d = el("details", "t-sp-recent");
        d.appendChild(el("summary", "", "Latest requests"));
        d.appendChild(speedTable(["When", "Read", "Cached", "pp/s", "Wrote", "tok/s", "Wait"], p.recent.map((r) => [
          new Date(r.t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }), kTok(r.in), kTok(r.cached),
          r.pp ? num(Math.round(r.pp)) : null, num(r.out), r.tps, r.wait ? `${r.wait} s` : null])));
        card.appendChild(d);
      }
      box.appendChild(card);
    }
    box.appendChild(el("p", "t-sheet-note", "Decode: tokens written per second. Prompt: new prompt tokens read per second (cached ones are skipped). Wait: time queued for a free slot. Chats and agents share a pair, so these drop while it is busy."));
  }

  // ---------------------------------------------------------------- Team mode (chat_team.py): the chat's lead starts agents
  const TEAM_CAPS = [0, 2, 3, 4, 6, 8, 12];
  const TEAM_STATUS = { queued: "Waiting for a slot", running: "Working", done: "Done", failed: "Failed", stopped: "Stopped", interrupted: "Interrupted" };
  const teamLive = (a) => a.status === "queued" || a.status === "running";
  const teamOf = () => (C.id && C.chat ? C.chat.team : null) || { cap: C.id ? 0 : (C.newTeam || 0), agents: [], running: 0, slots: [] };
  function syncTeam() {
    syncGoal();
    if (AG.open && AG.chat !== C.id) { AG.chat = C.id; showAgent(null); }  // another chat: its agents
    const b = $("t-team"), t = teamOf();
    let off = !!(C.chat && (C.chat.cousteau || C.chat.private)) || isPriv() || !!((K.state() || {}).llm || {}).connected;  // (connect.py: no agents)
    // connected to a running server (connect.py): the chip stays, greyed, and says why there is no Team
    const away = !!((K.state() || {}).llm || {}).connected && !(C.chat && (C.chat.cousteau || C.chat.private)) && !isPriv();
    b.hidden = off && !away;
    b.disabled = away;
    b.title = away ? "Team needs the app's own boxes: it isn't there while the app is connected to a running server" : "";
    $("t-team-k").textContent = t.cap ? `Team ${t.cap}` : "Team";
    b.classList.toggle("lv-none", !t.cap);
    b.setAttribute("aria-label", t.cap ? `Team mode: up to ${t.cap} agents. Change` : "Team mode: off. Change");
    const line = $("t-team-line"), agents = t.agents || [], live = agents.filter(teamLive);
    const look = live.filter((a) => AG_BAD.has((a.health || {}).state));  // looping, failing, stalled (not just slow)
    const hide = off || !live.length || AG.open;
    const sig = hide ? "" : `${live.filter((a) => a.status === "running").length}/${live.length}/${look.map((a) => a.name).join(",")}`;
    line.hidden = hide;
    if (line.dataset.sig !== sig) {  // drawn anew only when it changes (a focused button stays put between polls)
      line.dataset.sig = sig;
      line.replaceChildren();
    }
    if (!hide && !line.firstChild) {
      const working = live.filter((a) => a.status === "running").length, waiting = live.length - working;
      line.append(document.createTextNode(`Team · ${working} working${waiting ? ` · ${waiting} waiting for a slot` : ""}`));
      if (look.length) line.appendChild(el("span", "t-team-warn", ` · ${look.length === 1 ? `${look[0].name} needs a look` : `${look.length} need a look`}`));
      line.append(document.createTextNode(" · "));
      const v = el("button", "textbtn", "Open agents"); v.type = "button"; v.addEventListener("click", () => openAgents());
      line.appendChild(v);
    }
    noteChanges(agents);
    clearTimeout(C.teamPoll);
    if (C.id && (live.length || AG.open)) C.teamPoll = setTimeout(pollTeam, live.length ? 2500 : 10000);
  }
  async function pollTeam() {
    const id = C.id;
    if (!id || document.hidden) { C.teamPoll = setTimeout(pollTeam, 3000); return; }
    try { const v = await call("GET", `/studio/api/chats/${id}/team`); if (C.id === id && C.chat) C.chat.team = v; } catch (e) { /* the next poll tries again */ }
    if (C.id !== id) return;
    syncTeam();
    if (!$("t-sheet-wrap").hidden && C.view === "team") renderSheet();
    if (AG.open && !AG.aid) renderAg();
  }
  function openTeam() {
    if (!$("t-sheet-wrap").hidden && C.view === "team") { closeSheet(); return; }
    if ($("t-sheet-wrap").hidden) openSheet();
    C.view = "team"; $("t-team").setAttribute("aria-expanded", "true"); renderSheet();
    if (C.id) pollTeam();
  }
  async function setTeam(cap) {
    if (!C.id) { C.newTeam = cap || null; syncTeam(); renderSheet(); if (AG.open) renderAg(); return; }
    try {
      C.chat.team = await call("PUT", `/studio/api/chats/${C.id}/team`, { cap });
      K.toast(cap ? `Team mode on: up to ${cap} agents` : "Team mode off");
    } catch (e) { K.toast(e.message, true); }
    syncTeam(); renderSheet(); if (AG.open) renderAg();
  }
  function renderTeam(box) {
    const t = teamOf();
    box.appendChild(el("p", "t-sheet-note", "This chat's model can start helper agents that work in parallel and report back to it. Ask in plain words, like “spin up 4 agents to …”. They run DeepSeek uncensored on the extra pairs (and the main boxes when idle) with this chat's tools, unless you ask for sandboxed agents."));
    const card = el("div", "t-card"); card.setAttribute("role", "menu"); card.setAttribute("aria-label", "Team mode");
    for (const cap of TEAM_CAPS) {
      card.appendChild(row(cap ? `Up to ${cap} agents` : "Off", cap ? null : "The model works alone", cap === (t.cap || 0), () => setTeam(cap)));
    }
    box.appendChild(card);
    if ((t.slots || []).length) {
      const free = t.slots.reduce((n, s) => n + (s.free || 0), 0);
      box.appendChild(el("p", "t-sheet-note", `Free agent slots now: ${free} (${t.slots.map((s) => `${s.label} ${s.free}`).join(", ")}). Each pair keeps one slot for chats; more agents than that wait their turn.`));
    }
    if (!C.id) return;
    const agents = t.agents || [], live = agents.filter(teamLive).length;
    const open = el("button", "t-team-open"); open.type = "button";
    open.append(svg('<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M14.5 4.5v15"/>', "t-team-ic"),
      el("span", "", agents.length ? `Open the agents panel · ${live ? `${live} working` : `${agents.length} agent${agents.length === 1 ? "" : "s"}`}` : "Open the agents panel"));
    open.addEventListener("click", () => { closeSheet(); openAgents(); });
    box.appendChild(open);
  }

  // ---------------------------------------------------------------- the Agents panel: a column beside the chat, like the viewer. The list (what each
  // agent is doing now, its health from the watcher) and one agent's timeline, followed live; write to it, stop it, run
  // it again. chat_team.py keeps the timeline and runs the watcher.
  const AG = { open: false, chat: null, aid: null, d: null, log: [], seq: 0, cks: [], tab: "activity", poll: null, tick: null, trigger: null, seen: null, sending: false };
  const AG_WARN = { slow: "Slow", reconnecting: "Reconnecting", "near its limit": "Near its limit", looping: "Repeating itself", erroring: "Calls failing", stalled: "Stalled", stopping: "Stopping" };
  const AG_BAD = new Set(["looping", "erroring", "stalled"]);
  const AG_DOT = { queued: "Waiting", running: "Working", done: "Done", failed: "Failed", stopped: "Stopped", interrupted: "Interrupted" };
  const dur = (s) => { s = Math.max(0, Math.round(s || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = String(s % 60).padStart(2, "0"); return h ? `${h}:${String(m).padStart(2, "0")}:${x}` : `${m}:${x}`; };
  const num = (n) => Number(n || 0).toLocaleString("en-US");
  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });  // a check-in's time of day
  const cap1 = (t) => (t ? String(t).charAt(0).toUpperCase() + String(t).slice(1) : "");
  const why = (t) => cap1(String(t || "").replace(/^stopped by the user$/i, "stopped by you"));  // why an agent ended, in the user's words
  // a time that keeps counting between polls: its value when read, and when
  const ticking = (cls, base, live) => { const s = el("span", cls, dur(base)); if (live) { s.dataset.base = String(base || 0); s.dataset.at = String(Date.now()); } return s; };
  function tickAg() {
    for (const s of $("tm").querySelectorAll("[data-at]")) s.textContent = dur(Number(s.dataset.base) + (Date.now() - Number(s.dataset.at)) / 1000);
  }
  function agentOf(id) { return (teamOf().agents || []).find((a) => a.id === id) || null; }
  // what changed since the last look: a toast when an agent ends or the watcher flags one (not on a chat's first read)
  function noteChanges(agents) {
    const key = C.id;
    if (!AG.seen || AG.seen.chat !== key) { AG.seen = { chat: key, by: {} }; for (const a of agents) AG.seen.by[a.id] = [a.status, (a.health || {}).state]; return; }
    for (const a of agents) {
      const was = AG.seen.by[a.id], hs = (a.health || {}).state;
      AG.seen.by[a.id] = [a.status, hs];
      if (!was) continue;
      if (was[0] !== a.status && !teamLive(a)) {
        if (a.status === "done") K.toast(`Agent ${a.name} finished`);
        else if (a.status === "failed") K.toast(`Agent ${a.name} failed: ${a.error || ""}`.trim(), true);
        else if (a.status === "stopped" && /watcher/.test(a.error || "")) K.toast(`The watcher stopped ${a.name}: ${a.error.replace(/^the watcher [^:]*: /, "")}`, true);
      } else if (teamLive(a) && hs !== was[1] && AG_BAD.has(hs)) K.toast(`${a.name}: ${(a.health || {}).line || AG_WARN[hs]}`, true);
    }
  }
  function openAgents(aid) {
    if (!$("tm")) return;
    if (!$("viewer").hidden) $("v-close").click();  // one column beside the chat at a time
    if (!AG.open) AG.trigger = document.activeElement;
    AG.open = true; AG.chat = C.id;
    $("tm").hidden = false; $("app").classList.add("tm-open"); document.documentElement.classList.add("tm-on");
    showAgent(aid || null);
    clearInterval(AG.tick); AG.tick = setInterval(tickAg, 1000);
    syncTeam(); pollTeam();
    requestAnimationFrame(() => $("tm").focus({ preventScroll: true }));  // the panel itself (labelled), not a button
  }
  function closeAgents() {
    tipHide();
    if (!AG.open) return;
    AG.open = false; AG.aid = null; AG.d = null;
    clearTimeout(AG.poll); clearInterval(AG.tick);
    $("tm").hidden = true; $("app").classList.remove("tm-open"); document.documentElement.classList.remove("tm-on");
    $("tm-body").replaceChildren();
    syncTeam();
    if (AG.trigger && AG.trigger.isConnected) AG.trigger.focus();
  }
  function showAgent(aid) {
    tipHide();
    clearTimeout(AG.poll);
    AG.aid = aid; AG.d = null; AG.log = []; AG.seq = 0; AG.cks = []; AG.tab = "activity";
    $("tm-body").replaceChildren(); $("tm-body").scrollTop = 0;
    renderAg();
    if (aid) pollAgent();
  }
  async function pollAgent() {
    clearTimeout(AG.poll);
    const id = C.id, aid = AG.aid;
    if (!AG.open || !aid || !id) return;
    if (!document.hidden) {
      try {
        const d = await call("GET", `/studio/api/chats/${id}/team/${aid}?after=${AG.seq}`);
        if (AG.aid !== aid || C.id !== id) return;
        const fresh = !AG.d;
        AG.d = d;
        if (d.checkins) AG.cks = d.checkins;  // sent on the first read and when a new one comes (chat_team.detail)
        for (const e of d.log || []) {
          const i = AG.log.findIndex((x) => x.n === e.n);
          if (i >= 0) AG.log[i] = e; else AG.log.push(e);
        }
        AG.seq = Math.max(AG.seq, d.seq || 0);
        renderAg(fresh ? null : d.log || []);
      } catch (e) { if (AG.aid === aid && !AG.d) { $("tm-body").replaceChildren(el("p", "tm-empty", `Couldn't read the agent: ${e.message}`)); } }
    }
    const live = AG.d && teamLive(AG.d);
    if (AG.open && AG.aid === aid && (live || !AG.d)) AG.poll = setTimeout(pollAgent, 1500);
  }
  function renderAg(newEvents) {
    if (!AG.open) return;
    $("tm-back").hidden = !AG.aid;
    $("tm-body").classList.toggle("detail", !!AG.aid);
    if (!AG.aid) { renderAgList(); return; }
    renderAgDetail(newEvents);
  }
  // what each watcher flag means, with the watcher's own numbers (chat_team.Team.view: watch)
  function flagHelp(state) {
    const w = teamOf().watch || { slow_min: 5, stall_min: 20, loop: [3, 5], errs: [4, 7], near_pct: 80, steps: 120, wall_min: 45, retry_s: [5, 15, 30, 60] };
    return {
      slow: `One reply or tool call has been running for over ${w.slow_min} minutes. Nothing is wrong yet: the watcher stops it only if no step finishes for ${w.stall_min} minutes.`,
      reconnecting: `Its pair didn't answer, so it is trying again (after ${w.retry_s.join(", ")} s). It carries on by itself once the pair is back.`,
      "near its limit": `It has used ${w.near_pct}% of the step or time cap it was given (agents have none unless you ask for one). The watcher told it to finish the main thing and write its report soon.`,
      looping: `The same tool call gave the same result ${w.loop[0]} times in a row, so the watcher told it to try something else. At ${w.loop[1]} it is stopped and writes its report.`,
      erroring: `${w.errs[0]} tool calls in a row failed, so the watcher told it to read the errors and change approach. At ${w.errs[1]} it is stopped and writes its report.`,
      stalled: `No step finished for ${w.stall_min} minutes, so the watcher stopped it.`,
    }[state] || "";
  }
  function healthTag(h, key, focusable) {
    if (!h || !AG_WARN[h.state]) return null;
    const t = el("span", `tm-health ${AG_BAD.has(h.state) ? "bad" : "warn"}`, AG_WARN[h.state]);
    t.dataset.tip = flagHelp(h.state) + (h.line ? `\n\nNow: ${h.line}` : "");
    t.dataset.tipkey = key;
    if (focusable) { t.tabIndex = 0; t.setAttribute("role", "button"); t.setAttribute("aria-label", `${AG_WARN[h.state]}: what it means`); }
    tipOn(t, focusable);
    return t;
  }
  // one floating explanation for a small label: hover, keyboard focus, or a tap (over everything, kept on the screen)
  const TIP = { el: null, for: null, key: null };
  function tipShow(node) {
    if (!node.dataset.tip) return;
    if (!TIP.el) { TIP.el = el("div", "tm-tip"); TIP.el.id = "tm-tip"; TIP.el.setAttribute("role", "tooltip"); TIP.el.hidden = true; document.body.appendChild(TIP.el); }
    TIP.el.textContent = node.dataset.tip; TIP.el.hidden = false; TIP.for = node; TIP.key = node.dataset.tipkey || null;
    node.setAttribute("aria-describedby", "tm-tip");
    const r = node.getBoundingClientRect(), w = TIP.el.offsetWidth, h = TIP.el.offsetHeight;
    const x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    const y = r.bottom + 8 + h > window.innerHeight - 8 ? r.top - h - 8 : r.bottom + 8;
    TIP.el.style.left = `${Math.round(x)}px`; TIP.el.style.top = `${Math.round(Math.max(8, y))}px`;
  }
  function tipHide() {
    if (TIP.el) TIP.el.hidden = true;
    if (TIP.for) TIP.for.removeAttribute("aria-describedby");
    TIP.for = null; TIP.key = null;
  }
  function tipOn(node, tappable) {
    node.addEventListener("mouseenter", () => tipShow(node));
    node.addEventListener("mouseleave", tipHide);
    node.addEventListener("focus", () => tipShow(node));
    node.addEventListener("blur", tipHide);
    if (tappable) {
      node.addEventListener("click", (e) => { e.stopPropagation(); if (TIP.for === node) tipHide(); else tipShow(node); });
      node.addEventListener("keydown", (e) => { if (e.key === "Escape" && TIP.for === node) { e.stopPropagation(); tipHide(); } });
    }
  }
  // a redraw replaces the label under the pointer: its explanation follows the new one
  function tipFollow(root) {
    if (!TIP.key) return;
    const n = root.querySelector(`[data-tipkey="${CSS.escape(TIP.key)}"]`);
    if (n) tipShow(n); else tipHide();
  }
  document.addEventListener("scroll", () => { if (TIP.for) tipHide(); }, true);
  function statusDot(a) {
    const d = el("span", `tm-dot st-${a.status}${AG_BAD.has((a.health || {}).state) && teamLive(a) ? " bad" : ""}`);
    d.setAttribute("role", "img"); d.setAttribute("aria-label", AG_DOT[a.status] || a.status);
    return d;
  }
  async function agAct(path, body, done) {
    try { const r = await call("POST", `/studio/api/chats/${C.id}/team${path}`, body || {}); if (r && r.cap !== undefined) C.chat.team = r; else if (r && r.team) C.chat.team = r.team; if (done) done(r); }
    catch (e) { K.toast(e.message, true); }
    syncTeam(); renderAg(); pollTeam();
  }
  function renderAgList() {
    const box = $("tm-body"), t = teamOf(), all = [...(t.agents || [])].reverse();
    const agents = [...all.filter(teamLive).reverse(), ...all.filter((a) => !teamLive(a))];  // working ones first, in order
    $("tm-title").textContent = "Agents";
    $("tm-say").hidden = true;
    const keep = box.scrollTop, f = box.contains(document.activeElement) ? document.activeElement : null;
    const fk = f ? (f.dataset.aid ? `a:${f.dataset.aid}` : `t:${f.textContent}`) : null;
    box.replaceChildren();
    const head = el("div", "tm-sum");
    const live = agents.filter(teamLive), working = live.filter((a) => a.status === "running").length;
    const done = agents.filter((a) => a.status === "done").length, bad = agents.filter((a) => a.status === "failed").length;
    const parts = [`${working} working`];
    if (live.length - working) parts.push(`${live.length - working} waiting`);
    if (done) parts.push(`${done} done`);
    if (bad) parts.push(`${bad} failed`);
    const capRow = el("div", "tm-cap");
    capRow.append(el("span", "", t.cap ? `Up to ${t.cap} at once` : "Team mode is off"));
    const ch = el("button", "textbtn", "Change"); ch.type = "button"; ch.addEventListener("click", openTeam);
    capRow.appendChild(ch);
    if (live.length) {
      const wrapping = live.some((a) => a.stopping === "wrap");  // asked for their reports: again stops them at once
      const all = el("button", "textbtn tm-stopall", wrapping ? "Stop all now" : "Stop all"); all.type = "button";
      all.title = wrapping ? "Those still writing their reports stop at once" : "Each stops what it's doing and writes its report";
      all.addEventListener("click", () => agAct("/stop-all", wrapping ? { now: true } : {}));
      capRow.appendChild(all);
    }
    head.append(capRow);
    if (agents.length) head.appendChild(el("p", "tm-counts", parts.join(" · ")));
    if ((t.slots || []).length) {
      const ul = el("ul", "tm-pairs");  // each pair as the conductor sees it: who holds its streams, held or set aside
      for (const p of t.slots) {
        const li = el("li", "tm-pair");
        const who = [p.chats ? (p.chats === 1 ? "your chat" : `${p.chats} chats`) : null, ...(p.agents || [])].filter(Boolean);
        const busy = (p.chats || 0) + (p.agents || []).length;
        li.append(el("span", "tm-pair-n", p.label),
          el("span", "tm-pair-s", p.away ? "set aside: didn't answer" : p.streams ? `${busy} of ${p.streams} streams${who.length ? ` · ${who.join(", ")}` : ""}${p.held ? " · held for agents" : ""}`
            : p.state === "ready" ? "on another model" : p.state));
        ul.appendChild(li);
      }
      head.appendChild(ul);
    }
    if ((t.decisions || []).length) {  // the conductor's latest moves and holds
      const d = el("details", "tm-decisions");
      d.appendChild(el("summary", "", `Conductor · ${t.decisions[0].text}`));
      for (const x of t.decisions.slice(1)) d.appendChild(el("p", "tm-decision", x.text));
      head.appendChild(d);
    }
    box.appendChild(head);
    if (!agents.length) {
      box.appendChild(el("p", "tm-empty", t.cap ? "No agents yet. Ask the chat in plain words, like “spin up 3 agents to …”, and they show up here as they start."
        : "No agents in this chat. Turn Team mode on with Change (or the Team button by the composer), then ask the chat to spin up agents."));
      return;
    }
    const list = el("ul", "tm-list");
    for (const a of agents) {
      const li = el("li", `tm-card st-${a.status}`);
      const b = el("button", "tm-card-b"); b.type = "button";
      b.setAttribute("aria-label", `${a.name}, ${AG_DOT[a.status] || a.status}. Open its timeline`);
      const top = el("span", "tm-card-top");
      top.append(statusDot(a), el("span", "tm-name", a.name), el("span", "tm-id", `${a.id} \u00b7 ${AG_DOT[a.status] || a.status}`));
      const h = teamLive(a) && healthTag(a.health, `list-${a.id}`, false); if (h) top.appendChild(h);
      top.appendChild(ticking("tm-time", a.elapsed, a.status === "running"));
      const now = el("span", "tm-now");
      if (teamLive(a)) {
        now.append(document.createTextNode(a.stopping === "wrap" ? "Stopped: writing its report" : a.doing || (a.status === "queued" ? "Waiting for a free slot" : "Working")));
        if (a.doing_for != null && a.status === "running") { now.append(document.createTextNode(" · ")); now.appendChild(ticking("", a.doing_for, true)); }
      } else now.textContent = a.status === "done" ? (a.report_line || "Finished") : why(a.error || AG_DOT[a.status]);
      const meta = [a.pair_label, a.steps ? `${a.steps} step${a.steps === 1 ? "" : "s"}` : null, a.calls ? `${a.calls} call${a.calls === 1 ? "" : "s"}` : null,
        a.tps ? `${a.tps} tok/s` : null, a.pp ? `pp ${kTok(a.pp)}/s` : null, a.role === "explore" ? "explorer" : null, a.fork ? "forked" : null, a.workdir ? `in ${a.workdir}/` : null, a.sandbox ? "sandboxed" : null, a.retry_of ? `again from ${a.retry_of}` : null].filter(Boolean);
      b.append(top, now);
      if (meta.length) b.appendChild(el("span", "tm-meta", meta.join(" · ")));
      if (teamLive(a) && a.health && a.health.line && AG_WARN[a.health.state]) b.appendChild(el("span", `tm-hline ${AG_BAD.has(a.health.state) ? "bad" : ""}`, a.health.line));
      if (teamLive(a) && a.checkin && a.checkin.line) b.appendChild(el("span", "tm-cline", `Check-in ${a.checkin.n} · ${clock(a.checkin.t)}: ${a.checkin.line}`));
      b.dataset.aid = a.id;
      b.addEventListener("click", () => showAgent(a.id));
      li.appendChild(b);
      list.appendChild(li);
    }
    box.appendChild(list);
    box.scrollTop = keep;
    tipFollow(box);
    if (fk) { const g = [...box.querySelectorAll("button")].find((x) => (x.dataset.aid ? `a:${x.dataset.aid}` : `t:${x.textContent}`) === fk); if (g) g.focus({ preventScroll: true }); }
  }
  // one timeline event (chat_team.Team._log): its node, keyed by its number so an update replaces it in place
  function agEvent(e) {
    const who = { owner: "You", lead: "The lead", watcher: "The watcher" };
    let n;
    if (e.k === "model") {
      const meta = [e.out ? `${num(e.out)} tokens` : null, e.tps ? `${e.tps} tok/s` : null, e.pp ? `pp ${kTok(e.pp)}/s` : null,
        e.wait ? `waited ${e.wait} s` : null].filter(Boolean).join(" \u00b7 ");
      if (e.calls && !e.text) {  // it only chose its next call: one quiet line, its thinking inside
        n = el(e.think ? "details" : "div", "tm-ev tm-thought");
        const h = el(e.think ? "summary" : "p", "tm-ev-h");
        h.append(el("span", "tm-ev-k", `Thought for ${e.secs} s`), el("span", "tm-ev-m", meta));
        n.appendChild(h);
        if (e.think) n.appendChild(el("div", "tm-pre", e.think));
      } else {
        n = el("div", "tm-ev tm-model");
        const h = el("p", "tm-ev-h");
        h.append(el("span", "tm-ev-k", e.calls ? "Said, then called tools" : "Answered"), el("span", "tm-ev-m", [`${e.secs} s`, meta].filter(Boolean).join(" \u00b7 ")));
        n.appendChild(h);
        if (e.think) { const d = el("details", "tm-think"); d.append(el("summary", "", "Thinking"), el("div", "tm-pre", e.think)); n.appendChild(d); }
        if (e.text) n.appendChild(el("div", "tm-text", e.text));
      }
    } else if (e.k === "tool") {
      n = el("details", `tm-ev tm-tool st-${e.status}`);
      const sm = el("summary", "tm-ev-h");
      const ic = el("span", `tm-tool-ic st-${e.status}`); ic.setAttribute("aria-hidden", "true");
      sm.append(ic, el("span", "tm-tool-l", e.line || e.name));
      if (e.status === "running") sm.appendChild(ticking("tm-ev-m", (Date.now() / 1000) - e.t, true));
      else sm.appendChild(el("span", "tm-ev-m", [e.status !== "ok" ? e.status : null, e.secs != null ? `${e.secs} s` : null].filter(Boolean).join(" · ")));
      n.appendChild(sm);
      const b = el("div", "tm-tool-b");
      b.append(el("p", "tm-lab", `${e.name} · arguments`), el("pre", "tm-pre", e.args || "{}"));
      if (e.result != null) b.append(el("p", "tm-lab", "Result"), el("pre", "tm-pre", e.result || "(nothing)"));
      n.appendChild(b);
    } else if (e.k === "note") {
      n = el("div", `tm-ev tm-note${e.who === "owner" ? " mine" : ""}`);
      n.append(el("p", "tm-ev-h", `${who[e.who] || "A note"} → ${AG.d ? AG.d.name : "agent"}`), el("div", "tm-text", e.text));
      n.appendChild(el("p", "tm-ev-m", e.read ? "Read" : "It reads this before its next step"));
    } else if (e.k === "watch") {
      n = el("div", `tm-ev tm-watch ${AG_BAD.has(e.state) || e.state === "stalled" ? "bad" : ""}`);
      n.append(el("p", "tm-ev-h", `Watcher · ${e.state === "nudged" ? "Sent back to it" : AG_WARN[e.state] || e.state}`), el("div", "tm-text", e.text || ""));
    } else if (e.k === "checkin") {
      n = el("div", "tm-ev tm-checkin");
      n.append(el("p", "tm-ev-h", `Check-in ${e.ck} \u00b7 ${dur(e.elapsed)} in \u00b7 ${num(e.steps)} steps \u00b7 sent to the lead`), el("div", "tm-text", e.text || ""));
    } else if (e.k === "end") {
      n = el("div", `tm-ev tm-end st-${e.status}`);
      n.append(el("p", "tm-ev-h", { done: "Finished", failed: "Failed", stopped: "Stopped", interrupted: "Interrupted" }[e.status] || e.status));
      if (e.text && e.status !== "done") n.appendChild(el("div", "tm-text", why(e.text)));
    } else {
      n = el("p", "tm-ev tm-line", e.text || e.k);
    }
    n.dataset.n = String(e.n);
    const at = new Date(e.t * 1000);
    n.title = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
    return n;
  }
  function renderAgDetail(newEvents) {
    const box = $("tm-body"), d = AG.d, a = agentOf(AG.aid) || d;
    $("tm-title").textContent = (a && a.name) || AG.aid;
    if (!d) { if (!box.firstChild) box.appendChild(el("p", "tm-empty", "Reading the agent…")); $("tm-say").hidden = true; return; }
    for (const x of [...box.children]) if (x.classList.contains("tm-empty")) x.remove();  // "Reading the agent…"
    const live = teamLive(d);
    // the head: status, health, what it is doing, the numbers (drawn anew; the timeline below keeps its nodes)
    let head = box.querySelector(".tm-dhead");
    const nh = el("div", "tm-dhead");
    const st = el("p", "tm-dstatus");
    st.append(statusDot(d), el("span", "", `${AG_DOT[d.status] || d.status}`), el("span", "tm-id", d.id));
    const h = live && healthTag(d.health, `agent-${d.id}`, true); if (h) st.appendChild(h);
    nh.appendChild(st);
    if (live) {
      const now = el("p", "tm-dnow");
      now.append(document.createTextNode(d.doing || "Waiting for a free slot"));
      if (d.doing_for != null && d.status === "running") { now.append(document.createTextNode(" · ")); now.appendChild(ticking("", d.doing_for, true)); }
      nh.appendChild(now);
      if (d.health && d.health.line && AG_WARN[d.health.state]) nh.appendChild(el("p", `tm-hline ${AG_BAD.has(d.health.state) ? "bad" : ""}`, d.health.line));
    } else if (d.error) nh.appendChild(el("p", "tm-hline bad", why(d.error)));
    const meta = el("p", "tm-dmeta");
    meta.appendChild(ticking("", d.elapsed, d.status === "running"));
    const avg = d.speed || {};
    for (const x of [d.max_steps ? `${d.steps} of ${d.max_steps} steps` : `${num(d.steps)} step${d.steps === 1 ? "" : "s"}`, `${num(d.calls)} call${d.calls === 1 ? "" : "s"}`,
      avg.tps || d.tps ? `${avg.tps || d.tps} tok/s` : null, avg.pp || d.pp ? `pp ${kTok(avg.pp || d.pp)}/s` : null,
      avg.wait ? `waited ${avg.wait} s` : null,
      d.out_tokens ? `${num(d.out_tokens)} tokens written` : null, d.pair_label, d.sandbox ? "sandboxed" : null, d.retry_of ? `again from ${d.retry_of}` : null]) {
      if (x) meta.append(document.createTextNode(` \u00b7 ${x}`));
    }
    nh.appendChild(meta);
    if (d.max_steps) {  // a cap the user asked for: how much of it is used (agents have none by default)
      const bar = el("div", "tm-bar"); bar.setAttribute("role", "progressbar"); bar.setAttribute("aria-label", "Steps used");
      bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", String(d.max_steps)); bar.setAttribute("aria-valuenow", String(d.steps));
      const fill = el("span"); fill.style.width = `${Math.min(100, (100 * d.steps) / Math.max(1, d.max_steps))}%`; bar.appendChild(fill);
      nh.appendChild(bar);
    }
    const acts = el("div", "tm-acts");
    if (live) {
      // Stop: it stops what it's doing and writes its report; while it writes: Stop now (at once)
      const wrap = d.stopping === "wrap", hard = d.stopping === "now";
      const s = el("button", "tm-btn", hard ? "Stopping…" : wrap ? "Stop now" : "Stop"); s.type = "button";
      s.disabled = hard;
      s.title = wrap ? "It's writing its report; this stops it at once" : hard ? "" : "It stops what it's doing and writes its report";
      s.addEventListener("click", () => agAct(`/${d.id}/stop`, wrap ? { now: true } : {}, () => pollAgent()));
      acts.appendChild(s);
    } else {
      const r = el("button", "tm-btn", "Run again"); r.type = "button";
      r.addEventListener("click", () => agAct(`/${d.id}/retry`, {}, (x) => { if (x && x.agent) showAgent(x.agent); }));
      acts.appendChild(r);
    }
    st.appendChild(acts);
    const focused = head && head.contains(document.activeElement) ? document.activeElement.textContent : null;
    if (head) head.replaceWith(nh); else box.appendChild(nh);
    if (focused) { const f = [...nh.querySelectorAll("button, [tabindex='0']")].find((x) => x.textContent === focused); if (f) f.focus({ preventScroll: true }); }
    tipFollow(nh);
    // the tabs
    let tabs = box.querySelector(".tm-tabs");
    if (!tabs) {
      tabs = el("div", "tm-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "Show");
      for (const [k, label] of [["activity", "Activity"], ["brief", "Brief"], ["report", "Report"]]) {
        const b = el("button", "tm-tab", label); b.type = "button"; b.setAttribute("role", "tab"); b.dataset.tab = k;
        b.addEventListener("click", () => { AG.tab = k; renderAgDetail(null); });
        tabs.appendChild(b);
      }
      box.appendChild(tabs);
    }
    for (const b of tabs.children) { const on = b.dataset.tab === AG.tab; b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1; }
    const cks = AG.cks;
    tabs.querySelector('[data-tab="report"]').textContent = d.report ? "Report" : cks.length ? `Report · check-in ${cks[cks.length - 1].n}` : "Report · none yet";
    let pane = box.querySelector(".tm-pane");
    const want = AG.tab;
    if (!pane || pane.dataset.tab !== want) {
      const np = el("div", "tm-pane"); np.dataset.tab = want; np.setAttribute("role", "tabpanel");
      if (pane) pane.replaceWith(np); else box.appendChild(np);
      pane = np; newEvents = null;
    }
    if (want === "brief") { pane.replaceChildren(el("div", "tm-doc", d.brief || "")); }
    else if (want === "report") {
      if (d.report) pane.replaceChildren(el("div", "tm-doc", d.report));
      else if (cks.length) {  // still working: its latest check-in, the ones before folded
        const last = cks[cks.length - 1];
        pane.replaceChildren(el("p", "tm-lab", `Latest check-in (${last.n}) \u00b7 ${clock(last.t)} \u00b7 ${dur(last.elapsed)} in \u00b7 ${num(last.steps)} steps`),
          el("div", "tm-doc", last.text));
        if (live) pane.appendChild(el("p", "tm-empty", "It checks in about every hour and writes its report when it's done."));
        for (const c of cks.slice(0, -1).reverse()) {
          const x = el("details", "tm-think"); x.append(el("summary", "", `Check-in ${c.n} \u00b7 ${clock(c.t)} \u00b7 ${dur(c.elapsed)} in`), el("div", "tm-doc", c.text));
          pane.appendChild(x);
        }
      } else pane.replaceChildren(el("p", "tm-empty", live ? "It writes its report when it's done (and a check-in about every hour); the lead reads it then." : "No report."));
    } else {
      // the timeline: new events added, updated ones replaced in place (an open tool call stays open), the view kept
      // at the end only while it was there
      const atEnd = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 60;
      const full = newEvents === null, evs = full ? AG.log : newEvents || [];
      if (full) pane.replaceChildren();
      for (const e of evs) {
        const old = pane.querySelector(`[data-n="${e.n}"]`), node = agEvent(e);
        if (old) { if (old.open) node.open = true; old.replaceWith(node); } else pane.appendChild(node);
      }
      const none = pane.querySelector(".tm-empty");
      if (none && pane.children.length > 1) none.remove();
      if (!pane.firstChild) pane.appendChild(el("p", "tm-empty", "Nothing yet."));
      if (full) pane.scrollTop = live ? pane.scrollHeight : 0;  // a working agent at its latest step, a finished one from the start
      else if ((atEnd || AG.stick) && evs.length) { pane.scrollTop = pane.scrollHeight; AG.stick = false; }
    }
    // writing to it
    const say = $("tm-say");
    say.hidden = !live;
    $("tm-say-t").placeholder = `Write to ${d.name}… it reads it before its next step`;
  }
  function wireAgents() {
    if (!$("tm")) return;
    $("tm-close").addEventListener("click", closeAgents);
    $("tm-back").addEventListener("click", () => showAgent(null));
    $("tm").addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || (e.target === $("tm-say-t") && $("tm-say-t").value)) return;
      e.stopPropagation();
      if (AG.aid) showAgent(null); else closeAgents();
    });
    const ta = $("tm-say-t");
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("tm-say").requestSubmit(); } });
    $("tm-say").addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = ta.value.trim(), aid = AG.aid;
      if (!text || !aid || AG.sending) return;
      AG.sending = true; $("tm-say-go").disabled = true;
      try { await call("POST", `/studio/api/chats/${C.id}/team/${aid}/message`, { text }); ta.value = ""; AG.stick = true; pollAgent(); }
      catch (err) { K.toast(err.message, true); }
      AG.sending = false; $("tm-say-go").disabled = false; ta.focus();
    });
    document.addEventListener("halo:viewer-open", closeAgents);  // studio.js: the viewer takes the column
  }
  function syncPerm() {
    syncTeam();
    const b = $("t-perm"), lv = curPerm();
    let off = !!(C.chat && C.chat.cousteau);
    b.hidden = off;
    $("t-perm-ic").innerHTML = PERM_IC[lv] || (lv === "direct" ? PERM_IC.full : PERM_IC.none);
    $("t-perm-k").textContent = PERM_SHORT[lv] || "Off";
    b.classList.toggle("full", ["full", "direct"].includes(lv)); b.classList.toggle("lv-none", lv === "none");
    b.setAttribute("aria-label", `Permissions: ${PERM_NAME[lv] || "No tools"}. Change`);
    const line = $("t-perm-line"), o = permOpts();
    line.replaceChildren();
    if (C.chat && C.chat.private) {  // a Private chat: the line says so, and Lock is one tap away
      line.hidden = false; line.classList.add("priv");
      line.append(svg(LOCK_IC, "t-priv-ic"), document.createTextNode(" Private \u00b7 sealed on this Mac \u00b7 "));
      const lk = el("button", "textbtn", "Lock"); lk.type = "button"; lk.addEventListener("click", lockPrivate);
      line.appendChild(lk);
      return;
    }
    line.classList.remove("priv");
    line.hidden = off || !["full", "direct"].includes(lv);
    if (!line.hidden && lv === "direct") {
      const computer = o.computer || (C.newPermBody || {}).computer;
      const name = (((C.perms || {}).computers || []).find((t) => t.id === computer) || {}).name || (computer === "mac" ? "Mac" : computer);
      line.textContent = `Direct access · ${name || "Choose computer"} · no approval prompts`;
      return;
    }
    if (!line.hidden) line.textContent = `Full access on this Mac${o.folder ? " \u00b7 " + o.folder : ""}`;
  }
  async function loadPerms(fresh) {
    const q = new URLSearchParams({ profile: curModel() });
    if (C.id) q.set("chat", C.id);
    if (fresh) q.set("fresh", "1");
    try { C.perms = await call("GET", `/studio/api/chat/perms?${q}`); } catch (e) { C.perms = null; }
  }
  function openPerms() {
    if (!$("t-sheet-wrap").hidden && (C.view === "perms" || C.view === "perms-full" || C.view === "perms-direct")) { closeSheet(); return; }
    if ($("t-sheet-wrap").hidden) openSheet();
    C.view = "perms"; C.fromPerm = true; $("t-perm").setAttribute("aria-expanded", "true"); renderSheet();
    // focus on the checked level once it is drawn (it stayed on the chip)
    loadPerms().then(() => { if (!$("t-sheet-wrap").hidden && C.view.startsWith("perms")) { renderSheet(); const f = $("t-sheet-body").querySelector("[aria-checked='true'], button"); if (f) f.focus(); } });
  }
  // a chat's level (PUT; a new chat sends it with its first message, POST /chats)
  async function setPerm(lv, extra, onErr) {
    const body = Object.assign({ perm: lv }, extra || {});
    if (!C.id) { C.newPerm = lv === "none" ? null : lv; C.newPermBody = lv === "none" ? null : body; syncPerm(); redrawHello(); return true; }
    try {
      const c = await call("PUT", `/studio/api/chats/${C.id}`, body);
      Object.assign(C.chat, { perm: c.perm, perm_opts: c.perm_opts });
      syncPerm(); reload(); return true;
    } catch (e) { if (onErr) onErr(e.message); else K.toast(e.message, true); return false; }
  }
  function renderPerms(box) {
    const P = C.perms;
    if (!P) { box.appendChild(el("p", "t-sheet-note", "Reading the permissions\u2026")); return; }
    const card = el("div", "t-card"); card.setAttribute("role", "menu"); card.setAttribute("aria-label", "Permissions");
    for (const r of P.levels) {
      const b = row(r.name, r.offered ? r.desc : `${r.desc} ${r.why}.`, r.id === curPerm(), async () => {
        if (!r.offered || (r.id === curPerm() && !["full", "direct"].includes(r.id))) return;
        if (r.id === "direct") {
          C.view = "perms-direct"; C.directComputer = permOpts().computer || (C.newPermBody || {}).computer || "mac";
          renderSheet(); return;
        }
        if (r.id === "full") {  // also while Full is on: its scope and folders change there
          C.view = "perms-full"; C.fullScope = curPerm() === "full" ? (permOpts().scope || "folder") : "folder"; C.fullDirs = null;
          renderSheet(); return;
        }
        if (await setPerm(r.id)) { closeSheet(); $("t-prompt").focus(); }
      });
      if (!r.offered) { b.classList.add("off"); b.setAttribute("aria-disabled", "true"); }
      card.appendChild(b);
    }
    box.appendChild(card);
    if (P.toolbox && !P.toolbox.ok) box.appendChild(el("p", "t-sheet-note t-err", P.toolbox.words));
    if (P.private) {  // the accepted limit of Private chats
      box.appendChild(el("p", "t-sheet-note", "In a Private chat the container's folder is sealed on Toolbox with this chat's key and opened only while a tool runs. While it runs, an administrator of Toolbox could read it. Web searches and pages it reads go out to the internet."));
    }
    if (P.levels.some((r) => (r.id === "box" || r.id === "box_web") && r.offered)) {
      box.appendChild(el("p", "t-sheet-note", "The container's folder on Toolbox stays while the chat exists. It is removed when you delete the chat, or after 30 days without use."));
    }
    box.appendChild(el("p", "t-sheet-note", "A change applies from your next message. Lowering it also stops the tools above it in an answer that is running."));
  }
  function renderPermsDirect(box) {
    box.appendChild(el("p", "t-sheet-note", "The model stays on the main boxes. Choose where it runs commands and edits files."));
    const card = el("div", "t-card");
    const label = el("label", "t-sheet-note", "Computer"); label.htmlFor = "t-direct-computer";
    const select = el("select", "t-input"); select.id = "t-direct-computer";
    select.style.cssText = "display:block;width:calc(100% - 40px);margin:12px 20px 20px;padding:12px;font:inherit;border-radius:12px";
    for (const target of (C.perms.computers || [])) {
      const opt = el("option", "", target.name + (target.available ? "" : ` (${target.off || "not connected"})`));
      opt.value = target.id; opt.disabled = !target.available; select.appendChild(opt);
    }
    const on = (C.perms.computers || []).filter((t) => t.available);  // the first one switched on, unless the user picked one
    select.value = on.some((t) => t.id === C.directComputer) ? C.directComputer : (on[0] ? on[0].id : "");
    select.addEventListener("change", () => { C.directComputer = select.value; });
    card.append(label, select); box.appendChild(card);
    box.appendChild(el("p", "t-sheet-note", "Commands use that computer’s account permissions, without approval prompts, container isolation or a network filter. Your private-folder exclusion still applies."));
    const error = el("p", "t-sheet-note t-err"); error.setAttribute("role", "alert"); box.appendChild(error);
    const go = el("button", "go solid", "Use Direct access"); go.type = "button";
    go.addEventListener("click", async () => {
      go.disabled = true;
      if (await setPerm("direct", { computer: select.value }, (text) => { error.textContent = text; })) {
        closeSheet(); $("t-prompt").focus();
      } else go.disabled = false;
    });
    box.appendChild(go);
  }
  function renderPermsFull(box) {
    const F = (C.perms && C.perms.full) || {};
    const w = el("div", "t-confirm");
    for (const line of F.confirm || ["It can run commands and change files in its own folder on this Mac.", "Risky commands wait for your tap.",
      "The earlier messages in this chat stay in the conversation, and the model reads them with its new access."]) w.appendChild(el("p", "", line));
    if ((F.scopes || []).length > 1) {
      const card = el("div", "t-card"); card.setAttribute("role", "menu"); card.setAttribute("aria-label", "What it can read");
      for (const sc of F.scopes) card.appendChild(row(sc.name, [sc.desc, sc.residual].filter(Boolean).join(" "), (C.fullScope || "folder") === sc.id, () => { C.fullScope = sc.id; renderSheet(); }));
      w.appendChild(card);
    }
    const FD = F.folders, err = el("p", "t-sheet-note t-err t-full-err"); err.setAttribute("role", "alert"); err.hidden = true;
    const say = (m) => { err.textContent = m || ""; err.hidden = !m; };
    if (FD) {  // the folders the user adds (chat_full.view's words): read only or read and write; checked by the server
      if (!C.fullDirs) C.fullDirs = (FD.dirs || []).map((d) => ({ path: d.path, write: !!d.write }));
      w.appendChild(el("p", "t-sheet-sec", FD.heading));
      const list = el("div", "t-card t-fdirs");
      if (!C.fullDirs.length) list.appendChild(el("p", "t-fdir-none", FD.none));
      C.fullDirs.forEach((d, i) => {
        const r = el("div", "t-fdir"), mode = el("button", "t-fdir-mode", d.write ? FD.rw : FD.ro), rm = el("button", "h-round t-fdir-rm");
        mode.type = rm.type = "button";
        mode.setAttribute("aria-pressed", String(d.write)); mode.setAttribute("aria-label", `${d.path}: ${d.write ? FD.rw : FD.ro}`);
        rm.setAttribute("aria-label", `${FD.remove} ${d.path}`); rm.appendChild(svg('<path d="M6 6l12 12M18 6L6 18"/>'));
        mode.addEventListener("click", () => { d.write = !d.write; renderSheet(); });
        rm.addEventListener("click", () => { C.fullDirs.splice(i, 1); renderSheet(); });
        r.append(el("code", "t-fdir-p", d.path), mode, rm); list.appendChild(r);
      });
      w.appendChild(list);
      if (C.fullDirs.length < (FD.max || 6)) {
        const f = el("form", "t-fadd"), inp = el("input", "t-fadd-in"), add = el("button", "", FD.add_b);
        f.noValidate = true; inp.type = "text"; inp.placeholder = FD.placeholder; inp.setAttribute("aria-label", FD.add);
        inp.autocomplete = "off"; inp.autocapitalize = "off"; inp.spellcheck = false; add.type = "submit";
        inp.addEventListener("input", () => say(""));
        f.addEventListener("submit", (e) => {
          e.preventDefault();
          const v = inp.value.trim();
          if (!v) return;
          if (!(v.startsWith("~/") || v.startsWith("/"))) { say(FD.full_path); return; }
          if (!C.fullDirs.some((d) => d.path === v)) C.fullDirs.push({ path: v, write: false });
          renderSheet(); const n = $("t-sheet-body").querySelector(".t-fadd-in"); if (n) n.focus();
        });
        f.append(inp, add); w.appendChild(f);
      }
    }
    w.appendChild(err);
    const on = curPerm() === "full" && !!C.id;
    const bt = el("div", "t-tap-b"), go = el("button", "go", on ? "Save" : "Turn on"), no = el("button", "", "Cancel");
    go.type = no.type = "button";
    go.addEventListener("click", async () => {
      go.disabled = true; say("");
      const typed = ($("t-sheet-body").querySelector(".t-fadd-in") || {}).value;  // a path typed and not added yet goes too
      if (typed && typed.trim() && (typed.trim().startsWith("~/") || typed.trim().startsWith("/")) && !C.fullDirs.some((d) => d.path === typed.trim())) C.fullDirs.push({ path: typed.trim(), write: false });
      const body = { confirm: true, scope: C.fullScope || "folder" };
      if (FD) body.dirs = C.fullDirs.map((d) => ({ path: d.path, write: d.write }));
      if (await setPerm("full", body, say)) { closeSheet(); $("t-prompt").focus(); } else go.disabled = false;
    });
    no.addEventListener("click", () => { C.view = "perms"; renderSheet(); });
    bt.append(go, no); w.appendChild(bt);
    box.appendChild(w);
  }
  function permDivider(m) { return el("div", "t-perm-div", `Permissions: ${PERM_NAME[m.meta.perm_to] || m.meta.perm_to}`); }
  const STEP_STATE = { running: "Running\u2026", refused: "Not run", denied: "Denied", failed: "Failed", stopped: "Stopped" };
  // a document of the chat (chat_docs): a PDF opens in the app's viewer (beside the chat; over it on a phone),
  // and Download saves the file (a phone's share sheet). Opening the file itself left the installed app on a page with
  // no way back, and its download went nowhere. A Mac's ⌘-click still opens it in a tab, but not a
  // Private chat's: its grant goes in a header
  function docLink(a, f, download) {
    const url = `/studio/doc/${f.id}${download ? "?dl=1" : ""}`, priv = !!(C.chat && C.chat.private);
    a.title = download ? `Download ${f.name || "the file"}` : `Open ${f.name || "the PDF"}`;
    a.href = priv ? "#" : url;
    a.addEventListener("click", (e) => {
      if (!priv && (e.metaKey || e.ctrlKey || e.shiftKey || e.button)) return;
      e.preventDefault();
      if (download) K.saveFile(url, f.name || "document"); else K.openDoc(f, priv);
    });
    return a;
  }
  function bytesWords(n) {
    return !n ? "" : n >= 1048576 ? `${(n / 1048576).toFixed(1).replace(/\.0$/, "")} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
  }
  function docCard(f) {  // a file a tool made: its page 1, its name, Open and Download
    const w = el("div", "t-doc");
    w.dataset.file = f.id;
    if (f.thumb && !(C.chat && C.chat.private)) {
      const a = docLink(el("a", "t-doc-thumb"), f, f.kind !== "pdf"), i = el("img");
      i.src = `/studio/ref/${f.id}.thumb`; i.alt = ""; i.loading = "lazy";
      i.addEventListener("error", () => { a.replaceChildren(svg(IC.file)); a.classList.add("t-doc-ic"); }, { once: true });
      a.appendChild(i); w.appendChild(a);
    } else {
      const ic = el("span", "t-doc-thumb t-doc-ic"); ic.appendChild(svg(IC.file)); w.appendChild(ic);
    }
    const m = el("div", "t-doc-m"), b = el("div", "t-doc-b");
    const detail = String(f.detail || "").split(" · ").filter((x) => x && !/ words?$/.test(x));  // its pages, form and size
    m.append(el("span", "t-doc-n", f.name || "document"), el("small", "", [...detail, bytesWords(f.bytes)].filter(Boolean).join(" · ")));
    if (f.kind === "pdf") b.appendChild(docLink(el("a", "t-doc-go go", "Open"), f, false));
    b.appendChild(docLink(el("a", "t-doc-go" + (f.kind === "pdf" ? "" : " go"), "Download"), f, true));
    m.appendChild(b); w.appendChild(m);
    return w;
  }
  const OPEN_STEPS = new Set();  // the steps the user opened (tool call ids), in this page only
  // update_plan's checklist (chat_plan): the newest of an answer whole, earlier ones folded to their line (CSS)
  const PLAN_MARK = { pending: "○", in_progress: "◐", done: "✓" };
  function planCard(t) {
    const w = el("div", "t-plan " + (t.status || "running"));
    if (t.id) w.dataset.sid = t.id;
    w.appendChild(el("p", "t-plan-h", t.line || "Plan"));
    const ul = el("ul", "t-plan-list");
    for (const s of t.plan || []) {
      const li = el("li", "t-plan-" + s.status);
      li.append(el("span", "t-plan-m", PLAN_MARK[s.status] || "○"), el("span", "", s.step));
      ul.appendChild(li);
    }
    w.appendChild(ul);
    return w;
  }
  function stepBlock(t, more) {
    if (t.plan) return planCard(t);
    if (t.file) {  // the step, then the file it made, which stays in view when the step is folded
      const w = el("div", "t-step-wrap");
      w.append(stepBlock(Object.assign({}, t, { file: null }), more), docCard(t.file));
      return w;
    }
    const d = el("details", "t-step " + (t.status || "running")), s = el("summary");
    // a step the user opened stays open when it finishes and through every redraw: kept by the tool call's id, which
    // the stored step shares
    if (t.id) {
      d.dataset.sid = t.id;
      if (OPEN_STEPS.has(t.id)) d.open = true;
      d.addEventListener("toggle", () => { if (d.open) OPEN_STEPS.add(t.id); else OPEN_STEPS.delete(t.id); });
    }
    s.append(el("span", "t-step-l", t.line || t.name || "Tool"), el("span", "t-step-s", STEP_STATE[t.status] || (t.secs != null ? `${t.secs} s` : "Done")));
    d.appendChild(s);
    const k = t.id ? `${C.id}:${t.id}` : null, has = t.out != null || t.live != null;
    if (k && t.out) { OUTS.set(k, t.out); if (OUTS.size > 500) OUTS.delete(OUTS.keys().next().value); }
    const pre = el("pre", "t-step-out", has ? t.out || t.live || "" : (k && OUTS.get(k)) || "");
    d.appendChild(pre);
    if (more && !has && t.out_n && !(k && OUTS.has(k))) {
      fillOnOpen(d, pre, () => more().then((x) => { const o = x.out || x.live || ""; if (k && o) OUTS.set(k, o); return o; }));
    }
    if (t.shown) { const i = el("img", "t-step-shown"); i.src = `/studio/ref/${t.shown}.thumb`; i.alt = ""; i.loading = "lazy"; d.appendChild(i); d.open = true; }
    if ((t.refused || []).length) d.appendChild(el("p", "t-note", t.refused.slice(0, 5).join("; ")));
    return d;
  }
  function tapCard(a) {
    const w = el("div", "t-tap");
    w.appendChild(el("p", "", a.why ? `Waiting for your tap: ${a.why}.` : "Waiting for your tap."));
    w.appendChild(el("code", "", a.command || ""));
    if (a.folder) w.appendChild(el("p", "t-tap-f", `In ${a.folder}`));
    if (a.open) {
      const b = el("div", "t-tap-b"), chat = C.id;
      for (const [d, label] of [["allow", "Allow once"], ["allow_all", "Allow all"], ["deny", "Deny"]]) {
        const x = el("button", d === "allow" ? "go" : "", label); x.type = "button";
        if (d === "allow_all") x.title = "Approve this and future requests in this chat. Existing access limits still apply.";
        x.addEventListener("click", async () => {
          b.querySelectorAll("button").forEach((y) => { y.disabled = true; });
          try {
            const r = await call("POST", `/studio/api/chats/${chat}/answer`, { ask: a.id, decision: d });
            if (!r.ok) throw new Error("This request is no longer waiting for approval.");
          }
          catch (e) { K.toast(e.message, true); b.querySelectorAll("button").forEach((y) => { y.disabled = false; }); }
        });
        b.appendChild(x);
      }
      w.appendChild(b);
    } else {
      w.appendChild(el("p", "t-tap-done", a.decision === "allow" ? (a.how === "chat_all" ? "Allow all enabled for this chat." : a.how === "chat_auto" ? "Automatically approved for this chat." : "Allowed once.") : a.how === "timeout" ? "Denied: no answer in 10 minutes."
        : a.how === "stop" ? "Denied: the answer was stopped." : "Denied."));
      if (a.how === "chat_all" || a.how === "chat_auto") {
        const buttons = el("div", "t-tap-b"), revoke = el("button", "", "Ask each time"), chat = C.id;
        revoke.type = "button";
        revoke.addEventListener("click", async () => {
          revoke.disabled = true;
          try { await call("POST", `/studio/api/chats/${chat}/answer`, { decision: "ask_each" }); revoke.textContent = "Auto-approval turned off"; }
          catch (e) { K.toast(e.message, true); revoke.disabled = false; }
        });
        buttons.appendChild(revoke); w.appendChild(buttons);
      }
    }
    return w;
  }
  // a live answer: what it wrote so far stays, the step or card follows, and later words go below it
  function segment(T, node) {
    cancelAnimationFrame(T.raf); T.raf = 0;
    if (T.text) MD.render(T.body, T.text, { onCopy: copy }); else T.body.remove();
    T.node.appendChild(node);
    T.body = el("div", "msg-body t-body"); T.node.appendChild(T.body); T.text = "";
    follow();
  }
  function showOffline(id, body, words) {
    C.offline = { id, body };
    $("t-offline-t").textContent = words || "Toolbox isn't answering, so this chat's container can't run.";
    $("t-offline").hidden = false;
  }
  function hideOffline() { C.offline = null; $("t-offline").hidden = true; }

  // ---------------------------------------------------------------- the model sheet (bottom sheet on phones, popover above the chip on desktop)
  // on the Mac the sheet opens above the composer: never taller than the room there (a tall composer pushed it off the
  // top of the window, out of reach)
  function fitSheet() {
    const s = $("t-sheet"), wrap = $("t-sheet-wrap");
    if (window.matchMedia && window.matchMedia("(max-width: 860px)").matches) { s.style.maxHeight = ""; wrap.classList.remove("top"); return; }
    const bar = document.querySelector(".topbar"), barB = bar && bar.offsetParent !== null ? bar.getBoundingClientRect().bottom : 0;
    const room = wrap.parentElement.getBoundingClientRect().top - 8 - 16 - barB;  // below the top bar, so the sheet's × shows
    const top = room < 260;  // too little room above the composer: the sheet opens under the header instead (N3, round 3)
    wrap.classList.toggle("top", top);
    s.style.maxHeight = `${top ? Math.max(200, Math.min(600, window.innerHeight - 120)) : Math.min(600, Math.round(room))}px`;
  }
  function openSheet() {
    C.sheet = document.activeElement; C.view = "main"; C.fromCtx = false; C.fromPerm = false;
    $("t-sheet-wrap").hidden = false; $("t-model").setAttribute("aria-expanded", "true");
    fitSheet();
    renderSheet();
    const into = () => { const f = $("t-sheet-body").querySelector("[aria-checked='true'], button"); if (f) f.focus(); };
    // focus goes in again after the models load and the sheet is drawn anew (it fell to the page)
    loadModels().then(() => { if (!$("t-sheet-wrap").hidden) { const had = $("t-sheet-wrap").contains(document.activeElement); renderSheet(); if (had || document.activeElement === document.body) into(); } });
    requestAnimationFrame(() => { $("t-sheet-wrap").classList.add("in"); into(); });
  }
  function closeSheet() {
    if ($("t-sheet-wrap").hidden) return;
    $("t-sheet-wrap").classList.remove("in"); $("t-sheet-wrap").hidden = true; $("t-model").setAttribute("aria-expanded", "false"); $("t-ctx").setAttribute("aria-expanded", "false");
    $("t-perm").setAttribute("aria-expanded", "false"); $("t-team").setAttribute("aria-expanded", "false");
    if (C.sheet && C.sheet.isConnected) C.sheet.focus();
    C.sheet = null;
  }
  async function choose(patch) {
    if (patch.model) store.set("model", patch.model);
    if (patch.effort) store.set("effort", patch.effort);
    if (C.chat && C.id) {
      try {
        const c = await call("PUT", `/studio/api/chats/${C.id}`, patch);
        Object.assign(C.chat, { model: c.model, effort: c.effort });
      }
      catch (e) { K.toast(e.message, true); }
    }
    syncChip(); renderSheet();
  }
  function row(title, desc, on, fn, extra) {
    const b = el("button", "t-opt"); b.type = "button"; b.setAttribute("role", "menuitemradio"); b.setAttribute("aria-checked", String(!!on));
    const tx = el("span", "t-opt-t"); tx.appendChild(el("span", "t-opt-n", title));
    if (extra) tx.firstChild.appendChild(el("span", "t-tag", extra));
    if (desc) tx.appendChild(el("span", "t-opt-d", desc));
    b.appendChild(tx);
    if (on) b.appendChild(svg(IC.check, "t-opt-check"));
    b.addEventListener("click", fn);
    return b;
  }
  function renderSheet() {
    const box = $("t-sheet-body"), M = C.models;
    box.replaceChildren();
    const head = $("t-sheet-h"), x = $("t-sheet-x");
    if (C.view === "unlock") {  // Private's Unlock
      head.textContent = "Unlock Private";
      x.replaceChildren(svg('<path d="M6 6l12 12M18 6L6 18"/>')); x.setAttribute("aria-label", "Close");
      renderUnlock(box);
      return;
    }
    if (C.view === "perms" || C.view === "perms-full" || C.view === "perms-direct") {  // the Permissions chip (chat_tools.view)
      head.textContent = C.view === "perms" ? "Permissions" : C.view === "perms-direct" ? "Direct access" : "Full access";
      const close = C.view === "perms" && C.fromPerm;
      x.replaceChildren(svg(close ? '<path d="M6 6l12 12M18 6L6 18"/>' : IC.back)); x.setAttribute("aria-label", close ? "Close" : "Back");
      if (C.view === "perms") renderPerms(box); else if (C.view === "perms-direct") renderPermsDirect(box); else renderPermsFull(box);
      return;
    }
    if (C.view === "speed") {  // an answer's speeds and every pair's last hour
      head.textContent = "Speed";
      x.replaceChildren(svg('<path d="M6 6l12 12M18 6L6 18"/>')); x.setAttribute("aria-label", "Close");
      renderSpeed(box);
      return;
    }
    if (C.view === "team") {  // the Team chip (chat_team.py); the agents themselves are in the Agents panel
      head.textContent = "Team";
      x.replaceChildren(svg('<path d="M6 6l12 12M18 6L6 18"/>')); x.setAttribute("aria-label", "Close");
      renderTeam(box);
      return;
    }
    if (C.view !== "main") {
      head.textContent = C.view === "effort" ? "Effort" : C.view === "context" ? "Context window" : "More models";
      if (C.view === "context" && C.fromCtx) { x.replaceChildren(svg('<path d="M6 6l12 12M18 6L6 18"/>')); x.setAttribute("aria-label", "Close"); }
      else { x.replaceChildren(svg(IC.back)); x.setAttribute("aria-label", "Back"); }
    } else {
      head.textContent = "Select model";
      x.replaceChildren(svg('<path d="M6 6l12 12M18 6L6 18"/>')); x.setAttribute("aria-label", "Close");
    }
    if (!M) { box.appendChild(el("p", "t-sheet-note", "Reading the models…")); return; }
    const card = el("div", "t-card"); card.setAttribute("role", "menu");
    const tag = (m) => (m.loaded ? (m.state === "ready" ? "Loaded" : "Loading") : null);
    if (C.view === "context") { renderSizes(box, card); return; }
    if (C.view === "main" || C.view === "more") {
      for (const m of M.models.filter((x2) => x2.main === (C.view === "main"))) {
        let on = m.profile === curModel();
        const patch = { model: m.profile };
        card.appendChild(row(m.name, m.desc + (m.context ? ` · ${Math.round(m.context / 1024)}k context` : ""), on,
          () => { choose(patch); if (C.view === "more") { C.view = "main"; renderSheet(); } }, tag(m)));
      }
      box.appendChild(card);
      if (C.view === "main") {
        const e = effortOf(curEffort());
        const ef = el("button", "t-card t-nav"); ef.type = "button";
        ef.append(el("span", "t-opt-n", "Effort"), el("span", "t-nav-v", e ? e.name : "Low"), svg(IC.chev, "t-nav-ch"));
        ef.addEventListener("click", () => { C.view = "effort"; renderSheet(); box.querySelector("button") && box.querySelector("button").focus(); });
        const z = sizeOf(curModel());
        const cx = el("button", "t-card t-nav"); cx.type = "button";
        cx.append(el("span", "t-opt-n", "Context window"), el("span", "t-nav-v", z ? z.k : "64K"), svg(IC.chev, "t-nav-ch"));
        cx.addEventListener("click", () => { C.view = "context"; renderSheet(); box.querySelector("button") && box.querySelector("button").focus(); });
        const pm = el("button", "t-card t-nav"); pm.type = "button";
        pm.append(el("span", "t-opt-n", "Permissions"), el("span", "t-nav-v", PERM_NAME[curPerm()] || "No tools"), svg(IC.chev, "t-nav-ch"));
        pm.addEventListener("click", () => { C.view = "perms"; C.fromPerm = false; renderSheet(); loadPerms().then(() => { if (C.view.startsWith("perms")) renderSheet(); }); });
        const more = el("button", "t-card t-nav"); more.type = "button";
        more.append(el("span", "t-opt-n", "More models"), el("span", "t-nav-v", ""), svg(IC.chev, "t-nav-ch"));
        more.addEventListener("click", () => { C.view = "more"; renderSheet(); box.querySelector("button") && box.querySelector("button").focus(); });
        box.append(ef, cx, pm);
        if (M.models.some((x2) => !x2.main)) box.append(more);
        const s = M.serving;
        box.appendChild(el("p", "t-sheet-note", s && s.state && s.state !== "idle"
          ? ((K.state() || {}).llm || {}).connected ? "Your server has this one model."
          : `Changing the model loads it on the boxes when you send: about a minute, and it stops ${s.name || "the one loaded"}.`
          : "Changing the model loads it on the boxes when you send: about a minute."));
      }
    } else {
      for (const e of M.efforts) card.appendChild(row(e.name, e.desc, e.id === curEffort(), () => { choose({ effort: e.id }); C.view = "main"; renderSheet(); }));
      box.appendChild(card);
    }
  }
  // the context window: its sizes (each a profile the server whitelists; 128K and 256K wait for their checks), how
  // full this chat is, Compact now, and the restart a different size needs (the Switch rules: never over a job on the
  // boxes, an answer being written or an agent session)
  function renderSizes(box, card) {
    const m = modelOf(curModel()), w = C.id && C.chat ? C.chat.window : null, cur = sizeOf(curModel());
    if (w) {
      const use = el("div", "t-card t-use");
      const pct = Math.min(100, Math.round((w.used / Math.max(1, w.context)) * 100));
      use.appendChild(el("p", "t-use-t", `${kOf(w.used)} of ${w.loaded && w.loaded !== w.chosen ? kOf(w.loaded) : (cur ? cur.k : kOf(w.context))} used · ${kOf(Math.max(0, w.context - w.used))} left (${100 - pct}%)${w.measured ? "" : " (estimated)"}`));
      const bar = el("div", "t-use-bar"); bar.setAttribute("role", "progressbar"); bar.setAttribute("aria-valuenow", String(pct)); bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100");
      const fill = el("span", ""); fill.style.width = pct + "%"; bar.appendChild(fill); use.appendChild(bar);
      use.appendChild(el("p", "t-use-d", "A long chat is compacted on its own before it fills the window: older turns become a summary, the newest stay word for word."));
      const cn = el("button", "t-use-go", w.compacting ? "Compacting…" : "Compact now"); cn.type = "button";
      cn.disabled = !!w.compacting || !(C.chat.messages || []).some((x) => x.role === "assistant");
      cn.addEventListener("click", () => { closeSheet(); compactNow(); });
      use.appendChild(cn);
      box.appendChild(use);
    }
    for (const c of (m && m.contexts) || []) {
      const b = row(c.k, c.ready ? c.note : (c.why || "Not tested yet"),
        cur && c.profile === cur.profile, c.ready ? () => chooseSize(c) : () => {});
      if (!c.ready) { b.disabled = true; b.setAttribute("aria-disabled", "true"); b.classList.add("off"); }
      card.appendChild(b);
    }
    box.appendChild(card);
    const sw = C.sw && C.sw.profile === curModel() ? C.sw : null;
    if (w && w.loaded && cur && w.loaded_profile && w.loaded_profile !== cur.profile && sw && sw.restarts) {
      const r = el("div", "t-card t-restart");
      r.appendChild(el("p", "t-use-d", sw.blockers.length ? `${sw.restarts.replace(", about a minute.", "")} once this is done: ${sw.blockers.join("; ")}.` : sw.restarts));
      if (!sw.blockers.length) {
        const go = el("button", "t-use-go", `Restart with ${cur.k}`); go.type = "button";
        go.addEventListener("click", async () => {
          try { await call("POST", "/studio/api/chat/model/start", { profile: curModel(), switch: true }); K.toast(`Restarting ${(m || {}).name || "the model"} with ${cur.k}`); closeSheet(); }
          catch (e) { K.toast(e.message, true); }
          K.poll();
        });
        r.appendChild(go);
      }
      box.appendChild(r);
    } else if (w && w.loaded && cur && w.loaded_profile && w.loaded_profile !== cur.profile) {
      loadSwitch().then((asked) => { if (asked && !$("t-sheet-wrap").hidden && C.view === "context") renderSheet(); });
    }
    box.appendChild(el("p", "t-sheet-note", "The window is set when the model loads: another size restarts it, about a minute. Replies get a little slower in very long chats."));
  }
  async function chooseSize(c) {
    await choose({ model: c.profile });
    C.sw = null;
    renderSheet();
  }
  if ($("t-ctx-top")) $("t-ctx-top").addEventListener("click", () => $("t-ctx").click());  // the phone's ring opens the same sheet
  $("t-ctx").addEventListener("click", () => {
    if (!$("t-sheet-wrap").hidden && C.view === "context") { closeSheet(); return; }
    if ($("t-sheet-wrap").hidden) openSheet();
    C.view = "context"; C.fromCtx = true; $("t-ctx").setAttribute("aria-expanded", "true"); renderSheet();
  });
  // a phone's sheet comes down with a drag (from its handle or header, or anywhere while its list is at the top)
  (() => {
    const sh = $("t-sheet");
    let y0 = null, dy = 0;
    sh.addEventListener("touchstart", (e) => {
      y0 = null;
      if (!window.matchMedia("(max-width: 600px)").matches || e.touches.length !== 1) return;
      if (sh.scrollTop > 0 && !e.target.closest(".t-grab, .t-sheet-head")) return;
      y0 = e.touches[0].clientY; dy = 0;
    }, { passive: true });
    sh.addEventListener("touchmove", (e) => {
      if (y0 === null) return;
      dy = e.touches[0].clientY - y0;
      if (dy <= 0) { sh.style.transform = ""; return; }
      if (e.cancelable) e.preventDefault();
      sh.style.transition = "none"; sh.style.transform = `translateY(${dy}px)`;
    }, { passive: false });
    const end = () => {
      if (y0 === null) return;
      y0 = null; sh.style.transition = ""; sh.style.transform = "";
      if (dy > 90) closeSheet();
      dy = 0;
    };
    sh.addEventListener("touchend", end); sh.addEventListener("touchcancel", end);
  })();
  // files dropped on the chat page attach as from the + button (desktop)
  (() => {
    const page = $("page-talk"), drop = el("div", "s-drop t-drop");
    drop.hidden = true; drop.appendChild(el("p", "", "Drop to attach"));
    page.appendChild(drop);
    let n = 0;
    const files = (e) => [...((e.dataTransfer && e.dataTransfer.types) || [])].includes("Files");
    page.addEventListener("dragenter", (e) => { if (files(e)) { n++; drop.hidden = false; } });
    page.addEventListener("dragleave", () => { n = Math.max(0, n - 1); if (!n) drop.hidden = true; });
    page.addEventListener("dragover", (e) => { if (files(e)) e.preventDefault(); });
    page.addEventListener("drop", (e) => { if (!files(e)) return; e.preventDefault(); n = 0; drop.hidden = true; addAny([...e.dataTransfer.files]); });
  })();
  $("t-model").addEventListener("click", () => { if ($("t-sheet-wrap").hidden) openSheet(); else closeSheet(); });
  // back to the sheet's first view, or closed (the context view opened from its chip closes at once)
  const sheetBack = () => {
    if (C.view === "unlock" || C.view === "team" || C.view === "speed") { closeSheet(); return; }
    if (C.view === "perms-full" || C.view === "perms-direct") { C.view = "perms"; renderSheet(); return; }
    if (C.view !== "main" && !(C.view === "context" && C.fromCtx) && !(C.view === "perms" && C.fromPerm)) { C.view = "main"; renderSheet(); } else closeSheet();
  };
  $("t-perm").addEventListener("click", openPerms);
  $("t-team").addEventListener("click", openTeam);
  wireAgents();
  $("t-offline-retry").addEventListener("click", () => { const o = C.offline; if (o) { hideOffline(); stream(o.id, o.body); } });
  $("t-offline-plain").addEventListener("click", () => { const o = C.offline; if (o) { hideOffline(); stream(o.id, Object.assign({}, o.body, { no_tools: true })); } });
  $("t-sheet-x").addEventListener("click", sheetBack);
  $("t-scrim").addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !$("t-sheet-wrap").hidden) {  // Tab stays inside the open sheet (it reached the page behind)
      const s = $("t-sheet"), f = [...s.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), select, textarea, [tabindex="0"]')].filter((n) => n.offsetParent !== null);
      if (!f.length) return;
      const i = f.indexOf(document.activeElement);
      if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && (i === -1 || i === f.length - 1)) { e.preventDefault(); f[0].focus(); }
      return;
    }
    if (e.key !== "Escape") return;
    if (!$("t-sheet-wrap").hidden) { sheetBack(); e.preventDefault(); }
    else if (C.menu) { closeMenu(); $("t-more").focus(); }
  });

  // ---------------------------------------------------------------- the chat's menu: rename, project, delete
  function closeMenu() { C.menu = false; $("t-menu").hidden = true; $("t-more").setAttribute("aria-expanded", "false"); }
  async function openMenu(view) {
    if (!C.chat) { $("t-more").hidden = true; return; }  // no chat (locked, before setup, an empty new one): nothing to act on
    C.menu = true; $("t-menu").hidden = false; $("t-more").setAttribute("aria-expanded", "true");
    const m = $("t-menu");
    m.replaceChildren();
    const item = (label, fn, cls) => { const b = el("button", "t-mi" + (cls ? " " + cls : ""), label); b.type = "button"; b.setAttribute("role", "menuitem"); b.addEventListener("click", fn); m.appendChild(b); return b; };
    if (view === "project") {
      if (!C.projects) { try { C.projects = (await call("GET", "/studio/api/projects")).projects; } catch (e) { C.projects = []; } }
      item("‹ Back", () => openMenu());
      item("No project", () => moveTo(null), !C.chat.project_id ? "on" : "");
      for (const p of C.projects) item(p.name, () => moveTo(p.id), C.chat.project_id === p.id ? "on" : "");
      if (!C.projects.length) m.appendChild(el("p", "t-mi-note", "No projects yet: make one under Projects."));
    } else if (view === "delete") {
      m.appendChild(el("p", "t-mi-note", `Delete "${C.chat.title}"? This cannot be undone.${K.keepsFolder(C.chat)}`));
      item("Delete", async () => { closeMenu(); try { await call("DELETE", `/studio/api/chats/${C.id}`); } catch (e) { K.toast(e.message, true); return; } location.hash = "#/talk"; K.loadList(); }, "danger");
      item("Cancel", () => openMenu());
    } else if (view === "rename") {  // the phone's page has no title to edit in place: the menu takes the new name
      const f = el("form", "t-mi-form"); f.noValidate = true;
      const input = el("input", "t-title-in"); input.value = C.chat.title; input.maxLength = 200; input.setAttribute("aria-label", "Title");
      const save = el("button", "t-mi", "Save"); save.type = "submit";
      f.append(input, save); m.appendChild(f);
      f.addEventListener("submit", async (e) => {
        e.preventDefault(); const t = input.value.trim(); closeMenu();
        if (t && t !== C.chat.title) { try { await call("PUT", `/studio/api/chats/${C.id}`, { title: t }); C.chat.title = t; } catch (x) { K.toast(x.message, true); } }
        setTitle(); K.loadList();
      });
      item("Cancel", () => openMenu());
      setTimeout(() => { input.focus(); input.select(); }, 0);
      return;
    } else {
      item("Rename", () => { if ($("t-title").offsetParent) { closeMenu(); renameTitle(); } else openMenu("rename"); });
      item("Move to project", () => openMenu("project"));
      if ((C.chat.messages || []).some((x) => x.role === "assistant")) item("Compact now", () => { closeMenu(); compactNow(); });
      item("Speed", () => { closeMenu(); openSpeed(null); });
      if ("Notification" in window && Notification.permission === "default") {
        item("Notify me when an answer is ready", async () => {
          closeMenu();
          try { await Notification.requestPermission(); } catch (e) { /* the browser said no */ }
          K.toast(Notification.permission === "granted" ? "StrixFold UI tells you when an answer is ready or a tap waits, while this page is open"
            : "Notifications are off for this page in the browser's settings", Notification.permission !== "granted");
        });
      }
      item("Delete", () => openMenu("delete"), "danger");
    }
    const f = view === "delete" ? [...m.querySelectorAll("button")].pop() : m.querySelector("button"); if (f) f.focus();  // Delete's view: Cancel
  }
  $("t-ctxwarn-go").addEventListener("click", () => compactNow());
  async function compactNow() {
    const id = C.id, before = ((C.chat && C.chat.window) || {}).summaries || 0;
    try { await call("POST", `/studio/api/chats/${id}/compact`); } catch (e) { K.toast(e.message, true); return; }
    K.toast("Compacting the conversation…");
    for (let i = 0; i < 80 && C.id === id; i++) {  // the summary takes about as long as an answer
      await new Promise((r) => setTimeout(r, 1500));
      await reload();
      const w = (C.chat && C.chat.window) || {};
      if ((w.summaries || 0) > before) { K.toast("Conversation compacted"); return; }
      if (!w.compacting && i > 2) break;
    }
  }
  async function moveTo(pid) {
    closeMenu();
    try { const c = await call("PUT", `/studio/api/chats/${C.id}`, { project_id: pid }); C.chat.project_id = c.project_id; K.toast(pid ? "Moved to the project" : "Taken out of the project"); }
    catch (e) { K.toast(e.message, true); }
    C.projects = null;
  }
  function renameTitle() {
    const h = $("t-title"), input = el("input", "t-title-in"); input.value = C.chat.title; input.maxLength = 200; input.setAttribute("aria-label", "Title");
    h.replaceChildren(input); input.focus(); input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const t = input.value.trim();
      if (save && t && t !== C.chat.title) { try { await call("PUT", `/studio/api/chats/${C.id}`, { title: t }); C.chat.title = t; } catch (e) { K.toast(e.message, true); } }
      setTitle(); K.loadList();
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
    input.addEventListener("blur", () => finish(true));
  }
  $("t-more").addEventListener("click", () => { if (C.menu) closeMenu(); else openMenu(); });
  // a tap anywhere else closes it: pointerdown, as an iPhone sends no click for a tap on plain text
  document.addEventListener("pointerdown", (e) => { if (C.menu && !e.composedPath().some((n) => n.id === "t-menu" || n.id === "t-more")) closeMenu(); });

  // ---------------------------------------------------------------- Projects: folders of chats with shared instructions
  async function renderProjects(id) {
    C.projectId = id;
    const box = $("p-body");
    let d;
    try { d = id ? await call("GET", `/studio/api/projects/${id}`) : await call("GET", "/studio/api/projects"); }
    catch (e) {
      if (e.status === 404) { location.hash = "#/projects"; return; }
      if (e.status === 401 || C.page !== "projects" || C.projectId !== id) return;
      // it couldn't be read (the Mac away, F3: the page stayed blank): say so, and look again by itself
      const w = el("div", "p-err"); w.append(el("p", "", "Couldn't reach StrixFold UI on the Mac."));
      const again = el("button", "go", "Try again"); again.type = "button"; again.addEventListener("click", () => renderProjects(id));
      w.appendChild(again); box.replaceChildren(w);
      clearTimeout(renderProjects.t); renderProjects.t = setTimeout(() => { if (C.page === "projects" && C.projectId === id && box.contains(w)) renderProjects(id); }, 5000);
      return;
    }
    if (C.page !== "projects" || C.projectId !== id) return;
    box.replaceChildren();
    if (!id) {
      C.projects = d.projects;
      $("p-h").textContent = "Projects"; $("p-note").textContent = "Folders of chats that share instructions.";
      const make = el("button", "go solid p-new", "New project"); make.type = "button";
      make.addEventListener("click", () => newProjectForm(box, make));
      box.appendChild(make);
      if (!d.projects.length) box.appendChild(el("p", "q-empty", "No projects yet. A project keeps related chats together and gives them the same instructions."));
      const ul = el("ul", "p-list");
      for (const p of d.projects) {
        const li = el("li"); const a = el("a", "p-card"); a.href = `#/projects/${p.id}`;
        a.appendChild(svg(IC.folder, "p-ic"));
        const tx = el("span", "p-tx");
        tx.append(el("span", "p-name", p.name), el("span", "p-sub", `${plural(p.chats, "chat")}${p.instructions ? " · " + p.instructions.split("\n")[0].slice(0, 80) : ""}`));
        a.appendChild(tx); li.appendChild(a); ul.appendChild(li);
      }
      box.appendChild(ul);
      return;
    }
    $("p-h").textContent = d.name; $("p-note").textContent = plural(d.chats.length, "chat");
    const back = el("a", "textbtn p-back", "‹ All projects"); back.href = "#/projects";
    const lab = el("label", "s-field p-instr"); lab.appendChild(el("span", "", "Instructions for every chat in this project"));
    const ta = el("textarea"); ta.value = d.instructions || ""; ta.rows = 6; ta.placeholder = "For example: answer in British English; I work on my clients' sites.";
    lab.appendChild(ta);
    const save = el("button", "go", "Save instructions"); save.type = "button";
    save.addEventListener("click", async () => { try { await call("PUT", `/studio/api/projects/${id}`, { instructions: ta.value }); K.toast("Instructions saved"); } catch (e) { K.toast(e.message, true); } });
    const go = el("button", "go solid", "New chat in this project"); go.type = "button";
    go.addEventListener("click", () => { C.newProject = id; C.projects = C.projects || [{ id, name: d.name }]; location.hash = "#/talk"; if (C.page === "talk") open(null); });
    const ren = el("button", "textbtn", "Rename"); ren.type = "button";
    ren.addEventListener("click", () => {
      const input = el("input", "t-title-in"); input.value = d.name; input.maxLength = 120; input.setAttribute("aria-label", "Project name");
      $("p-h").replaceChildren(input); input.focus(); input.select();
      let done = false;
      const fin = async (ok) => { if (done) return; done = true; const v = input.value.trim(); if (ok && v && v !== d.name) { try { await call("PUT", `/studio/api/projects/${id}`, { name: v }); } catch (e) { K.toast(e.message, true); } } renderProjects(id); };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") fin(true); if (e.key === "Escape") fin(false); });
      input.addEventListener("blur", () => fin(true));
    });
    const del = el("button", "textbtn danger", "Delete project"); del.type = "button";
    const acts = el("div", "p-acts"); acts.append(go, ren, del);
    del.addEventListener("click", () => K.confirmInline(acts, "Delete the project? Its chats stay, outside any project.", "Delete", async () => {
      try { await call("DELETE", `/studio/api/projects/${id}`); } catch (e) { K.toast(e.message, true); return; }
      C.projects = null; location.hash = "#/projects";
    }));
    box.append(back, acts, lab, save);
    const ul = el("ul", "p-chats");
    for (const c of d.chats) {
      const li = el("li"); const a = el("a", "p-chat"); a.href = `#/talk/${c.id}`;
      let icon = K.ICON.chat;
      a.append(svg(icon, "h-ico"), el("span", "", c.title));
      li.appendChild(a); ul.appendChild(li);
    }
    if (d.chats.length) { box.appendChild(el("h2", "p-sub-h", "Chats")); box.appendChild(ul); }
  }
  function newProjectForm(box, make) {
    const f = el("form", "p-form"); f.noValidate = true;
    const n = el("label", "s-field"); n.appendChild(el("span", "", "Name")); const ni = el("input"); ni.maxLength = 120; ni.required = true; n.appendChild(ni);
    const i = el("label", "s-field"); i.appendChild(el("span", "", "Instructions (optional)")); const ii = el("textarea"); ii.rows = 4; i.appendChild(ii);
    const err = el("p", "err");
    const row = el("div", "row"); const ok = el("button", "go solid", "Create project"); ok.type = "submit"; const no = el("button", "textbtn", "Cancel"); no.type = "button";
    row.append(ok, no); f.append(n, i, err, row);
    no.addEventListener("click", () => { f.remove(); make.hidden = false; make.focus(); });
    f.addEventListener("submit", async (e) => {
      e.preventDefault(); err.textContent = "";
      try { const p = await call("POST", "/studio/api/projects", { name: ni.value, instructions: ii.value }); C.projects = null; location.hash = `#/projects/${p.id}`; }
      catch (x) { err.textContent = x.message; }
    });
    make.hidden = true; make.after(f); ni.focus();
  }

  window.HaloChat = { route, openId: () => (C.page === "talk" ? C.id : null), onState, refresh,
    header: () => (C.page === "talk" ? { title: chatTitle(), sub:
      (modelOf(curModel()) || {}).name || "" } : null),
    chatModel: () =>  // a plain chat on a chat model (the Mac chip names it)
      true,
    status: () => {  // the chat's model, for the header: loaded, loading, or not loaded
      if (C.page !== "talk") return null;
      const m = modelOf(curModel()), l = (K.state() || {}).llm || {};
      if (!m) return null;
      const b = (l.pairs || []).find((x) => x.model_id === m.model_id && x.state === "ready");
      if (b && !(C.chat && C.chat.private)) return { text: `${m.name} · extra pairs`, on: true, busy: false };  // App.chat_state sends it there
      const mine = l.model_id === m.model_id;
      return { text: mine && l.state === "ready" ? m.name : mine && ["waiting", "loading", "linking"].includes(l.state) ? `${m.name} · loading` : mine && l.state === "failed" ? `${m.name} · didn't load` : `${m.name} · not loaded`,
        on: mine && l.state === "ready", busy: mine && l.state !== "ready" };
    } };
  // studio.js may have routed before this script ran: catch up with it
  const m = (location.hash || "").match(/^#\/(talk|projects)(?:\/([0-9a-f]{6,40}))?$/);
  if (m && K.page() === m[1] && K.unlocked() && C.page !== m[1]) route(m[1], m[2] || null, true);
})();
