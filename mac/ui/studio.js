// The private side of the pages: the lock (a passcode or a paired device), the list of chats, the settings, the viewer
// of a chat's documents and the poll behind them. Everything it shows comes from the private store on this Mac through
// /studio/ (random ids, no-store). Model output is rendered by md.js, never as HTML.
(() => {
  "use strict";
  const H = window.Halo;
  const $ = (id) => document.getElementById(id);
  let KEPT = "chats";  // what the lock and the search name
  const TOKEN = document.querySelector('meta[name="halo-ui-token"]').content;
  const S = { page: null, list: [], q: "", auth: null, st: null, v: null };

  // ---------------------------------------------------------------- helpers
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const svg = (paths, cls) => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); if (cls) s.setAttribute("class", cls); s.innerHTML = paths; return s; };
  const ICON = {
    image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.7"/><path d="M4 17l5-5 4 4 2.5-2.5L20 18"/>',
    video: '<rect x="3.5" y="5.5" width="13" height="13" rx="2"/><path d="M16.5 10l4-2.5v9l-4-2.5z"/>',
    audio: '<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
    chat: '<path d="M4.5 6.5A2 2 0 0 1 6.5 4.5h11a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H10l-4.5 3.5V16.5h0a1 1 0 0 1-1-1z"/>',
    text: '<path d="M7 3.5h7l4.5 4.5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-15.5a1 1 0 0 1 1-1z"/><path d="M14 3.5V8h4.5M9 12.5h6M9 15.5h6"/>',
  };
  // Private chats: the token from Unlock, kept in this closure only (never storage, a cookie or a URL);
  // a reload, Lock or the idle lock drops it
  let PRIV = null;
  const privHeaders = () => (PRIV ? { "X-Halo-Private": PRIV } : {});
  const SAVES = new Map();  // url -> a chat document's bytes on their way, for a Save tapped again (fileOf)
  async function call(method, path, body) {
    const opt = { method, headers: privHeaders(), cache: "no-store", credentials: "same-origin" };
    if (method !== "GET") { opt.headers["Content-Type"] = "application/json"; opt.headers["X-Halo-UI-Token"] = TOKEN; opt.body = JSON.stringify(body || {}); }
    let r;
    try { r = await fetch(path, opt); }
    catch (e) { const x = new Error("Can't reach StrixFold UI on the Mac. Try again in a moment."); x.down = true; throw x; }  // not "Failed to fetch"
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    // Tailscale's own 502 while the app is down carries no JSON (the app's own 502s say what failed)
    if ((r.status === 502 || r.status === 504) && !data) { const e = new Error("Can't reach StrixFold UI on the Mac. Try again in a moment."); e.status = r.status; e.down = true; throw e; }
    if (r.status === 401) { locked(); const e = new Error("StrixFold UI is locked"); e.status = 401; throw e; }
    if (!r.ok) { const e = new Error((data && data.error) || `HTTP ${r.status}`); e.status = r.status; e.data = data; throw e; }
    return data;
  }
  const onStudio = () => !!S.page;

  // ---------------------------------------------------------------- the lock
  // the Mac's idle lock (below): opening or reloading a page restarts this tab's clock, but only real input is shared
  const IDLE = { on: false, t: Date.now(), wrote: 0, remember: true, dlg: null, home: null, title: "", x: null, y: null };
  const IDLE_KEY = "halo.idle.active", IDLE_MIN = "halo.idle.min", IDLE_LOCK = "halo.idle.locked", IDLE_OPEN = "halo.idle.unlocked";
  const lsGet = (k) => { try { return Number(localStorage.getItem(k)) || 0; } catch (e) { return 0; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, String(v)); } catch (e) { /* private window: this tab alone */ } };
  async function checkAuth() {
    // the route always answers 200 with JSON: anything else (Tailscale's 502 while the app is down) is "can't reach", null
    try { const r = await fetch("/studio/api/auth", { cache: "no-store" }); S.auth = r.ok ? await r.json() : null; } catch (e) { S.auth = null; }
    // theme.js reads this before a page paints, so a tab opened after the limit blurs before it shows anything
    if (S.auth) lsSet(IDLE_MIN, S.auth.local && S.auth.set ? (S.auth.idle_lock_min || 0) : 0);
    return S.auth && S.auth.unlocked;
  }
  // Private chats: any lock (the Lock button, a 401, the idle lock) ends this page's private token and
  // takes every Private chat's words off the page, which the idle lock's blur would otherwise leave in place underneath
  function dropPrivate() {
    PRIV = null;
    forgetPrivateFiles();
    let masked = false;
    for (const c of S.list || []) if (c.private && c.title !== "Private chat") { c.title = "Private chat"; masked = true; }
    if (masked) renderList();
    window.dispatchEvent(new Event("halo:private-locked"));
  }
  function locked() {
    dropPrivate();
    if (S.auth) S.auth.unlocked = false;
    if (IDLE.on) return;  // the idle lock: the pages stay as they are under the blur (every module's 401 comes here)
    if (onStudio()) showLock();
    $("s-list").replaceChildren(el("p", "chat-empty", "StrixFold UI is locked."));
  }
  const LOCKED_PAGES = ["page-studio-settings", "page-cousteau", "page-talk", "page-projects"];
  function hidePages() {  // nothing behind the lock or the can't-reach message (Chat and projects too)
    if (!IDLE.on) document.querySelectorAll("#main > .page").forEach((p) => { if (LOCKED_PAGES.includes(p.id)) p.hidden = true; });
  }
  function showLock() {
    $("s-lock").classList.toggle("down", !S.auth);
    if (!S.auth) {  // the check itself failed (the app down or restarting): never "set a passcode"; look again shortly
      $("s-lock").hidden = false;
      hidePages();
      $("s-lock-err").textContent = "";
      $("s-lock-link").hidden = true;
      $("s-lock-h").textContent = "Can't reach StrixFold UI";
      $("s-lock-p").textContent = "The Mac's StrixFold UI isn't answering. This page tries again by itself every few seconds.";
      $("s-lock-form").classList.add("noform");
      clearTimeout(showLock.again);
      // under the idle lock a reload only locks again (theme.js), so the passcode form shows in place
      showLock.again = setTimeout(async () => { await checkAuth(); if (S.auth && !IDLE.on) location.reload(); else showLock(); }, 5000);
      return;
    }
    const a = S.auth;
    const setup = !a.set;
    $("s-lock").hidden = false;
    hidePages();
    $("s-lock-err").textContent = "";
    $("s-lock-again-w").hidden = !setup;
    $("s-lock-rem-w").hidden = setup;
    $("s-lock-pass").autocomplete = setup ? "new-password" : "current-password";
    $("s-lock-link").hidden = true;
    if (a.https_only) {  // plain HTTP from the home network: it opens only over HTTPS on the tailnet
      $("s-lock-h").textContent = "StrixFold UI opens over your tailnet";
      $("s-lock-p").textContent = a.url && a.tailnet_ready
        ? "On the home network StrixFold UI needs HTTPS, so your phone and other devices open it through Tailscale."
        : "On the home network StrixFold UI needs HTTPS through Tailscale, which is not switched on yet. Use StrixFold UI on the Mac for now.";
      if (a.url && a.tailnet_ready) {
        $("s-lock-link").href = a.url;
        $("s-lock-link").textContent = `Open ${a.url.replace("https://", "").replace(/\/#\/.*$/, "")}`; $("s-lock-link").hidden = false;
      }
      $("s-lock-form").classList.add("noform");
      return;
    }
    if (setup && !a.local) {
      $("s-lock-h").textContent = "Set the passcode on the Mac";
      $("s-lock-p").textContent = "StrixFold UI needs a passcode before it opens anywhere else. Open StrixFold UI on the Mac itself and set one there.";
      $("s-lock-form").classList.add("noform");
    } else {
      $("s-lock-form").classList.remove("noform");
      $("s-lock-h").textContent = setup ? "Choose a passcode for StrixFold UI" : "StrixFold UI is locked";
      $("s-lock-p").textContent =
        setup ? "Your chats stay private on this Mac. The passcode opens them here and on your phone; at least 6 characters."
        : IDLE.on ? `Locked after ${IDLE.min || "a few"} minute${IDLE.min === 1 ? "" : "s"} with no input on this Mac. Enter the passcode to carry on where you were.`
        : `Enter the passcode to see your ${KEPT}.`;
      if (IDLE.on) $("s-lock-rem").checked = IDLE.remember !== false;
      $("s-lock-l1").textContent = setup ? "New passcode" : "Passcode";
      $("s-lock-go").textContent = setup ? "Set passcode" : "Unlock";
      setTimeout(() => $("s-lock-pass").focus(), 50);
    }
  }
  $("s-lock-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const a = S.auth || {}, pass = $("s-lock-pass").value;
    $("s-lock-err").textContent = "";
    if (!a.set && pass !== $("s-lock-again").value) { $("s-lock-err").textContent = "The two passcodes differ."; return; }
    try {
      await call("POST", a.set ? "/studio/api/auth/unlock" : "/studio/api/auth/setup", { passcode: pass, remember: $("s-lock-rem").checked });
    } catch (err) {  // said again each time, so a screen reader hears a second wrong try too
      $("s-lock-err").textContent = ""; setTimeout(() => { $("s-lock-err").textContent = err.message; }, 60); $("s-lock-pass").select(); return;
    }
    $("s-lock-pass").value = ""; $("s-lock-again").value = "";
    await checkAuth();
    if (IDLE.on) { idleOff(true); return; }
    $("s-lock").hidden = true;
    let back = null;  // Lab chat or Agents sent the user here to unlock (they live in the private store too)
    try { back = sessionStorage.getItem("halo.after-unlock"); sessionStorage.removeItem("halo.after-unlock"); } catch (err2) { back = null; }
    if (back && /^#\/(chat|agents)/.test(back)) { location.hash = back; return; }
    route(S.page, S.want, true);  // the chat the link asked for before the lock
  });

  // ---------------------------------------------------------------- the Mac's idle lock
  // After S.auth.idle_lock_min minutes with no input in any StrixFold UI tab on this Mac (Settings; /studio/api/auth
  // sends 0 to the phone and the tailnet page), StrixFold UI locks: the server forgets this device's session, so
  // nothing opens again without the passcode, and the whole window blurs behind the passcode form. The pages stay as
  // they are underneath (a draft, a scroll position, an open chat), and what plays stops. Tabs share the last input
  // time and the lock through localStorage, so input in one tab keeps the others open and a lock in one locks them all.
  const LOCKED_TITLE = "StrixFold UI · locked";
  // also when it is locked already: the dashboard's own pages have no passcode on this Mac, and the window blurs all the
  // same
  const idleLimit = () => (S.auth && S.auth.local && S.auth.set && S.auth.idle_lock_min > 0) ? S.auth.idle_lock_min * 60000 : 0;
  // only a person counts: trusted events, and a pointer that really moved (content scrolling under a still cursor, or
  // a reply streaming in, is not input). The shared time is written at most every 2 s.
  function idleInput(e) {
    if (IDLE.on || !e.isTrusted) return;  // typing the passcode on the blurred window is not "using StrixFold UI"
    if (e.type === "pointermove") {
      if (e.screenX === IDLE.x && e.screenY === IDLE.y) return;
      IDLE.x = e.screenX; IDLE.y = e.screenY;
    }
    IDLE.t = Date.now();
    if (IDLE.t - IDLE.wrote > 2000) { IDLE.wrote = IDLE.t; lsSet(IDLE_KEY, IDLE.t); }
  }
  ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "touchmove"].forEach((ev) => window.addEventListener(ev, idleInput, { capture: true, passive: true }));
  function idleCheck() {  // synchronous up to the lock's first frame (visibilitychange: before the tab shows content)
    const lim = idleLimit();
    if (lim && !IDLE.on && Date.now() - Math.max(IDLE.t, lsGet(IDLE_KEY)) >= lim) idleOn(true);
  }
  setInterval(idleCheck, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) idleCheck(); });
  window.addEventListener("storage", (e) => {
    if (e.key === IDLE_LOCK && e.newValue && !IDLE.on && S.auth && S.auth.local) idleOn(false);
    if (e.key === IDLE_OPEN && e.newValue && IDLE.on) checkAuth().then((ok) => { if (ok) idleOff(false); });
  });
  // the tab strip and Mission Control show "StrixFold UI · locked", whatever a page sets while locked
  new MutationObserver(() => { if (IDLE.on && document.title !== LOCKED_TITLE) { IDLE.title = document.title; document.title = LOCKED_TITLE; } })
    .observe(document.head, { childList: true, characterData: true, subtree: true });
  async function idleOn(first) {
    IDLE.on = true;
    IDLE.min = (S.auth && S.auth.idle_lock_min) || lsGet(IDLE_MIN) || 0;
    IDLE.remember = !(S.auth && S.auth.remember === false);
    IDLE.title = document.title; document.title = LOCKED_TITLE;
    // a modal dialog sits above everything, other dialogs included; its backdrop blurs the window, and the rest of
    // the page is inert (no clicks, no focus) while it is open
    if (!IDLE.dlg) {
      IDLE.dlg = document.createElement("dialog");
      IDLE.dlg.className = "idle-dlg";
      IDLE.dlg.setAttribute("aria-label", "StrixFold UI is locked");
      IDLE.dlg.addEventListener("cancel", (e) => e.preventDefault());  // Esc does not close it
      IDLE.dlg.addEventListener("close", () => { if (IDLE.on) IDLE.dlg.showModal(); });  // nor does a forced close
      document.body.appendChild(IDLE.dlg);
    }
    const lock = $("s-lock");
    if (!IDLE.home) IDLE.home = [lock.parentNode, lock.nextSibling];
    IDLE.dlg.appendChild(lock);
    document.body.classList.add("idle-locked");
    dropPrivate();  // at once, before the server's lock answers
    if (!IDLE.dlg.open) IDLE.dlg.showModal();  // also hides open popovers; earlier modal dialogs sit under its blur
    document.documentElement.classList.remove("idle-boot");
    if (first) lsSet(IDLE_LOCK, Date.now());
    try { await call("POST", "/studio/api/auth/lock"); } catch (e) { /* locked either way */ }
    if (!S.auth) await checkAuth();  // a page opened locked asks before the start's check answers: the form, not "can't reach"
    locked();
    showLock();  // on any page: the dashboard's own pages blur too
  }
  function idleOff(first) {
    IDLE.t = Date.now(); IDLE.wrote = 0; lsSet(IDLE_KEY, IDLE.t);  // the clock restarts before anything shows
    IDLE.on = false;
    document.title = IDLE.title || document.title;
    const lock = $("s-lock");
    lock.hidden = true;
    if (IDLE.home) { IDLE.home[0].insertBefore(lock, IDLE.home[1]); IDLE.home = null; }
    if (IDLE.dlg && IDLE.dlg.open) IDLE.dlg.close();
    document.body.classList.remove("idle-locked");
    if (first) lsSet(IDLE_OPEN, Date.now());
    route(S.page, S.want, false);  // fresh data; the open chat and its draft stay as they were
    window.dispatchEvent(new Event("halo:unlocked"));  // the Command Centre follows a reply again that the lock cut off
  }
  // a page opened after the limit: theme.js has hidden it before it painted; lock it now, before anything renders
  if (document.documentElement.classList.contains("idle-boot")) idleOn(true);

  // ---------------------------------------------------------------- routing (chat.js calls this on every hash change)
  async function route(page, id, force) {
    const was = S.page;
    S.page = page; S.want = id;
    if (IDLE.on) { showLock(); return; }  // the idle lock holds the form; idleOff routes again after the passcode
    if (!page) { $("s-lock").hidden = true; closeViewer(); return; }
    if (!(S.auth && S.auth.unlocked) && !(await checkAuth())) { showLock(); return; }
    $("s-lock").hidden = true;
    document.querySelectorAll("#main > .page").forEach((p) => { p.hidden = p.id !== `page-${page}`; });
    // each page's tab title, on the Mac and the phone alike; Chat and the Command Centre set their own
    const TITLE = { "studio-settings": "Settings", projects: "Projects", boxes: "Boxes",
    }[page];
    if (TITLE && !IDLE.on) document.title = `${TITLE} · StrixFold UI`;
    else if (page === "talk" && !id && !IDLE.on) document.title = "New chat · StrixFold UI";  // a chat sets its own
    if (was !== page) closeViewer();
    loadList();
    pollState();
    if (page === "studio-settings") loadSettings();
    if (page !== "talk" && $("t-ctx-top")) $("t-ctx-top").hidden = true;  // a chat's context ring: on its own page only
    if ((page === "talk" || page === "projects") && window.HaloChat) window.HaloChat.route(page, id, force);
    if (page === "cousteau" && window.HaloCousteau) window.HaloCousteau.route(id, force);
  }
  window.HaloStudio = { route };
  window.HaloStudioKit = { call, el, svg, ICON, copy, confirmInline, attThumb, refOf, keepsFolder, toast: (m, bad) => H.toast(m, bad),
    state: () => S.st, unlocked: () => !!(S.auth && S.auth.unlocked), page: () => S.page, poll: () => pollState(), loadList: () => loadList(),
    renderList: () => renderList(), locked: () => locked(), TOKEN, attachRowMenu: (item, more, items) => attachRowMenu(item, more, items),
    setPrivate: (t) => { PRIV = t || null; }, privHeaders: () => privHeaders(), hasPrivate: () => !!PRIV,
    openDoc: (f, priv) => openViewer({ kind: "doc", id: f.id, name: f.name, priv: !!priv }), saveFile: (url, name) => saveFile(url, name),
    forgetPrivate: () => forgetPrivateFiles(),
  };

  // ---------------------------------------------------------------- the list of chats
  async function loadList() {
    if (!(S.auth && S.auth.unlocked)) return;
    try { S.list = (await call("GET", `/studio/api/chats?kind=all${S.q ? "&q=" + encodeURIComponent(S.q) : ""}`)).chats; } catch (e) { return; }
    renderList();
  }
  // The list has two modes: inside the Command Centre it is "Projects", every open project (pinned first, then by last activity);
  // everywhere else it is "Recents", the chats only, by time. Each mode keeps its own scroll.
  const listScroller = (box) => { for (let n = box; n && n !== document.body; n = n.parentElement) { const o = getComputedStyle(n).overflowY; if (o === "auto" || o === "scroll") return n; } return null; };
  function renderList() {
    const box = $("s-list");
    if (box.querySelector("input, .inline-confirm")) return;  // a rename or a delete confirm open: it stays
    const CZ = window.HaloCousteau, mode = S.page === "cousteau" && CZ ? "projects" : "recents";
    const sc = listScroller(box);
    S.listTop = S.listTop || {};
    if (sc && S.listMode && S.listMode !== mode) S.listTop[S.listMode] = sc.scrollTop;
    const keep = sc && S.listMode === mode ? sc.scrollTop : (S.listTop[mode] || 0);
    const changed = S.listMode !== mode;
    S.listMode = mode;
    $("h-recents-h").textContent = mode === "projects" ? "Projects" : "Recents";
    $("s-search").placeholder = mode === "projects" ? "Search projects" : "Search " + KEPT;
    box.replaceChildren();
    const restore = () => { if (sc && (changed || sc.scrollTop !== keep)) sc.scrollTop = keep; };
    if (mode === "projects") {
      const projects = CZ.sidebarRows(S.q);
      if (!projects) { box.appendChild(el("p", "chat-empty", "Loading projects…")); return; }
      const rows = [...projects.filter((p) => p.pinned), ...projects.filter((p) => !p.pinned)];  // the dashboard's order (the user arranges it there)
      if (!rows.length) box.appendChild(el("p", "chat-empty", S.q ? "Nothing matches." : "No open projects. Archived ones are on the dashboard."));
      for (const p of rows) box.appendChild(CZ.sidebarRow(p));
      restore();
      return;
    }
    const rows = S.list.map((c) => ({ c, t: c.updated || 0 })).sort((a, b) => b.t - a.t);
    if (!rows.length) { box.appendChild(el("p", "chat-empty", S.q ? "Nothing matches." : `No ${KEPT.replace(" and ", " or ")} yet.`)); restore(); return; }
    const openTalk = window.HaloChat ? window.HaloChat.openId() : null;
    for (const r of rows) {
      const c = r.c;
      let on = S.page === "talk" && c.id === openTalk, href = `#/talk/${c.id}`, icon = ICON.chat, untitled = "New chat", what = "chat";
      const item = el("div", "chat-item h-item" + (on ? " on" : ""));
      const a = el("a", "", ""); a.href = href; a.title = c.title;
      if (on) a.setAttribute("aria-current", "page");
      a.append(svg(icon, "h-ico"), el("span", "h-t", c.title || untitled));
      a.lastChild.setAttribute("aria-label", `${c.title}, ${what}`);
      // one ⋯ per row (a long press opens it too): Rename, Delete
      const more = el("button", "h-more"); more.type = "button"; more.setAttribute("aria-haspopup", "menu"); more.setAttribute("aria-expanded", "false");
      more.setAttribute("aria-label", `More for ${c.title}`);
      more.appendChild(svg('<circle cx="6" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="18" cy="12" r="1.4"/>', "h-more-ic"));
      item.append(a, more);
      attachRowMenu(item, more, () => [
        { label: "Rename", key: "R", fn: () => renameInline(item, c) },
        { label: "Delete", key: "D", cls: "danger", fn: () => {
          let ask = `Delete "${c.title}"?${keepsFolder(c)}`;
          confirmInline(item, ask, "Delete", async () => {
            try { await call("DELETE", `/studio/api/chats/${c.id}`); } catch (e) { H.toast(e.message, true); return; }
            if (openTalk === c.id) location.hash = "#/talk";
            loadList();
          });
        } }]);
      box.appendChild(item);
    }
    restore();
  }
  function closeRowMenu(refocus) {
    const m = document.querySelector(".h-menu");
    if (!m) return;
    if (m._btn) m._btn.setAttribute("aria-expanded", "false");
    m.remove();
    if (refocus && m._btn && m._btn.isConnected) m._btn.focus();
  }
  // a row's menu: items {label, key, fn, cls, sub, sep}. A sub opens in place with a Back row; letters
  // are shortcuts while it is open. It opens from the row's ⋯, a right-click or a long press, floats over the list
  // (flipping up near the bottom) and is a sheet at the bottom of a phone.
  function rowMenu(item, btn, items, at) {
    const open = document.querySelector(".h-menu");
    closeRowMenu();
    if (open && open._item === item && !at) return;
    const m = el("div", "h-menu floating"); m.setAttribute("role", "menu"); m._item = item; m._btn = btn;
    const label = (item.querySelector(".h-t") || {}).textContent;
    if (label) m.setAttribute("aria-label", label);
    const add = (x, onBack) => {
      if (x.sep) { m.appendChild(el("div", "h-sep")); return; }
      const b = el("button", "h-mi" + (x.cls ? " " + x.cls : "") + (x.sub ? " has-sub" : "") + (onBack ? " back" : "")); b.type = "button"; b.setAttribute("role", "menuitem");
      if (onBack) b.appendChild(svg('<path d="M14.5 6l-6 6 6 6"/>', "h-mi-ch"));
      b.appendChild(el("span", "h-mi-l", x.label));
      if (x.key) { const k = el("kbd", "h-key", x.key); k.setAttribute("aria-hidden", "true"); b.appendChild(k); b.setAttribute("aria-keyshortcuts", x.key); }
      if (x.sub) { b.setAttribute("aria-haspopup", "menu"); b.appendChild(svg('<path d="M9.5 6l6 6-6 6"/>', "h-mi-ch")); }
      b.addEventListener("click", (e) => { e.stopPropagation(); if (onBack) { onBack(); return; } if (x.sub) { fill(x.sub(), x.label); return; } closeRowMenu(); x.fn(); });
      m.appendChild(b);
    };
    const fill = (list, sub) => {
      m.replaceChildren();
      if (sub) add({ label: sub }, () => fill(items));
      for (const x of list) add(x);
      place();
      (m.querySelector(sub ? ".h-mi:not(.back)" : ".h-mi") || m).focus();
    };
    const place = () => {
      const phone = window.matchMedia("(max-width: 600px)").matches;
      m.classList.toggle("sheet", phone);
      if (phone) { m.style.left = ""; m.style.top = ""; return; }
      const r = (btn && btn.offsetParent ? btn : item).getBoundingClientRect(), w = m.offsetWidth, h = m.offsetHeight;
      let x = at ? at.x : r.right - w, y = at ? at.y : r.bottom + 4;
      x = Math.max(8, Math.min(x, innerWidth - w - 8));
      if (y + h > innerHeight - 8) y = !at && r.top - h - 4 >= 8 ? r.top - h - 4 : Math.max(8, innerHeight - h - 8);  // above its ⋯, else as low as fits
      m.style.left = `${x}px`; m.style.top = `${y}px`;
    };
    document.body.appendChild(m);
    if (btn) btn.setAttribute("aria-expanded", "true");
    fill(items);
    m.addEventListener("keydown", (e) => {
      const o = [...m.querySelectorAll(".h-mi")], i = o.indexOf(document.activeElement);
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeRowMenu(true); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); o[(i + 1) % o.length].focus(); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); o[(i - 1 + o.length) % o.length].focus(); return; }
      if (e.key === "ArrowLeft" && m.querySelector(".h-mi.back")) { e.preventDefault(); m.querySelector(".h-mi.back").click(); return; }
      if (e.key === "ArrowRight" && document.activeElement.classList.contains("has-sub")) { e.preventDefault(); document.activeElement.click(); return; }
      if (e.key === "Tab") { closeRowMenu(); return; }
      const k = e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey ? e.key.toUpperCase() : null;
      const hit = k && o.find((b) => b.getAttribute("aria-keyshortcuts") === k);
      if (hit) { e.preventDefault(); hit.click(); }
    });
  }
  // the ways in to a row's menu: its ⋯, a right-click (at the pointer) and a long press on a phone (not a tap)
  function attachRowMenu(item, more, items) {
    more.addEventListener("click", (e) => { e.stopPropagation(); rowMenu(item, more, items()); });
    item.addEventListener("contextmenu", (e) => { if (item.querySelector("input")) return; e.preventDefault(); rowMenu(item, more, items(), { x: e.clientX, y: e.clientY }); });
    let t = 0, sx = 0, sy = 0, fired = false;
    item.addEventListener("touchstart", (e) => {
      const p = e.touches[0]; sx = p.clientX; sy = p.clientY; fired = false; clearTimeout(t);
      t = setTimeout(() => { fired = true; rowMenu(item, more, items()); }, 520);
    }, { passive: true });
    item.addEventListener("touchmove", (e) => { const p = e.touches[0]; if (Math.hypot(p.clientX - sx, p.clientY - sy) > 10) clearTimeout(t); }, { passive: true });
    item.addEventListener("touchend", (e) => { clearTimeout(t); if (fired) e.preventDefault(); });
    item.addEventListener("click", (e) => { if (fired) { e.preventDefault(); e.stopPropagation(); fired = false; } }, true);
  }
  document.addEventListener("click", (e) => { if (!e.target.closest(".h-menu")) closeRowMenu(); });
  window.addEventListener("resize", () => closeRowMenu());
  function renameInline(item, c) {
    const input = el("input"); input.value = c.title; input.maxLength = 200; input.setAttribute("aria-label", "Title");
    item.replaceChildren(input); input.focus(); input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const t = input.value.trim(); input.remove();
      if (save && t && t !== c.title) {
        try {
          await call("PUT", `/studio/api/chats/${c.id}`, { title: t });
        } catch (e) { H.toast(e.message, true); }
      }
      loadList();
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
    input.addEventListener("blur", () => finish(true));
  }
  // a chat that had Full access keeps its folder on this Mac when it is deleted (chat_full.py: the user's files are
  // never deleted by themselves): the delete confirms say so
  function keepsFolder(c) {
    let o = {};
    try { o = JSON.parse((c && c.perm_opts) || "{}") || {}; } catch (e) { o = {}; }
    const f = o.folder || o.last_folder;
    return f ? ` Its folder ${f} stays on this Mac.` : "";
  }
  // an in-page confirm in place of a row, never window.confirm
  function confirmInline(host, text, yes, onYes) {
    const keep = [...host.childNodes], back = document.activeElement;  // focus goes back where it was
    const box = el("div", "inline-confirm");
    box.setAttribute("role", "alertdialog"); box.setAttribute("aria-label", text);
    const p = el("p", "", text);
    const ok = el("button", "go danger-solid", yes); ok.type = "button";
    const no = el("button", "textbtn", "Cancel"); no.type = "button";
    const restore = () => host.replaceChildren(...keep);
    const cancel = () => {
      restore();
      if (back && back.isConnected && back !== document.body) back.focus();
      else { const f = host.querySelector("button, a, input"); if (f) f.focus(); }  // (the menu item it came from is gone)
      if (host.closest && host.closest("#s-list")) {  // what changed while it was open, and focus on the same row after
        const a = host.querySelector("a[href]"), href = a && a.getAttribute("href");
        Promise.resolve(loadList()).then(() => {
          const n = href && [...document.querySelectorAll("#s-list a[href]")].find((x) => x.getAttribute("href") === href);
          if (n) n.focus({ preventScroll: true });
        });
      }
    };
    ok.addEventListener("click", () => { restore(); onYes(); });
    no.addEventListener("click", cancel);
    box.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); cancel(); } });
    box.append(p, el("div", "row"));
    box.lastChild.append(ok, no);
    host.replaceChildren(box);
    no.focus();  // Enter never deletes by itself
  }
  let searchT = 0;
  $("s-search").addEventListener("input", (e) => { clearTimeout(searchT); searchT = setTimeout(() => { S.q = e.target.value.trim(); loadList(); }, 200); });
  // New session: a new chat
  const newSession = () => { if (location.hash === "#/talk") window.HaloChat && window.HaloChat.route("talk", null, true); else location.hash = "#/talk"; window.HaloDrawer && window.HaloDrawer(false); setTimeout(() => { const t = $("t-prompt"); if (t) t.focus(); }, 50); };
  $("s-new").addEventListener("click", newSession);
  $("s-new-foot").addEventListener("click", newSession);  // a phone's: the pill at the sidebar's foot
  $("s-search-btn").setAttribute("aria-label", "Search " + KEPT);
  $("s-search-wrap").querySelector(".sr").textContent = "Search " + KEPT;
  $("s-search-btn").addEventListener("click", () => {
    const w = $("s-search-wrap"), open = w.hidden;
    w.hidden = !open; $("s-search-btn").setAttribute("aria-expanded", String(open));
    if (open) $("s-search").focus(); else if (S.q) { $("s-search").value = ""; S.q = ""; loadList(); }
  });

  // an attachment's view: the server's, else the upload's thumbnail by id
  function refOf(id, c) {
    return (c && c.refs || {})[id] ||
      (/^u/.test(id) ? { id, kind: "image", thumb_url: `/studio/ref/${id}.thumb` } : null);
  }
  function attThumb(ref, role, small) {
    const w = el("figure", "att-thumb" + (small ? " sm" : ""));
    if (!ref || ref.missing) { w.appendChild(el("span", "att-gone", "removed")); }
    else if (ref.kind === "image") {
      const i = el("img"); i.src = ref.thumb_url || ref.url; i.alt = ""; i.loading = "lazy"; w.appendChild(i);
      i.addEventListener("error", () => { w.classList.add("att-icon"); i.replaceWith(svg(ICON.image)); }, { once: true });
    }
    else { w.classList.add("att-icon"); w.appendChild(svg(ICON[ref.kind] || ICON.image)); }
    return w;
  }
  async function copy(text, b) {
    try { await navigator.clipboard.writeText(text); }
    catch (e) { const ta = el("textarea"); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); }
    if (b) {  // its own children come back (restoring textContent wiped an icon button's svg)
      if (b.dataset.copying) return;
      const kids = [...b.childNodes], label = b.getAttribute("aria-label");
      b.dataset.copying = "1";
      if (b.querySelector("svg")) { b.classList.add("copied"); b.setAttribute("aria-label", "Copied"); }
      else b.textContent = "Copied";
      setTimeout(() => { b.replaceChildren(...kids); b.classList.remove("copied"); if (label) b.setAttribute("aria-label", label); delete b.dataset.copying; }, 1200);
    }
  }
  // ---------------------------------------------------------------- "Open elsewhere": plain addresses from the state (never a token)
  function elsewhere(host, L) {
    const rows = !L ? [] : L.here === "tailnet"
      ? (L.home && L.home.length ? [{ label: "Dashboard (home Wi-Fi)", urls: L.home, note: "Works on home Wi-Fi only. StrixFold UI itself stays on this secure link." }] : [])
      : [L.dashboard && { label: "Dashboard on this Mac", urls: [L.dashboard] },
         L.phone && { label: "Phone (Tailscale)", urls: [L.phone], note: "StrixFold UI over HTTPS on your Tailscale devices." }].filter(Boolean);
    host.replaceChildren();
    for (const r of rows) {
      const row = el("div", "else-row");
      row.appendChild(el("span", "else-l", r.label));
      for (const u of r.urls) {
        const line = el("div", "else-u");
        const a = el("a", "else-link", u.replace(/^https?:\/\//, "").replace(/\/$/, "")); a.href = u; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.title = u;
        const c = el("button", "textbtn else-copy", "Copy"); c.type = "button"; c.setAttribute("aria-label", `Copy ${u}`);
        c.addEventListener("click", () => copy(u, c));
        line.append(a, c);
        row.appendChild(line);
      }
      if (r.note) row.appendChild(el("small", "else-note", r.note));
      host.appendChild(row);
    }
    return rows.length > 0;
  }
  function syncElsewhere() {
    const L = S.st && S.st.links, sig = JSON.stringify(L || null);
    if (sig === S.elseSig) return;
    S.elseSig = sig;
    $("s-else").hidden = !elsewhere($("s-else-rows"), L);
    const ss = $("ss-else");
    if (ss) ss.hidden = !elsewhere(ss.querySelector(".s-else-rows"), L);
  }

  // ---------------------------------------------------------------- a chat's PDF in the viewer (chat_docs)
  // The installed app (display: standalone) shows a PDF it opens with no way back, and a phone's installed app saves no
  // download. So a PDF opens here, beside the chat or over it on a phone, with the viewer's own ×: its pages drawn by
  // the Mac (studio_http.doc_view), and Save, which is the share sheet (Save to Files) where a download goes nowhere. A
  // tapped Save again finds the bytes in SAVES (a share needs the tap fresh)
  function fileOf(url) {
    if (!SAVES.has(url)) {
      const p = fetch(url, { headers: privHeaders(), credentials: "same-origin", cache: "no-store" }).then((r) => {
        if (!r.ok) throw new Error(r.status === 401 || r.status === 403 ? "Unlock this chat to open its files." : "The file couldn't be opened.");
        return r.blob();
      });
      SAVES.set(url, p);
      p.catch(() => SAVES.delete(url));
      setTimeout(() => SAVES.delete(url), 120000);
    }
    return SAVES.get(url);
  }
  async function saveFile(url, name) {
    try {
      const blob = await fileOf(url);
      const file = new File([blob], name, { type: blob.type || "application/octet-stream" });
      if (matchMedia("(pointer: coarse)").matches && navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: name }); } catch (e) {
          if (e.name === "NotAllowedError") H.toast("The file is ready: tap Save again.");  // the tap went stale while it came
          else if (e.name !== "AbortError") throw e;
        }
        return;
      }
      const u = URL.createObjectURL(blob), a = el("a");
      a.href = u; a.download = name; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(u), 60000);
    } catch (e) { H.toast(e.message || "It couldn't be saved.", true); }
  }
  async function renderDoc(it, b) {
    const v = S.v, name = it.name || "document.pdf";
    v.urls = v.urls || [];
    $("v-title").textContent = name;
    b.replaceChildren();
    const acts = el("div", "v-acts"), save = el("button", "v-act", "Save");
    save.type = "button"; save.title = `Save ${name}`;
    save.addEventListener("click", () => saveFile(`/studio/doc/${it.id}?dl=1`, name));
    acts.appendChild(save);
    const pages = el("div", "v-doc");
    b.append(acts, pages);
    // here before Save is tapped: a phone's share sheet needs the tap fresh (a wide screen's download doesn't)
    if (matchMedia("(pointer: coarse)").matches) fileOf(`/studio/doc/${it.id}?dl=1`).catch(() => {});
    let info;
    try { info = await call("GET", `/studio/doc/${it.id}/info`); } catch (e) { if (S.v === v) pages.appendChild(el("p", "v-doc-note", e.message)); return; }
    if (S.v !== v) return;
    const n = info.pages || 0;
    $("v-title").textContent = `${name} · ${n} page${n === 1 ? "" : "s"}`;
    v.io = new IntersectionObserver((ents) => {
      for (const en of ents) {
        if (!en.isIntersecting) continue;
        v.io.unobserve(en.target);
        drawPage(v, it, en.target);
      }
    }, { root: b, rootMargin: "800px 0px" });
    for (let k = 1; k <= n; k++) {
      const pg = el("div", "v-page"), sz = (info.sizes || [])[k - 1];
      pg.dataset.n = k; pg.setAttribute("aria-label", `Page ${k} of ${n}`); pg.setAttribute("role", "img");
      pg.style.aspectRatio = sz ? `${sz[0]} / ${sz[1]}` : "8.5 / 11";
      pages.appendChild(pg); v.io.observe(pg);
    }
  }
  async function drawPage(v, it, pg) {
    try {
      const r = await fetch(`/studio/doc/${it.id}/page/${pg.dataset.n}`, { headers: privHeaders(), credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw new Error();
      const u = URL.createObjectURL(await r.blob());
      if (S.v !== v) { URL.revokeObjectURL(u); return; }
      v.urls.push(u);
      const img = el("img"); img.src = u; img.alt = ""; pg.appendChild(img);
    } catch (e) { if (S.v === v) pg.appendChild(el("p", "v-doc-note", `Page ${pg.dataset.n} couldn't be drawn.`)); }
  }

  // ---------------------------------------------------------------- the viewer: a panel beside the chat
  async function openViewer(it
  ) {
    document.dispatchEvent(new CustomEvent("halo:viewer-open"));  // the chat's Agents panel gives up the column
    if (S.v) dropDocView(S.v);  // another document open before: its pages go
    S.v = { it, trigger: document.activeElement };
    $("app").classList.add("viewer-open"); document.documentElement.classList.add("v-open");
    $("viewer").hidden = false;
    renderViewer();
    $("v-close").focus();
  }
  function dropDocView(v) {  // a document's pages (renderDoc): their observer and their images
    if (v.io) v.io.disconnect();
    (v.urls || []).forEach((u) => URL.revokeObjectURL(u));
    v.urls = [];
  }
  // Lock, the idle lock and a Private chat's own Lock: a Private chat's document leaves the viewer and every document's
  // bytes leave the Save cache (review: its pages stayed open, and Save worked for 120 s without the grant)
  function forgetPrivateFiles() {
    SAVES.clear();
    if (S.v && S.v.it.priv) closeViewer();
  }
  function closeViewer() {
    if (!S.v) return;
    const trig = S.v.trigger;
    dropDocView(S.v);
    S.v = null;
    $("viewer").hidden = true; $("app").classList.remove("viewer-open"); document.documentElement.classList.remove("v-open");
    $("v-body").replaceChildren();
    if (trig && trig.isConnected) trig.focus();
  }
  $("v-close").addEventListener("click", closeViewer);
  document.addEventListener("keydown", (e) => {
    if (!S.v || e.target.closest("input, textarea, select")) return;
    if (e.key === "Escape") { closeViewer(); e.preventDefault(); }
  });
  function renderViewer() {
    const it = S.v.it, b = $("v-body");
    return renderDoc(it, b);
  }
  function statusOf(st) {  // the header chip's words for the model
    if (!st) return null;
    const l = st.llm || {};
    if (!st.llm) return null;
    if (l.state === "ready" && l.unc) return { on: true, text: "Ready" };
    if (["waiting", "loading", "linking"].includes(l.state)) return { busy: true, text: "Starting the model" };
    return { text: l.state === "ready" ? `${l.model_name || l.profile_name || "Another model"} is loaded` : "Model stopped" };
  }
  window.HaloStudioStatusOf = statusOf;
  // each model's one name in StrixFold UI (halo_chat.MODEL_NAMES): the dashboard's header uses it on StrixFold UI's pages too
  const MODEL_NAMES = { dsv41: "DeepSeek V4.1", "dsv41-unc": "DeepSeek uncensored", qwen38: "Qwen 3.8", "qwen38-kyojin": "Qwen 3.8 (Kyojin)", "qwen38-halogen": "Qwen 3.8", "qwen38-gufo21": "Qwen 3.8 (Gufo 0.21)" };
  window.HaloModelName = (id) => MODEL_NAMES[id] || null;
  window.HaloStudioStatus = () => (S.auth && S.auth.unlocked ? statusOf(S.st) : null);
  window.HaloStudioDown = () => !!S.down || $("s-lock").classList.contains("down");  // couldn't reach the Mac
  // ---------------------------------------------------------------- polling the state
  let pollT = 0, polling = 0;
  async function pollState() {
    clearTimeout(pollT);
    if (!(S.auth && S.auth.unlocked)) { pollT = setTimeout(pollState, 10000); if (!S.auth) checkAuth(); return; }
    if (polling && Date.now() - polling < 10000) return;  // one at a time (a fetch frozen with a backgrounded tab: 10 s)
    const mine = polling = Date.now();
    try {
      const openId =
        S.page === "talk" && window.HaloChat ? window.HaloChat.openId()
        : S.page === "cousteau" && window.HaloCousteau ? window.HaloCousteau.openId() : null;  // a project's thread in the Command Centre
      const ask = openId ? [`chat=${openId}`] : [];
      S.st = await call("GET", `/studio/api/state?${ask.join("&")}`);
      S.down = 0;
      if (polling !== mine) return;  // a newer poll took over
      const rev = S.st.rev || {};
      if (rev.list && rev.list !== S.listRev) { const first = !S.listRev; S.listRev = rev.list; if (!first) loadList(); }
      if (window.HaloChat) window.HaloChat.onState(S.st, rev);
      if (window.HaloCousteau) window.HaloCousteau.onState(S.st, rev);
      const ini = $("h-initials"); if (ini && S.st.you && S.st.you.initials) ini.textContent = S.st.you.initials;
      syncElsewhere();
      window.dispatchEvent(new Event("halo-studio-state"));
    } catch (e) {  // locked or unreachable: the next poll tries again; a network failure (no status) is "can't reach"
      console.debug("state poll:", e);
      if (!e.status || e.down) {
        S.down = (S.down || 0) + 1; window.dispatchEvent(new Event("halo-studio-state"));
      }
    }
    if (polling !== mine) return;
    polling = 0;
    let pace = S.page ? 2000 : 15000;
    const back = S.down ? Math.min(10000, 2000 * S.down) : 0;  // slower while the Mac is away
    pollT = setTimeout(pollState, back || (document.hidden ? 15000 : pace));
  }
  // back to the page (a tab brought forward, iOS restoring it from its cache, focus, the network back): everything at once
  function wake() {
    if (document.hidden) return;
    polling = 0;
    pollState();  // its revs bring the open chat and the list up to date
  }
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("pageshow", (e) => { if (e.persisted) wake(); });
  window.addEventListener("focus", wake);
  window.addEventListener("online", wake);

  // ---------------------------------------------------------------- pull down to refresh (the page's own scroller stops Safari's)
  /* pull: pure (tests/test_studio.py runs this block in node) */
  const PULL = { T: 70, MAX: 116, SLOP: 6 };
  // one step of the gesture: g so far, ev = { type: start|move|end, x, y, top (scrollTop), touches, blocked }
  function pullStep(g, ev) {
    if (ev.type === "start") return ev.touches === 1 && ev.top <= 0 && !ev.blocked ? { x0: ev.x, y0: ev.y, on: false, pull: 0, armed: false } : { dead: true, pull: 0 };
    if (!g || g.dead) return { dead: true, pull: 0 };
    if (ev.type === "end") return Object.assign({}, g, { done: true, fire: !!(g.on && g.armed) });
    if (ev.touches > 1) return { dead: true, pull: 0 };  // a pinch
    const dx = ev.x - g.x0, dy = ev.y - g.y0;
    if (!g.on) {
      if (ev.top > 0 || dy < -PULL.SLOP || (Math.abs(dx) > PULL.SLOP && Math.abs(dx) >= dy)) return { dead: true, pull: 0 };  // a scroll or a swipe
      if (dy < PULL.SLOP) return g;
    }
    const d = Math.max(0, dy - PULL.SLOP);  // resistance: half the finger's travel, then a fifth, up to MAX
    const pull = Math.min(PULL.MAX, d < 2 * PULL.T ? d / 2 : PULL.T + (d - 2 * PULL.T) / 5);
    return Object.assign({}, g, { on: true, pull, armed: pull >= PULL.T });
  }
  /* pull: end */
  (() => {
    const main = $("main");
    if (!main) return;
    const ind = el("div", "s-pull"); ind.setAttribute("aria-hidden", "true"); ind.appendChild(el("i"));
    const say = el("p", "sr"); say.setAttribute("role", "status");
    document.body.append(ind, say);
    let g = null, busy = false;
    const pages = ["talk", "projects"];
    let off = "#t-composer, #t-sheet-wrap, #t-menu, #viewer, .viewer, .s-lock, #side, .side, input, textarea, select";  // where it never starts
    const blocked = (t) => !pages.includes(S.page) || !(S.auth && S.auth.unlocked) || busy || !!document.querySelector(".side-open")
      || (window.visualViewport && window.visualViewport.scale > 1.01)
      || !!(t.closest && t.closest(off));
    const show = (pull, armed) => {
      const top = (document.querySelector(".topbar") || main).getBoundingClientRect().bottom;
      ind.style.top = `${Math.round(top)}px`;
      const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;  // it follows the finger, without turning
      ind.style.transform = `translate(-50%, ${Math.round(pull - 44)}px)` + (still ? "" : ` rotate(${Math.round(pull * 3)}deg)`);
      ind.style.opacity = String(Math.min(1, pull / PULL.T));
      ind.classList.toggle("armed", !!armed);
    };
    main.addEventListener("touchstart", (e) => {
      const t = e.touches[0];
      g = pullStep(null, { type: "start", x: t.clientX, y: t.clientY, top: main.scrollTop, touches: e.touches.length, blocked: blocked(e.target) });
    }, { passive: true });
    main.addEventListener("touchmove", (e) => {
      if (!g || g.dead) return;
      const t = e.touches[0];
      g = pullStep(g, { type: "move", x: t.clientX, y: t.clientY, top: main.scrollTop, touches: e.touches.length });
      if (g.dead) { ind.classList.remove("on"); return; }
      if (g.on) { e.preventDefault(); ind.classList.add("on"); show(g.pull, g.armed); }
    }, { passive: false });
    const end = async () => {
      if (!g || g.dead || !g.on) { g = null; return; }
      const fire = pullStep(g, { type: "end" }).fire;
      g = null;
      if (!fire) { ind.classList.remove("on", "armed"); return; }
      busy = true; ind.classList.add("spin"); show(PULL.T, true); say.textContent = "Refreshing";
      try { await softRefresh(); } finally {
        busy = false; ind.classList.remove("on", "armed", "spin"); say.textContent = "";
      }
    };
    main.addEventListener("touchend", end);
    main.addEventListener("touchcancel", () => { g = null; ind.classList.remove("on", "armed"); });
  })();
  // a swipe to the right on a phone opens the drawer, a swipe to the left closes it; it starts in the
  // left part of the page, not at the very edge (Safari's own back swipe lives there), and not on what scrolls sideways
  /* swipe: pure (tests/test_studio.py runs this block in node) */
  const SWIPE_EDGE = 24;  // Safari's back swipe starts within this many px of the left edge
  function swipeZone(x, width) { return x >= SWIPE_EDGE && x <= width * 0.4; }
  /* swipe: end */
  (() => {
    let x0 = null, y0 = 0, mode = null;
    const phone = () => window.matchMedia("(max-width: 860px)").matches;
    const isOpen = () => !!document.querySelector(".app.side-open");
    let off = "pre, table, .t-composer, #t-sheet-wrap, #viewer, input, textarea, select";  // where it never starts
    document.addEventListener("touchstart", (e) => {
      x0 = null;
      if (!phone() || !S.page || e.touches.length !== 1 || !window.HaloDrawer) return;
      const t = e.touches[0], tg = e.target;
      if (isOpen()) { if (tg.closest && tg.closest("#side")) { x0 = t.clientX; y0 = t.clientY; mode = "close"; } return; }
      if (!swipeZone(t.clientX, window.innerWidth) || (tg.closest && tg.closest(off))) return;
      x0 = t.clientX; y0 = t.clientY; mode = "open";
    }, { passive: true });
    document.addEventListener("touchmove", (e) => {
      if (x0 === null) return;
      const t = e.touches[0], dx = t.clientX - x0, dy = t.clientY - y0;
      if (Math.abs(dy) > 40 && Math.abs(dy) > Math.abs(dx)) { x0 = null; return; }  // a scroll
      if (mode === "open" && dx > 70) { x0 = null; window.HaloDrawer(true); }
      if (mode === "close" && dx < -70) { x0 = null; window.HaloDrawer(false); }
    }, { passive: true });
    document.addEventListener("touchend", () => { x0 = null; });
  })();
  // state, the chat and the list again; the composer's draft and attachments stay as they are
  async function softRefresh() {
    try { await call("GET", "/studio/api/auth"); } catch (e) { if (e.status !== 401) location.reload(); return; }  // the soft way failed
    await Promise.all([pollState(), loadList(),
      (S.page === "talk" || S.page === "projects") && window.HaloChat ? window.HaloChat.refresh() : null]);
  }


  // ---------------------------------------------------------------- Settings
  async function loadSettings() {
    let d;
    try { d = await call("GET", "/studio/api/settings"); } catch (e) { return; }
    const s = d.settings, box = $("ss-body");
    box.replaceChildren();
    const save = async (patch) => { try { const r = await call("PUT", "/studio/api/settings", patch); Object.assign(s, r.settings); if (S.st) S.st.settings = r.settings; H.toast("Saved"); } catch (e) { H.toast(e.message, true); loadSettings(); } };
    const sec = (title, note) => { const x = el("section", "ss-sec"); const h = el("div", "ss-h"); h.appendChild(el("h2", "", title)); if (note) h.appendChild(el("p", "", note)); x.appendChild(h); box.appendChild(x); return x; };
    const segRow = (host, label, key, opts) => {
      const r = el("div", "ss-row"); r.appendChild(el("span", "ss-l", label));
      const seg = el("div", "seg"); seg.setAttribute("role", "radiogroup"); seg.setAttribute("aria-label", label);
      for (const [v, t, dis] of opts) { const b = el("button", "", t); b.type = "button"; b.setAttribute("role", "radio"); b.setAttribute("aria-checked", String(s[key] === v)); b.disabled = !!dis; if (dis) b.title = dis;
        b.addEventListener("click", () => { seg.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b))); save({ [key]: v }); }); seg.appendChild(b); }
      r.appendChild(seg); host.appendChild(r);
    };
    const p = sec("Privacy", null);
    const facts = el("dl", "facts ss-facts");
    const fact = (k, v) => { const x = el("div"); x.append(el("dt", "", k), el("dd", "", v)); facts.appendChild(x); };
    fact("Stored in", d.root); fact("Access", "Folders 700, files 600. Not synced to iCloud. Each request needs this passcode or a paired device, also at home.");
    p.appendChild(facts);
    segRow(p, "Lock when idle", "idle_lock_min", [[0, "Off"], [1, "1 min"], [5, "5 min"], [15, "15 min"], [30, "30 min"]]);
    p.querySelectorAll("[aria-label='Lock when idle'] button").forEach((b, i) => b.addEventListener("click", () => {
      if (S.auth && S.auth.local) { S.auth.idle_lock_min = [0, 1, 5, 15, 30][i]; lsSet(IDLE_MIN, S.auth.idle_lock_min); }
    }));
    p.appendChild(el("p", "ss-p", "On this Mac only (127.0.0.1:8711). With no mouse or keyboard input in StrixFold UI for that long, it locks and "
      + "the window blurs until you enter the passcode. It hides the window; it doesn't clear it. Your phone is not affected."));
    const lock = el("button", "go", "Lock now"); lock.type = "button";
    lock.addEventListener("click", async () => { try { await call("POST", "/studio/api/auth/lock"); } catch (e) { /* locked either way */ } S.auth.unlocked = false; locked(); });
    p.appendChild(lock);
    const cf = el("form", "ss-pass"); cf.noValidate = true;
    cf.appendChild(el("h3", "", "Change the passcode"));
    const f1 = el("label", "s-field"); f1.appendChild(el("span", "", "Current passcode")); const i1 = el("input"); i1.type = "password"; i1.autocomplete = "current-password"; f1.appendChild(i1);
    const f2 = el("label", "s-field"); f2.appendChild(el("span", "", "New passcode")); const i2 = el("input"); i2.type = "password"; i2.autocomplete = "new-password"; f2.appendChild(i2);
    const err = el("p", "err"); const go = el("button", "go", "Change passcode"); go.type = "submit";
    cf.append(f1, f2, err, go);
    cf.addEventListener("submit", async (e) => { e.preventDefault(); err.textContent = "";
      try { await call("POST", "/studio/api/auth/change", { old: i1.value, new: i2.value }); H.toast("Passcode changed; other devices sign in again"); loadSettings(); } catch (x) { err.textContent = x.message; } });
    p.appendChild(cf);
    p.appendChild(el("h3", "ss-sub", "Paired devices"));
    const ul = el("ul", "ss-devs");
    for (const dv of d.devices) {
      const li = el("li");
      li.appendChild(el("span", "", `${dv.label}${(dv.ids || [dv.id]).includes(d.this_device) ? " (this one)" : ""}`));
      li.appendChild(el("small", "", `Last used ${new Date(dv.last_seen * 1000).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" })} · ${dv.remember ? "paired for 90 days" : "until 12 h idle"}${dv.client ? ` · ${dv.client}` : ""}`));
      const rm = el("button", "textbtn danger", "Sign out"); rm.type = "button"; rm.setAttribute("aria-label", `Sign out ${dv.label}`);
      rm.addEventListener("click", async () => { try { const r = await call("DELETE", `/studio/api/devices/${dv.id}`); if (r.self) { locked(); return; } loadSettings(); } catch (e) { H.toast(e.message, true); } });
      li.appendChild(rm); ul.appendChild(li);
    }
    p.appendChild(ul);
    const es = sec("Open elsewhere"); es.id = "ss-else"; es.appendChild(el("div", "s-else-rows"));
    S.elseSig = null; syncElsewhere();
  }

  // ---------------------------------------------------------------- start
  // chat.js routed before this file loaded: route again now that its page can be shown
  checkAuth().then((ok) => {
    let ours = /^#\/(studio-settings|talk|projects|cousteau)\b/.test(location.hash);
    if (ours && window.HaloRoute) window.HaloRoute();
    else if (ok) { loadList(); pollState(); } else pollState();
  });
})();
