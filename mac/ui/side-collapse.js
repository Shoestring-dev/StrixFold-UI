// The sidebar folded away and back. On a wide screen (1024 px and up) the sidebar's Collapse button, the main area's
// Show sidebar button and Cmd+\ fold the sidebar away, so the page takes the whole width, and bring it back; a phone
// keeps its drawer. The choice is kept per browser (localStorage). Loaded in <head> like theme.js, so a page opened
// folded never paints the sidebar first. The look is chat.css's (html.side-collapsed); the move is 140 ms, and none
// with reduced motion.
(() => {
  "use strict";
  const KEY = "halo.side.collapsed", root = document.documentElement;
  const wide = () => window.matchMedia("(min-width: 1024px)").matches;
  try { if (localStorage.getItem(KEY) === "1") root.classList.add("side-collapsed"); } catch (e) { /* a private window: open */ }
  let settle = null;
  function sync() {
    const open = !root.classList.contains("side-collapsed");
    for (const id of ["side-collapse", "side-show"]) {
      const b = document.getElementById(id);
      if (b) b.setAttribute("aria-expanded", String(open));
    }
  }
  function set(folded, focus) {
    root.classList.add("side-anim");  // the move only when the user asks, never on load
    root.classList.toggle("side-collapsed", folded);
    clearTimeout(settle);
    settle = setTimeout(() => root.classList.remove("side-anim"), 200);
    try { localStorage.setItem(KEY, folded ? "1" : "0"); } catch (e) { /* this page only */ }
    sync();
    const b = focus && document.getElementById(folded ? "side-show" : "side-collapse");
    if (b) b.focus();
  }
  // its width, dragged at its edge: kept per browser like the fold; set here in <head> so a page opens at it; a double
  // click on the edge goes back to the look's width
  const WKEY = "halo.side.width", MIN = 200;
  const max = () => Math.max(MIN, Math.min(520, Math.round(window.innerWidth * 0.5)));
  const fit = (w) => Math.max(MIN, Math.min(max(), Math.round(w)));
  function width(w, keep) {
    if (w == null) root.style.removeProperty("--side-w");
    else root.style.setProperty("--side-w", fit(w) + "px");
    if (keep) try { if (w == null) localStorage.removeItem(WKEY); else localStorage.setItem(WKEY, String(fit(w))); } catch (e) { /* this page only */ }
  }
  let stored = null;
  try { stored = parseInt(localStorage.getItem(WKEY), 10) || null; } catch (e) { /* the look's width */ }
  if (stored) width(stored);
  window.addEventListener("resize", () => { if (stored) width(stored); });  // a narrower window keeps it inside its half
  function resizer() {
    const h = document.createElement("div");
    h.className = "side-resize"; h.tabIndex = 0;
    h.setAttribute("role", "separator"); h.setAttribute("aria-orientation", "vertical"); h.setAttribute("aria-label", "Sidebar width");
    const now = () => document.getElementById("side").getBoundingClientRect().width;
    h.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault(); h.setPointerCapture(e.pointerId); root.classList.add("side-resizing");
      const x0 = e.clientX, w0 = now();
      const move = (m) => width(w0 + m.clientX - x0);
      const up = () => {
        h.removeEventListener("pointermove", move); h.removeEventListener("pointerup", up); h.removeEventListener("pointercancel", up);
        root.classList.remove("side-resizing"); stored = fit(now()); width(stored, true);
      };
      h.addEventListener("pointermove", move); h.addEventListener("pointerup", up); h.addEventListener("pointercancel", up);
    });
    h.addEventListener("dblclick", () => { stored = null; width(null, true); });
    h.addEventListener("keydown", (e) => {  // the keyboard's way: arrows, 16 px a press
      const d = e.key === "ArrowLeft" ? -16 : e.key === "ArrowRight" ? 16 : 0;
      if (!d) return;
      e.preventDefault(); stored = fit(now() + d); width(stored, true);
    });
    document.body.appendChild(h);
  }
  document.addEventListener("DOMContentLoaded", () => {
    if (document.getElementById("side")) resizer();
    sync();
    const c = document.getElementById("side-collapse"), s = document.getElementById("side-show");
    if (c) c.addEventListener("click", () => set(true, true));
    if (s) s.addEventListener("click", () => set(false, true));
    document.addEventListener("keydown", (e) => {
      if (e.key === "\\" && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && wide()) {
        e.preventDefault();
        // the keyboard stays where it was (a box being typed in), unless it was in the part going away
        const folding = !root.classList.contains("side-collapsed"), a = document.activeElement;
        const going = document.getElementById(folding ? "side" : "side-show");
        set(folding, !!(a && going && going.contains(a)));
      }
    });
  });
})();
