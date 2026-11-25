// The phone page's shell (studio-shell.html, what the tailnet gets through `tailscale serve`): the hash router, the
// drawer, the toast and the status chip that app.js and chat.js give the same pages inside the full dashboard. It calls
// /studio/ only; the dashboard's own API is not reachable from the tailnet.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  function toast(msg, bad) {
    const t = $("toast");
    t.textContent = msg; t.classList.toggle("bad", !!bad); t.hidden = false;
    clearTimeout(toast.h); toast.h = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3200);
  }
  window.Halo = { toast };

  function setDrawer(open) {
    $("app").classList.toggle("side-open", open);
    $("scrim").hidden = !open;
    $("open-side").setAttribute("aria-expanded", String(open));
    if (open) ($("s-new").offsetParent ? $("s-new") : $("s-new-foot")).focus();  // a phone shows the foot's pill instead
  }
  window.HaloDrawer = setDrawer;
  $("open-side").addEventListener("click", () => setDrawer(true));
  $("side-close").addEventListener("click", () => setDrawer(false));
  $("scrim").addEventListener("click", () => setDrawer(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("app").classList.contains("side-open")) setDrawer(false); });

  function route() {
    const h = location.hash || "";
    let m = h.match(/^#\/(talk|projects|studio-settings)(?:\/([0-9a-f]{6,40}))?$/);  // an old #/runs link: New session
    const cz = h.match(/^#\/cousteau((?:\/[a-z0-9-]{1,40}){0,2})$/);  // the Command Centre: #/cousteau[/<page or project>[/<tab>]]
    if (!m && !cz) { if (!h || h === "#" || h.startsWith("#/")) location.hash = "#/talk"; return; }  // opens on New session
    const page = cz ? "cousteau" : m[1], id = cz ? cz[1].slice(1) || null : m[2] || null;
    document.querySelectorAll("#main > .page").forEach((p) => { p.hidden = p.id !== `page-${page}`; });
    document.querySelectorAll(".side-pages a").forEach((a) => a.classList.toggle("on", a.dataset.page === page));
    setDrawer(false);
    if (window.HaloStudio) window.HaloStudio.route(page, id);
  }
  window.HaloRoute = route;
  window.addEventListener("hashchange", route);
  $("side-studio").hidden = false;
  // the phone opens on New session (a new Chat)
  let known = /^#\/(talk|projects|studio-settings|cousteau)/.test(location.hash);
  if (!known) history.replaceState(null, "", "#/talk");

  // the skip link moves focus and leaves the address alone, as on the Mac ("#main" stayed in the phone's address)
  const skipA = document.querySelector("a.skip");
  if (skipA) skipA.addEventListener("click", (e) => { e.preventDefault(); const m = document.getElementById("main"); if (m) { m.setAttribute("tabindex", "-1"); m.focus(); } });
  // the chip: the model's state, from the same poll as the pages
  const dot = $("chip-dot"), text = $("chip-state");
  function paint(s) {
    dot.className = "chip-dot" + (s && s.busy ? " busy" : s && s.on ? " on" : "");
    const down = window.HaloStudioDown && window.HaloStudioDown();  // ("Locked" when the Mac was only away)
    text.textContent = down ? "Can't reach the Mac" : s ? s.text : "Locked";
    if (down) dot.className = "chip-dot";
    const hd = !$("page-talk").hidden && window.HaloChat && window.HaloChat.header();  // a chat's page (the one shown): its title in the header
    const cz = !hd && window.HaloCousteau && window.HaloCousteau.header();  // the Command Centre's pages: the page or project, and its line
    $("hd-model").textContent = hd ? hd.title : cz ? cz.title : "StrixFold UI";
    const cs = hd ? window.HaloChat.status && window.HaloChat.status() : cz ? cz.status : null;  // (what the boxes are busy with still wins)
    // the page's own status, unless the Mac can't be reached: then that wins everywhere
    if (!down && cs && !(s && s.busy && !/^Starting/.test(s.text))) { text.textContent = cs.text; dot.className = "chip-dot" + (cs.busy ? " busy" : cs.on ? " on" : ""); }
  }
  window.HaloShellPaint = () => paint(window.HaloStudioStatus && window.HaloStudioStatus());
  window.addEventListener("halo-studio-state", () => { const s = window.HaloStudioStatus && window.HaloStudioStatus(); if (s || (window.HaloStudioDown && window.HaloStudioDown())) paint(s); });
  async function chip() {
    let s = null;
    try {  // the lock first: no state request (and no 401) while it is locked
      const a = await (await fetch("/studio/api/auth", { cache: "no-store", credentials: "same-origin" })).json();
      if (a.unlocked) {
        s = window.HaloStudioStatus && window.HaloStudioStatus();
        if (!s) { const r = await fetch("/studio/api/state", { cache: "no-store", credentials: "same-origin" }); if (r.ok && window.HaloStudioStatusOf) s = window.HaloStudioStatusOf(await r.json()); }
      }
    } catch (e) { s = null; }
    paint(s);
    setTimeout(chip, document.hidden ? 15000 : 4000);
  }
  chip();
})();
