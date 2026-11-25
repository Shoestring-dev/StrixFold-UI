// What is left of Agents in the page: the composers' typing heartbeat (no agent request for 30 s after a keystroke).
(() => {
  "use strict";
  const K = () => window.HaloStudioKit;
  let last = 0;
  function beat() {
    const k = K();
    if (!k || !k.unlocked()) return;
    const now = Date.now();
    if (now - last < 5000) return;  // the server keeps 30 s from the last one
    last = now;
    k.call("POST", "/studio/api/agents/typing", {}).catch(() => {});
  }
  const TYPED = ["t-prompt", "prompt"];  // Chat's and the Lab chat's composers
  document.addEventListener("input", (e) => {
    const t = e.target;
    if (t && TYPED.includes(t.id)) beat();
  }, true);
  window.HaloAgents = { beat };
})();
