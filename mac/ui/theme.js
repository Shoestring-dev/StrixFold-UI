// The theme: System (the device's own setting), Light or Dark, chosen per device and kept in this browser
// (localStorage). Loaded in <head> before the stylesheets apply, so a page never paints in the other theme first. It
// sets data-theme on <html>, which every stylesheet here follows (Chat and the other pages). The switch is the
// Theme row under Appearance in Settings (#/studio-settings, the sidebar's avatar).
(() => {
  "use strict";
  // bin/halo-control opens the app with this run's key after #k= (halo_control.write_open_key): kept for this tab only,
  // and taken out of the address before anything else reads it, so it isn't in the history or a bookmark
  const k = /^#k=([A-Za-z0-9_-]{20,64})$/.exec(location.hash);
  if (k) {
    try { sessionStorage.setItem("halo.open-key", k[1]); } catch (e) { /* this tab can't skip, then */ }
    history.replaceState(null, "", location.pathname + location.search + "#/talk");
  }
})();
(() => {
  "use strict";
  const KEY = "halo.theme", root = document.documentElement;
  const get = () => { try { const v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : "system"; } catch (e) { return "system"; } };
  function apply(v) {
    if (v === "system") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", v);
    // the browser's own bar: per scheme for System, else the chosen one's colour
    for (const m of document.querySelectorAll('meta[name="theme-color"]')) {
      if (!m.dataset.media) m.dataset.media = m.media || "";
      m.media = v === "system" ? m.dataset.media : "";
      m.content = v === "system" ? (/dark/.test(m.dataset.media) ? "#000000" : "#ffffff") : (v === "dark" ? "#000000" : "#ffffff");
    }
  }
  function sync() {
    const v = get();
    for (const b of document.querySelectorAll("[data-theme-pick]")) {
      const on = b.dataset.themePick === v;
      b.setAttribute("aria-checked", String(on)); b.tabIndex = on ? 0 : -1;
    }
  }
  function set(v) {
    try { if (v === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, v); } catch (e) { /* a private window: this page only */ }
    apply(v); sync();
  }
  apply(get());
  // the Mac's idle lock (studio.js): a page opened after the limit with no input (a reopened or restored tab) is hidden
  // before it paints; studio.js then locks it with the blur and the passcode form. The limit is cached by studio.js
  // only on this Mac's own page (127.0.0.1), so the phone never gets this.
  try {
    const min = Number(localStorage.getItem("halo.idle.min")) || 0, last = Number(localStorage.getItem("halo.idle.active")) || 0;
    if (min > 0 && last > 0 && Date.now() - last >= min * 60000) root.classList.add("idle-boot");
  } catch (e) { /* a private window: no shared clock */ }
  document.addEventListener("DOMContentLoaded", () => {
    apply(get()); sync();
    for (const b of document.querySelectorAll("[data-theme-pick]")) {
      b.addEventListener("click", () => set(b.dataset.themePick));
      b.addEventListener("keydown", (e) => {  // a radio group: the arrows move the choice
        const all = [...document.querySelectorAll("[data-theme-pick]")].filter((x) => x.closest("[role=radiogroup]") === b.closest("[role=radiogroup]"));
        const i = all.indexOf(b), j = e.key === "ArrowRight" || e.key === "ArrowDown" ? i + 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? i - 1 : -1;
        if (j < 0 || j >= all.length) return;
        e.preventDefault(); all[j].focus(); all[j].click();
      });
    }
  });
  window.addEventListener("storage", (e) => { if (e.key === KEY) { apply(get()); sync(); } });  // another tab changed it
  window.HaloTheme = { get, set };
})();
