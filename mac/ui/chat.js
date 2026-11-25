// StrixFold UI's chat: the sidebar (chats, pages), the thread, the composer and the model status chip. The state of
// the boxes comes from app.js (window.Halo, refreshed every second); chats live on this Mac (/local/chats) and answers
// stream from the serving model through /local/chats/<id>/send. Model output is rendered by md.js, never as HTML.
(() => {
  "use strict";
  const H = window.Halo, M = window.HaloMotion, MD = window.HaloMD;
  const $ = (id) => document.getElementById(id);
  const TOKEN = document.querySelector('meta[name="halo-ui-token"]').content;
  const ACTIVE = ["waiting", "loading", "linking", "ready"];
  const STEP = { waiting: [1, "Waiting for GPUs"], loading: [2, "Loading weights"], linking: [3, "Linking ranks"], ready: [4, "Ready"], stopping: [0, "Stopping"] };
  const C = { id: null, chat: null, list: [], pending: {}, streaming: null, attach: [], queued: null, menuOpen: false, q: "" };

  // ---------------------------------------------------------------- helpers
  async function call(method, path, body) {
    const opt = { method, headers: {}, cache: "no-store" };
    if (method !== "GET") { opt.headers["Content-Type"] = "application/json"; opt.headers["X-Halo-UI-Token"] = TOKEN; opt.body = JSON.stringify(body || {}); }
    const r = await fetch(path, opt);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) { const e = new Error((data && data.error) || `HTTP ${r.status}`); e.status = r.status; e.data = data; throw e; }
    return data;
  }
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const st = () => H.S.st;
  const profileOf = (id) => H.S.profiles.find((p) => p.id === id);
  const modelOf = (id) => H.S.models.find((m) => m.id === id);
  function wantedProfile() {
    const s = (C.chat && C.chat.settings) || C.pending;
    const cur = st();
    const pick = s.profile || H.store.get("chatProfile");
    if (pick && profileOf(pick) && profileOf(pick).installed) return profileOf(pick);
    if (cur && ACTIVE.includes(cur.state) && cur.profile && profileOf(cur.profile.id)) return profileOf(cur.profile.id);
    return profileOf("agent-coding") || H.S.profiles[0];
  }
  // serving: the model the chat wants is the one ready (any of its profiles)
  function servingFor(p) {
    const cur = st();
    return !!(p && cur && cur.state === "ready" && cur.profile && cur.profile.model_id === p.model_id);
  }
  const nearBottom = () => { const m = $("main"); return m.scrollHeight - m.scrollTop - m.clientHeight < 140; };
  const toBottom = () => { const m = $("main"); m.scrollTop = m.scrollHeight; };

  // ---------------------------------------------------------------- routing and the drawer
  // The Lab chat and the operations pages are retired from the app; their old addresses open Chat. The code stays
  // (hidden), so they can come back.
  const RETIRED = ["chat", "models", "agents", "requests", "connect", "settings"];
  // the skip link moves focus to the content and leaves the address alone ("#main" stayed in it, and a reload landed
  // there)
  document.querySelector("a.skip") && document.querySelector("a.skip").addEventListener("click", (e) => {
    e.preventDefault(); const m = $("main"); m.setAttribute("tabindex", "-1"); m.focus();
  });
  function route() {
    const h = location.hash || "#/talk";
    const r = h.match(/^#\/([a-z-]+)/);
    // (the bare address too: HaloStudio, loaded after this, routes by the address it finds)
    if (!location.hash || h === "#" || (r && RETIRED.includes(r[1]))) { location.replace("#/talk"); return; }
    let m = h.match(/^#\/(chat|models|agents|boxes|requests|connect|settings|studio-settings|talk|projects)(?:\/([0-9a-f]{6,40}))?$/);  // an old #/runs link: the start page
    const cz = h.match(/^#\/cousteau((?:\/[a-z0-9-]{1,40}){0,2})$/);  // the Command Centre, in StrixFold UI: #/cousteau[/<page or project>[/<tab>]]
    if (cz) m = [h, "cousteau", cz[1].slice(1) || null];
    if (!m && (h.startsWith("#/") || !route.seen)) { location.replace("#/talk"); return; }  // no such page, or a first load on #main
    if (!m) return;  // an in-page anchor: leave the page as it is
    route.seen = true;
    const page = m[1];
    const studio =
      ["studio-settings", "talk", "projects", "boxes", "cousteau"].includes(page);  // all StrixFold UI now
    const TITLES = { boxes: "Boxes", "studio-settings": "Settings", projects: "Projects" };  // (it stayed "New chat · StrixFold UI")
    if (TITLES[page]) document.title = `${TITLES[page]} · StrixFold UI`;
    else if (page === "talk") { const hd = window.HaloChat && window.HaloChat.header && window.HaloChat.header(); document.title = hd && hd.title ? `${hd.title} · StrixFold UI` : "StrixFold UI"; }
    document.querySelectorAll("#main > .page").forEach((p) => { p.hidden = p.id !== `page-${page}`; });
    document.querySelectorAll(".side-pages a").forEach((a) => a.classList.toggle("on", a.dataset.page === page));
    // the sidebar shows the section the page belongs to: the Lab chat and StrixFold UI's pages, or StrixFold UI's own
    $("side-chat").hidden = studio; $("side-studio").hidden = !studio;
    $("mode-chat").classList.toggle("on", !studio); $("mode-studio").classList.toggle("on", studio);
    $(studio ? "mode-chat" : "mode-studio").removeAttribute("aria-current"); $(studio ? "mode-studio" : "mode-chat").setAttribute("aria-current", "page");
    $("chat-settings-btn").hidden = page !== "chat" || !m[2];
    setDrawer(false);
    if (page === "chat") {
      const id = m[2] || null;
      if (id !== C.id || !C.chat) openChat(id);
    } else { $("main").scrollTop = 0; }
    if (window.HaloStudio) window.HaloStudio.route(studio ? page : null, m[2] || null);
    if (C.lock && !studio) loadList(); else renderList();  // back from unlocking: the private list now opens
  }
  window.addEventListener("hashchange", route);
  function setDrawer(open) {
    $("app").classList.toggle("side-open", open);
    $("scrim").hidden = !open;
    $("open-side").setAttribute("aria-expanded", String(open));
    if (open) ($("side-studio").hidden ? $("new-chat") : $("s-new")).focus();
  }
  window.HaloDrawer = setDrawer;
  window.HaloRoute = route;
  $("open-side").addEventListener("click", () => setDrawer(true));
  $("side-close").addEventListener("click", () => setDrawer(false));
  $("scrim").addEventListener("click", () => setDrawer(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { if ($("app").classList.contains("side-open")) setDrawer(false); if (C.menuOpen) setMenu(false); }
  });

  // ---------------------------------------------------------------- the chat list
  // locked (the passcode, or Mac only from the LAN): known from app.js's /local/private first, never from a 401
  async function locked() {
    const p = window.HaloPrivate ? await window.HaloPrivate() : { open: true };
    if (p.open) return false;
    C.lock = p;
    return true;
  }
  async function loadList() {
    if (await locked()) { C.list = C.list || []; renderList(); return; }
    try { C.list = (await call("GET", `/local/chats${C.q ? "?q=" + encodeURIComponent(C.q) : ""}`)).chats; C.lock = null; }
    catch (e) { C.list = C.list || []; if (e.status === 401 || e.status === 403) C.lock = e.data || {}; }
    renderList();
  }
  // Lab chat lives in the private store: behind the passcode, and not over plain HTTP from the LAN
  window.HaloPrivateLock = function (what, data) {
    const box = el("div", "chat-empty priv-lock");
    if (data && data.error === "https_required") {  // Mac only, by design: agents never leave this Mac
      box.appendChild(el("p", "", "Lab chat and agents open on this Mac only. On your phone, use the chat."));
      return box;
    }
    box.appendChild(el("p", "", `${what} ${what.endsWith("s") ? "are" : "is"} private. Unlock ${what.endsWith("s") ? "them" : "it"} with your passcode.`));
    const b = el("button", "go", "Unlock"); b.type = "button";
    let lockPage = "#/talk";
    b.addEventListener("click", () => { try { sessionStorage.setItem("halo.after-unlock", location.hash || "#/chat"); } catch (e) { /* private window */ } location.hash = lockPage; });
    box.appendChild(b);
    return box;
  };
  function day(t) {
    const d = new Date(t * 1000), now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
    if (t >= start) return "Today";
    if (t >= start - 86400) return "Yesterday";
    if (t >= start - 6 * 86400) return "This week";
    return d.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  }
  function renderList() {
    const box = $("chat-list");
    if (box.querySelector("input")) return;  // a rename is being typed
    box.replaceChildren();
    if (C.lock) { box.appendChild(window.HaloPrivateLock("Lab chat", C.lock)); return; }
    if (!C.list.length) { box.appendChild(el("p", "chat-empty", C.q ? "No chat matches." : "No chats yet.")); return; }
    let last = null;
    for (const c of C.list) {
      const d = day(c.updated);
      if (d !== last) { box.appendChild(el("p", "chat-day", d)); last = d; }
      const item = el("div", "chat-item" + (c.id === C.id && !$("page-chat").hidden ? " on" : ""));
      const a = el("a", "", c.title || "New chat"); a.href = `#/chat/${c.id}`; a.title = c.title;
      const ren = el("button", "ci-act", "Rename"); ren.type = "button";
      const del = el("button", "ci-act danger", "Delete"); del.type = "button";
      ren.addEventListener("click", () => renameInline(item, c));
      del.addEventListener("click", () => deleteChat(c.id, c.title));
      item.append(a, ren, del);
      box.appendChild(item);
    }
  }
  function renameInline(item, c) {
    const input = el("input"); input.value = c.title; input.maxLength = 200; input.setAttribute("aria-label", "Chat title");
    item.replaceChildren(input); input.focus(); input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const t = input.value.trim();
      input.remove();
      if (save && t && t !== c.title) {
        try { await call("PUT", `/local/chats/${c.id}`, { title: t }); if (C.chat && C.chat.id === c.id) C.chat.title = t; } catch (e) { H.toast(e.message, true); }
      }
      loadList();
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
    input.addEventListener("blur", () => finish(true));
  }
  async function deleteChat(id, title) {
    if (!confirm(`Delete "${title || "this chat"}"? It cannot be undone.`)) return;
    try { await call("DELETE", `/local/chats/${id}`); } catch (e) { H.toast(e.message, true); return; }
    if (C.id === id) location.hash = "#/chat";
    loadList();
  }
  let searchT = 0;
  $("chat-search").addEventListener("input", (e) => { clearTimeout(searchT); searchT = setTimeout(() => { C.q = e.target.value.trim(); loadList(); }, 200); });
  $("new-chat").addEventListener("click", () => { if (location.hash === "#/chat") openChat(null); else location.hash = "#/chat"; setDrawer(false); $("prompt").focus(); });

  // ---------------------------------------------------------------- one chat
  async function openChat(id) {
    if (C.streaming && C.streaming.id !== id) { /* the stream keeps going on the Mac; its answer is stored */ C.streaming.detach = true; C.streaming = null; }
    C.id = id; C.chat = null; C.attach = []; renderAttach();
    if (!id) { C.pending = {}; renderThread(); syncComposer(); return; }
    if (await locked()) { location.hash = "#/chat"; return; }  // the list shows the lock
    try { C.chat = await call("GET", `/local/chats/${id}`); }
    catch (e) { H.toast(e.status === 404 ? "That chat was deleted." : e.message, true); location.hash = "#/chat"; return; }
    renderThread();
    syncComposer();
    toBottom();
    if (C.chat.streaming) pollWhileAnswering(id);
  }
  async function pollWhileAnswering(id) {  // an answer still streaming from an earlier page: show it as it lands
    for (let i = 0; i < 600 && C.id === id && !C.streaming; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      try { C.chat = await call("GET", `/local/chats/${id}`); } catch (e) { return; }
      const keep = nearBottom(); renderThread(); if (keep) toBottom();
      if (!C.chat.streaming) return;
    }
  }

  function renderThread() {
    const t = $("thread");
    t.replaceChildren();
    const msgs = (C.chat && C.chat.messages) || [];
    if (!msgs.length) { t.appendChild(hello()); return; }
    const lastUser = [...msgs].reverse().find((m) => m.role === "user");
    const lastAsst = [...msgs].reverse().find((m) => m.role === "assistant");
    for (const m of msgs) t.appendChild(m.role === "user" ? userMsg(m, m === lastUser) : asstMsg(m, m === lastAsst));
  }
  function hello() {
    const box = el("div", "hello");
    const p = wantedProfile(), mm = p && modelOf(p.model_id);
    box.appendChild(el("h1", "", "Chat with the model on the boxes."));
    box.appendChild(el("p", "", `${mm ? mm.name : "DeepSeek V4.1 Flash"}${p ? ", " + p.name : ""}. Chats stay on this Mac. Attach text or code files with Attach; Shift+Enter starts a new line.`));
    const g = el("div", "suggest");
    const S = [["Explain an error", "Paste a stack trace or a log line.", "Explain this error and the most likely fix:\n\n"],
      ["Review code", "Attach a file; ask for bugs and risky spots.", "Review the attached code for bugs, edge cases and unclear parts. Be specific, with line references.\n\n"],
      ["Summarize a document", "Attach notes, a transcript or a report.", "Summarize the attached document in eight bullet points, then list open questions.\n\n"],
      ["Draft a message", "A commit message, an email, a short note.", "Write a short, plain commit message for this change:\n\n"]];
    for (const [title, sub, text] of S) {
      const b = el("button"); b.type = "button";
      b.appendChild(document.createTextNode(title)); b.appendChild(el("small", "", sub));
      b.addEventListener("click", () => { $("prompt").value = text; autosize(); $("prompt").focus(); });
      g.appendChild(b);
    }
    box.appendChild(g);
    return box;
  }
  function userMsg(m, last) {
    const w = el("div", "msg msg-user"); w.dataset.id = m.id;
    const atts = (m.meta && m.meta.attachments) || [];
    let text = m.content;
    if (atts.length) { const cut = text.indexOf("\n\n```"); if (cut >= 0) text = text.slice(0, cut) || "(attached files)"; }
    w.appendChild(el("div", "bubble", text));
    if (atts.length) w.appendChild(el("div", "att", "Attached: " + atts.join(", ")));
    if (last) {
      const meta = el("div", "msg-meta");
      const ed = el("button", "", "Edit and resend"); ed.type = "button";
      ed.addEventListener("click", () => editLast(w, m));
      meta.appendChild(ed); w.appendChild(meta);
    }
    return w;
  }
  function asstMsg(m, last) {
    const w = el("div", "msg msg-asst"); w.dataset.id = m.id;
    const meta = m.meta || {};
    const label = el("div", "msg-label"); label.appendChild(el("span", "", meta.model || "Model"));
    w.appendChild(label);
    if (m.reasoning) w.appendChild(thinking(m.reasoning, false));
    const body = el("div", "msg-body"); MD.render(body, m.content, { onCopy: copy });
    w.appendChild(body);
    if (meta.error) w.appendChild(el("p", "msg-err", meta.error));
    w.appendChild(metaRow(m, last));
    return w;
  }
  function thinking(text, open) {
    const d = el("details", "thinking"); d.open = !!open;
    d.appendChild(el("summary", "", "Thinking"));
    const b = el("div", "msg-body"); MD.render(b, text, { onCopy: copy }); d.appendChild(b);
    return d;
  }
  function metaRow(m, last) {
    const meta = m.meta || {}, row = el("div", "msg-meta");
    const u = meta.usage || {};
    const bits = [];
    if (meta.stopped) bits.push("stopped");
    else if (meta.finish === "length") bits.push("cut at the reply cap");
    if (u.completion_tokens) bits.push(`${H.fmtN(u.completion_tokens)} tokens`);
    if (meta.tok_s) bits.push(`${H.fmtN(meta.tok_s, 1)} tok/s`);
    if (meta.ttft_s) bits.push(`first token ${H.fmtN(meta.ttft_s, 2)} s`);
    if (u.prompt_tokens) bits.push(`prompt ${H.fmtN(u.prompt_tokens)}${u.prompt_tokens_details && u.prompt_tokens_details.cached_tokens ? `, ${H.fmtN(u.prompt_tokens_details.cached_tokens)} cached` : ""}`);
    if (bits.length) row.appendChild(el("span", "", bits.join(" · ")));
    const cp = el("button", "", "Copy"); cp.type = "button";
    cp.addEventListener("click", () => copy(m.content, cp));
    row.appendChild(cp);
    if (last && !meta.partial) {
      const rg = el("button", "", "Regenerate"); rg.type = "button";
      rg.addEventListener("click", () => send("regenerate"));
      row.appendChild(rg);
    }
    return row;
  }
  async function copy(text, b) {
    try { await navigator.clipboard.writeText(text); }
    catch (e) { const ta = el("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); }
    if (b) { const o = b.textContent; b.textContent = "Copied"; setTimeout(() => { b.textContent = o; }, 1200); }
  }
  function editLast(w, m) {
    const box = el("div", "edit-box");
    const ta = el("textarea"); ta.value = m.content; ta.setAttribute("aria-label", "Edit your message");
    const row = el("div", "row");
    const cancel = el("button", "textbtn", "Cancel"); cancel.type = "button";
    const go = el("button", "go solid", "Resend"); go.type = "button";
    cancel.addEventListener("click", renderThread);
    go.addEventListener("click", () => send("edit", ta.value));
    row.append(cancel, go); box.append(ta, row);
    w.replaceChildren(box); ta.focus();
  }

  // ---------------------------------------------------------------- sending and streaming
  async function send(mode, text) {
    mode = mode || "send";
    if (C.streaming) return;
    const content = mode === "send" ? $("prompt").value : text;
    if (mode !== "regenerate" && !String(content || "").trim() && !C.attach.length) return;
    const p = wantedProfile();
    if (!servingFor(p)) {  // start (or switch to) the chat's model first; the message goes once it is ready
      C.queued = { mode, content, attach: C.attach.slice() };
      const started = await ensureStarted(p);
      if (!started) { C.queued = null; return; }
      syncComposer();
      return;
    }
    if (!C.id) {
      try {
        const c = await call("POST", "/local/chats", { settings: Object.assign({ profile: p.id }, C.pending) });
        C.id = c.id; C.chat = c;
        history.replaceState(null, "", `#/chat/${c.id}`);
        $("chat-settings-btn").hidden = false;
      } catch (e) { H.toast(e.message, true); return; }
    }
    const attachments = mode === "regenerate" ? [] : C.attach.map((a) => ({ name: a.name, text: a.text }));
    if (mode === "send") { $("prompt").value = ""; autosize(); C.attach = []; renderAttach(); }
    await stream(C.id, { content, mode, attachments });
  }

  async function stream(id, body) {
    const ctrl = new AbortController();
    const S = { id, ctrl, text: "", reasoning: "", node: null, body: null, think: null, raf: 0 };
    C.streaming = S;
    syncComposer();
    let resp;
    try {
      resp = await fetch(`/local/chats/${id}/send`, { method: "POST", signal: ctrl.signal, cache: "no-store",
        headers: { "Content-Type": "application/json", "X-Halo-UI-Token": TOKEN }, body: JSON.stringify(body) });
    } catch (e) { C.streaming = null; syncComposer(); H.toast(`No answer: ${e.message}`, true); return; }
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try { msg = (await resp.json()).error || msg; } catch (e) { /* not JSON */ }
      C.streaming = null; syncComposer(); H.toast(msg, true);
      if (resp.status === 409) H.S.st = H.S.st; // the chip and startbar show why on the next poll
      return;
    }
    if (body.mode !== "send") {  // edit or regenerate dropped messages on the Mac: show the chat as it is now
      try { C.chat = await call("GET", `/local/chats/${id}`); } catch (e) { /* the events below still draw the answer */ }
      renderThread();
    }
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
          if (line) onEvent(S, JSON.parse(line.slice(6)));
        }
      }
    } catch (e) { if (e.name !== "AbortError") H.toast(`The stream broke: ${e.message}`, true); }
    finally {
      if (C.streaming === S) C.streaming = null;
      cancelAnimationFrame(S.raf);
      if (C.id === id) { try { C.chat = await call("GET", `/local/chats/${id}`); renderThread(); } catch (e) { /* keep what is shown */ } }
      syncComposer(); loadList();
    }
  }
  function onEvent(S, ev) {
    if (S.detach || C.id !== S.id) return;
    const keep = nearBottom();
    if (ev.type === "user") {
      if (C.chat.messages.some((m) => m.id === ev.id)) return;  // already drawn (edit: fetched with the chat)
      if (!C.chat.messages.length) $("thread").replaceChildren();
      const m = { id: ev.id, role: "user", content: ev.content, meta: {} };
      C.chat.messages.push(m);
      $("thread").querySelectorAll(".msg-user .msg-meta").forEach((x) => x.remove());
      $("thread").appendChild(userMsg(m, false));
    } else if (ev.type === "start") {
      C.chat.messages = C.chat.messages.filter((m) => m.id !== ev.id);
      const old = $("thread").querySelector(`.msg[data-id="${ev.id}"]`);
      if (old) old.remove();
      if ($("thread").querySelector(".hello")) $("thread").replaceChildren();
      $("thread").querySelectorAll(".msg-asst .msg-meta").forEach((x) => x.remove());
      const w = el("div", "msg msg-asst"); w.dataset.id = ev.id;
      const label = el("div", "msg-label"); label.appendChild(el("span", "", ev.model || "Model"));
      S.body = el("div", "msg-body"); S.body.appendChild(el("span", "caret"));
      w.append(label, S.body);
      $("thread").appendChild(w); S.node = w;
      $("chat-status").textContent = `${ev.model || "The model"} is answering.`;
    } else if (ev.type === "reasoning") {
      S.reasoning += ev.text;
      if (!S.think) { S.think = thinking("", true); S.node.insertBefore(S.think, S.body); }
      paint(S);
    } else if (ev.type === "delta") {
      S.text += ev.text;
      if (S.think && S.think.open && !S.closedThink) { S.think.open = false; S.closedThink = true; }
      paint(S);
    } else if (ev.type === "error") {
      if (S.node) S.node.appendChild(el("p", "msg-err", ev.error)); else H.toast(ev.error, true);
    } else if (ev.type === "done") {
      paint(S, true);
      $("chat-status").textContent = ev.stopped ? "Answer stopped." : "Answer complete.";
    }
    if (keep) toBottom();
  }
  function paint(S, final) {
    const draw = () => {
      S.raf = 0;
      if (S.think) MD.render(S.think.querySelector(".msg-body"), S.reasoning, { onCopy: copy });
      if (S.body) { MD.render(S.body, S.text, { onCopy: copy }); if (!final) S.body.appendChild(el("span", "caret")); }
    };
    if (final) { cancelAnimationFrame(S.raf); draw(); return; }
    if (!S.raf) S.raf = requestAnimationFrame(draw);
  }
  async function stopStream() {
    const S = C.streaming;
    if (!S) return;
    // the Mac stops the model and closes the stream after storing the answer as it stands; abort only if it does not
    try { await call("POST", `/local/chats/${S.id}/stop`); } catch (e) { S.ctrl.abort(); return; }
    setTimeout(() => { if (C.streaming === S) S.ctrl.abort(); }, 5000);
  }

  // ---------------------------------------------------------------- starting the model from the composer
  async function ensureStarted(p) {
    const cur = st();
    if (!p) { H.toast("No installed profile to start.", true); return false; }
    if (!p.installed) { H.toast(p.missing || `${p.name} is not startable.`, true); return false; }
    if (cur && ACTIVE.includes(cur.state) && cur.profile && cur.profile.model_id === p.model_id) return true;  // already on its way
    if (cur && ACTIVE.includes(cur.state) && cur.profile) {
      const mm = modelOf(p.model_id);
      if (!confirm(`${cur.profile.name} is ${cur.state}. Stop it and start ${mm ? mm.name : p.model}, ${p.name}?`)) return false;
      return await H.startProfile(p.id, { confirmed: true });
    }
    return await H.startProfile(p.id);
  }
  $("startbar-go").addEventListener("click", async () => {
    const p = wantedProfile(), cur = st();
    if (cur && ACTIVE.includes(cur.state) && cur.state !== "ready" && cur.profile && p && cur.profile.model_id === p.model_id) { H.stopRun(); C.queued = null; return; }
    await ensureStarted(p);
    syncComposer();
  });

  // ---------------------------------------------------------------- the composer
  function autosize() { const t = $("prompt"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, window.innerHeight * 0.4) + "px"; }
  $("prompt").addEventListener("input", () => { autosize(); syncSend(); });
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (C.streaming) return; send("send"); }
  });
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); if (C.streaming) stopStream(); else send("send"); });
  function syncSend() {
    const b = $("send");
    b.classList.toggle("stop", !!C.streaming);
    b.setAttribute("aria-label", C.streaming ? "Stop the answer" : "Send");
    b.disabled = !C.streaming && !$("prompt").value.trim() && !C.attach.length;
  }

  $("attach").addEventListener("change", async (e) => {
    for (const f of [...e.target.files]) {
      if (f.size > 400 * 1024) { H.toast(`${f.name}: larger than 400 KB; attach a smaller part.`, true); continue; }
      const text = await f.text();
      if (text.slice(0, 4096).includes("\u0000")) { H.toast(`${f.name}: not a text file.`, true); continue; }
      C.attach.push({ name: f.name, text });
    }
    e.target.value = "";
    renderAttach(); syncSend();
  });
  function renderAttach() {
    const box = $("attach-list");
    box.replaceChildren();
    C.attach.forEach((a, i) => {
      const chip = el("span", "att-chip", `${a.name} · ${H.fmtN(a.text.length)} chars`);
      const x = el("button", "", "×"); x.type = "button"; x.setAttribute("aria-label", `Remove ${a.name}`);
      x.addEventListener("click", () => { C.attach.splice(i, 1); renderAttach(); syncSend(); });
      chip.appendChild(x); box.appendChild(chip);
    });
  }

  // thinking on/off for this chat
  $("think").addEventListener("change", async (e) => {
    const on = e.target.checked;
    if (C.chat) {
      C.chat.settings = Object.assign(C.chat.settings || {}, { thinking: on });
      try { C.chat = Object.assign(C.chat, await call("PUT", `/local/chats/${C.chat.id}`, { settings: { thinking: on } })); }
      catch (err) { H.toast(err.message, true); e.target.checked = !on; }
    } else C.pending.thinking = on;
  });

  // the model picker: a model, then one of its profiles (the profile a start uses)
  function setMenu(open) {
    C.menuOpen = open;
    $("model-menu").hidden = !open;
    $("model-pick").setAttribute("aria-expanded", String(open));
    if (open) renderMenu();
  }
  $("model-pick").addEventListener("click", () => setMenu(!C.menuOpen));
  document.addEventListener("click", (e) => { if (C.menuOpen && !e.target.closest("#model-menu, #model-pick")) setMenu(false); });
  function renderMenu() {
    const box = $("model-menu"), cur = wantedProfile();
    box.replaceChildren();
    for (const m of H.S.models) {
      const profs = H.S.profiles.filter((p) => p.model_id === m.id);
      const g = el("div", "mm-model");
      const h = el("h4"); h.appendChild(document.createTextNode(m.name));
      h.appendChild(el("small", "", m.installed === false ? (m.missing ? "pending" : "not startable") : servingFor(profs[0]) ? "serving" : ""));
      g.appendChild(h);
      if (m.warning) g.appendChild(el("p", "mm-warn", m.warning));
      else if (m.installed === false) g.appendChild(el("p", "", m.missing || m.why || ""));
      box.appendChild(g);
      for (const p of profs) {
        const b = el("button", "mm-prof", p.name); b.type = "button"; b.setAttribute("role", "option");
        b.setAttribute("aria-selected", String(cur && cur.id === p.id));
        const s = p.settings || {};
        b.appendChild(el("small", "", p.fixed ? (p.chips || []).slice(0, 2).join(", ") : `${H.fmtN(s.context)} tokens${s.drafts ? ", drafts" : ""}${s.prompt_cache_gib ? ", cache" : ""}`));
        b.disabled = !p.installed;
        b.addEventListener("click", () => choose(p));
        box.appendChild(b);
      }
    }
  }
  async function choose(p) {
    setMenu(false);
    H.store.set("chatProfile", p.id);
    if (C.chat) {
      try { C.chat = Object.assign(C.chat, await call("PUT", `/local/chats/${C.chat.id}`, { settings: { profile: p.id } })); } catch (e) { H.toast(e.message, true); }
    } else C.pending.profile = p.id;
    syncComposer();
    if (!C.chat || !C.chat.messages.length) renderThread();
  }

  // what the composer shows: the picked model, and a bar to start it when it is not the one serving
  function syncComposer() {
    const p = wantedProfile(), cur = st(), mm = p && modelOf(p.model_id);
    $("model-pick-name").textContent = mm ? mm.name : (p ? p.model : "Model");
    $("model-pick-profile").textContent = p ? p.name : "";
    const s = (C.chat && C.chat.settings) || C.pending;
    $("think").checked = s.thinking !== undefined ? !!s.thinking : !!(p && p.settings && p.settings.thinking);
    const bar = $("startbar"), prog = $("startbar-progress"), go = $("startbar-go");
    let text = "", label = "", showGo = true, progress = null;
    const otherActive = cur && ACTIVE.includes(cur.state) && cur.profile && p && cur.profile.model_id !== p.model_id;
    const mine = cur && ACTIVE.includes(cur.state) && cur.profile && p && cur.profile.model_id === p.model_id;
    if (!p) { text = "No installed profile."; showGo = false; }
    else if (servingFor(p)) { bar.hidden = true; }
    else if (mine) {
      progress = cur.state;
      text = cur.state === "waiting" ? waitingText(cur) : `${mm ? mm.name : p.model}, ${cur.profile.name}: ${STEP[cur.state][1].toLowerCase()}.`;
      label = "Cancel"; showGo = true;
    } else if (otherActive) {
      text = `${cur.profile.name} is ${cur.state === "ready" ? "serving" : cur.state}. This chat uses ${mm ? mm.name : p.model}.`;
      label = `Switch to ${mm ? mm.name : p.model}`;
    } else if (cur && cur.state === "stopping") { text = "Stopping the last run…"; showGo = false; }
    else {
      const held = lockHolders(cur);
      text = `${mm ? mm.name : p.model} is not running.` + (held ? ` ${held}; a start waits in line.` : "");
      label = `Start ${mm ? mm.name : p.model} (${p.name})`;
    }
    if (!servingFor(p)) {
      bar.hidden = false;
      $("startbar-text").replaceChildren(document.createTextNode(text));
      if (C.queued) $("startbar-text").appendChild(el("strong", "", " Your message goes as soon as it is ready."));
      $("startbar-label").textContent = label;
      go.classList.toggle("stop", label === "Cancel");
      go.hidden = !showGo;
      prog.hidden = !progress;
      if (progress) {
        const [n, word] = STEP[progress] || [0, progress];
        if ($("sb-step").dataset.word !== word) { $("sb-step").dataset.word = word; if (M) M.decode($("sb-step"), word); else $("sb-step").textContent = word; }
        $("sb-fill").style.width = `${Math.max(6, n * 25)}%`;
        $("sb-detail").textContent = cur.started ? `${H.dur(cur.t - cur.started)} so far` : "";
      } else $("sb-step").dataset.word = "";
    }
    $("composer-hint").textContent = servingFor(p) && cur.profile.id !== p.id ? `serving with ${cur.profile.name}` : "";
    syncSend();
    // a message waiting for the model goes now
    if (C.queued && servingFor(p) && !C.streaming) {
      const q = C.queued; C.queued = null;
      if (q.mode === "send") { $("prompt").value = q.content || ""; C.attach = q.attach; renderAttach(); }
      send(q.mode, q.content);
    }
  }
  // the same source as the Models page: app.js's teamBusy (names stay on Boxes & GPUs and behind "Who")
  function lockHolders(cur) {
    return cur && window.HaloTeamBusy && window.HaloTeamBusy(cur).busy ? "The GPUs are busy with other work" : "";
  }
  const narrow = () => window.matchMedia("(max-width: 600px)").matches;
  function waitingText(cur) {
    const held = lockHolders(cur);
    if (narrow()) return held ? "GPUs busy with other work: waiting" : "Taking the GPU locks";
    return held ? `${held}; StrixFold UI waits its turn.` : "Taking the GPU locks (box1, then box2).";
  }

  // ---------------------------------------------------------------- the status chip
  let chipModel = "";
  window.addEventListener("halo-studio-state", () => syncChip());  // the private side's poll: the chip changes with it
  function syncChip() {
    const cur = st(), dot = $("chip-dot"), state = $("chip-state");
    const p = cur && cur.profile ? cur.profile : wantedProfile();
    const mm = p && modelOf(p.model_id);
    const halo = !$("side-studio").hidden && window.HaloModelName && p && window.HaloModelName(p.model_id);  // StrixFold UI's pages: StrixFold UI's names
    const name = !cur && H.S.fails > 2 ? "StrixFold UI" : halo || (mm ? mm.name : "DeepSeek V4.1 Flash");  // away: no stale model name
    // on a plain chat's page, the chat's own model and its state, as the phone shows it ("Ready" on a chat whose
    // model wasn't loaded)
    const hc = cur &&
      !$("page-talk").hidden && window.HaloChat && window.HaloChat.chatModel && window.HaloChat.chatModel() && window.HaloChat.status();
    const hcParts = hc && hc.text ? hc.text.split(" · ") : null;
    const shown = hcParts ? hcParts[0] : name;
    const stKey = `${shown}|${cur ? cur.state : "none"}`;
    if (stKey !== chipModel) { chipModel = stKey; if (M) M.decode($("hd-model"), shown); else $("hd-model").textContent = shown; }
    let text = H.S.fails > 2 ? "Not answering" : "No contact with the boxes", cls = "";  // (no "halod")
    if (cur) {
      const live = cur.live || {};
      if (cur.state === "ready") {
        cls = "on";
        text = live.connections ? (live.decode_tps ? `Generating · ${H.fmtN(live.decode_tps, 1)} tok/s` : "Reading a prompt") : `Ready · ${cur.profile ? cur.profile.name : ""}`;
        if (live.connections) cls = "busy";
      } else if (["waiting", "loading", "linking", "stopping"].includes(cur.state)) {
        cls = "busy";
        text = cur.state === "waiting" ? (lockHolders(cur) ? "Waiting · GPUs busy with other work" : "Waiting for GPUs") : STEP[cur.state][1];
      } else {
        const held = lockHolders(cur);
        text = cur.state === "failed" ? "Failed · see Boxes" : held ? "Stopped · GPUs busy with other work" : "Stopped";
      }
    }
    dot.className = "chip-dot" + (cls ? " " + cls : "");
    state.textContent = text;
    if (hcParts && !hc.on) {  // its model loaded: the live words above (Generating, Reading a prompt) stay
      const w = hcParts.slice(1).join(" · ");
      state.textContent = w ? w.charAt(0).toUpperCase() + w.slice(1) : "Ready";  // "Not loaded", beside "Ready" and "Stopped"
      dot.className = "chip-dot" + (hc.busy ? " busy" : hc.on ? " on" : "");
    }
    $("status-chip").setAttribute("aria-label", `${shown}: ${state.textContent}. Open Boxes and GPUs`);  // its state is read out too
    $("hd-conn").classList.toggle("fake", !!(H.S.local && H.S.local.fake));
  }

  // ---------------------------------------------------------------- chat settings
  $("chat-settings-btn").addEventListener("click", () => {
    if (!C.chat) return;
    const s = C.chat.settings || {};
    $("cs-title").value = C.chat.title || "";
    $("cs-system").value = C.chat.system || "";
    $("cs-max").value = s.max_tokens || "";
    $("cs-temp").value = s.temperature || "";
    $("cs-topk").value = s.top_k || "";
    $("cs-err").textContent = "";
    $("cs-export").href = `/local/chats/${C.chat.id}/export`;
    $("chat-settings").showModal();
  });
  $("cs-close").addEventListener("click", () => $("chat-settings").close());
  $("cs-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const settings = {};
    if ($("cs-max").value.trim()) settings.max_tokens = $("cs-max").value.trim();
    settings.temperature = $("cs-temp").value.trim() || 0;
    if ($("cs-topk").value.trim()) settings.top_k = $("cs-topk").value.trim();
    try {
      C.chat = Object.assign(C.chat, await call("PUT", `/local/chats/${C.chat.id}`, { title: $("cs-title").value, system: $("cs-system").value, settings }));
      $("chat-settings").close(); loadList(); H.toast("Saved");
    } catch (err) { $("cs-err").textContent = err.message; }
  });
  $("cs-delete").addEventListener("click", () => { $("chat-settings").close(); deleteChat(C.chat.id, C.chat.title); });

  // ---------------------------------------------------------------- wiring
  document.addEventListener("halo:state", () => { syncChip(); syncComposer(); });
  document.addEventListener("halo:profiles", () => { syncChip(); syncComposer(); if (!C.chat || !C.chat.messages.length) renderThread(); });
  setInterval(loadList, 15000);
  loadList();
  route();
  autosize();
})();
