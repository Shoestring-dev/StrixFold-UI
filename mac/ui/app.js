// StrixFold UI's dashboard: polls the local server (which proxies halod on box2) and renders it.
(() => {
  "use strict";
  const M = window.HaloMotion;
  const $ = (id) => document.getElementById(id);
  // the main pair's boxes, rank 0 first, as halod names them; the page's
  // box labels follow them
  let NAMES = ["box1", "box2"];
  const boxNames = (st) => {
    const e = Object.entries((st && st.boxes) || {}).sort((a, b) => (a[1].rank || 0) - (b[1].rank || 0)).map((x) => x[0]);
    if (e.length >= 2 && (e[0] !== NAMES[0] || e[1] !== NAMES[1])) {
      NAMES = e.slice(0, 2);
      const set = (id, t) => { const x = document.getElementById(id); if (x) x.textContent = t; };
      set("lb-h1", `${NAMES[0]} · rank 0`); set("lb-h2", `${NAMES[1]} · rank 1`);
      document.querySelectorAll('.tab[data-log="0"], .tab[data-log="1"]').forEach((t) => { const i = +t.dataset.log; t.textContent = `Rank ${i} · ${NAMES[i]}`; });
    }
    return NAMES;
  };
  const TOKEN = document.querySelector('meta[name="halo-ui-token"]').content;
  const STATE_WORD = { idle: "Stopped.", waiting: "Waiting for GPUs.", loading: "Loading.", linking: "Linking.", ready: "Ready.",
    stopping: "Stopping.", failed: "Failed.", unreachable: "No contact.", connecting: "Connecting." };
  const STEP_OF = { waiting: 0, loading: 1, linking: 2, ready: 3 };
  const S = { st: null, profiles: [], models: [], model: null, streamsMax: 1, streamsNote: "", metrics: null, requests: [], local: null,
    lastProfile: null, deploys: null, logTab: "events", reqSeen: new Set(), fails: 0, agents: null, agLog: null };
  const store = {  // per-viewer conveniences only; the page works without them
    get(k) { try { return localStorage.getItem("halo." + k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem("halo." + k, v); } catch (e) { /* private window */ } },
  };

  // ---------------------------------------------------------------- helpers
  async function api(method, path, body) {
    const opt = { method, headers: {}, cache: "no-store" };
    if (method !== "GET") {
      opt.headers["Content-Type"] = "application/json";
      opt.headers["X-Halo-UI-Token"] = TOKEN;
      opt.body = JSON.stringify(body || {});
    }
    // a read that hangs (halod frozen) gives up after 8 s instead of holding one of the browser's six connections
    const ctl = method === "GET" && window.AbortController ? new AbortController() : null;
    const to = ctl ? setTimeout(() => ctl.abort(), 5000) : 0;
    if (ctl) opt.signal = ctl.signal;
    let r;
    try { r = await fetch(path, opt); } finally { clearTimeout(to); }
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) {
      const err = new Error((data && (data.error || (data.errors && Object.values(data.errors).join("; ")))) || `HTTP ${r.status}`);
      err.status = r.status; err.data = data;
      throw err;
    }
    return data;
  }
  const fmtN = (x, d = 0) => (x === null || x === undefined || isNaN(x)) ? "–" : Number(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  function dur(s) {
    if (s === null || s === undefined || isNaN(s)) return "–";
    s = Math.max(0, Math.round(s));
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60);
    return `${h} h ${m % 60} min`;
  }
  const clock = (t) => t ? new Date(t * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "–";
  const mmss = (s) => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  function toast(msg, bad) {
    const t = $("toast");
    t.textContent = msg; t.classList.toggle("bad", !!bad); t.hidden = false;
    clearTimeout(toast.h); toast.h = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3200);
  }
  const esc = (s) => String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function holderText(h) {
    const w = h.who || {};
    const since = h.since ? ` since ${clock(h.since)} (${dur(Date.now() / 1000 - h.since)})` : "";
    const label = w.owner && w.owner !== "StrixFold UI" && w.owner !== w.label ? `${w.owner}: ${w.label || "?"}` : (w.label || "?");
    return { label, kind: w.kind, since, mine: h.mine };
  }

  // the Mac's URL for what serves now: halod names the remote endpoint (DeepSeek :18080, Qwen on Gufo :18090)
  function endpointNow() {
    const st = S.st, L = S.local || {};
    const dflt = (L.endpoint && L.endpoint.base_url) || "http://127.0.0.1:18080/v1";
    if (!st || !st.endpoint || !["waiting", "loading", "linking", "ready"].includes(st.state)) return dflt;
    const m = /:(\d+)\//.exec(st.endpoint);
    return (m && L.endpoints && L.endpoints[m[1]]) || (L.fake ? st.endpoint : dflt);
  }
  const modelNow = () => (S.st && S.st.state === "ready" && S.st.served_model) || "dsv41";

  // Boxes: a failed model says why, with a way to start it again (the chip sent the user to a retired page); with no
  // contact the last readings go blank and the note says since when (they stayed up as if live)
  function boxesState(st) {
    const f = $("boxes-fail"), note = $("boxes-note");
    if (st) S.seenAt = Date.now();
    if (f) {
      const failed = st && st.state === "failed";
      f.hidden = !failed;
      // its last error, not the rank's whole log
      // halod's own words up to the log it quotes, then that log's last error if it has one (a log line showed
      // instead of the cause), cut at a word
      const raw = String(st && st.reason || "").replace(/\s+/g, " ").trim();
      let why = raw.split(/\b(?:info|error|warning):/)[0].replace(/\s*\([^)]*$/, "").replace(/[\s:;,(-]+$/, "").trim();  // (no dangling "(log")
      const errs = raw.match(/\berror:\s*(.*?)(?=\b(?:info|warning|error):|$)/g);
      const err = errs ? errs[errs.length - 1].replace(/^error:\s*/, "").trim() : "";
      if (err) why = why ? `${why}: ${err}` : err;
      why = (why || raw || "no reason given").replace(/[\s.…]+$/, "");
      const cut = why.length > 160;
      if (cut) why = why.slice(0, 160).replace(/\s+\S*$/, "");
      if (failed) $("boxes-fail-t").textContent = `${st.profile ? st.profile.name : "The model"} failed: ${why}${cut ? "…" : "."}`;
      $("boxes-restart").dataset.profile = failed && st.profile ? st.profile.id : "";
      $("boxes-restart").hidden = !(failed && st.profile);
    }
    if (!note) return;
    if (st) { note.textContent = "Read every 2 seconds. Power profile is shown, never changed."; return; }
    note.textContent = S.seenAt ? `No contact since ${new Date(S.seenAt).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}.` : "No contact yet.";
    document.querySelectorAll("#box-grid dd").forEach((d) => { d.textContent = "–"; });
    // the lock panel too: no holder and no running "since" while nothing is heard
    const tg = $("team-grid");
    if (tg) tg.innerHTML = NAMES.map((n) => `<div class="lock-col"><h3>${n} GPU lock</h3><p class="lock-who free">No contact</p><p class="lock-meta"></p></div>`).join("");
  }
  $("boxes-restart") && $("boxes-restart").addEventListener("click", async (e) => {
    const b = e.currentTarget, id = b.dataset.profile; if (!id) return;
    b.disabled = true;
    try { await api("POST", "/api/start", { profile: id, replace: false }); toast("Starting it again"); } catch (err) { toast(err.message, true); }
    b.disabled = false;
  });
  // ---------------------------------------------------------------- state
  function render() {
    const st = S.st;
    const state = st ? st.state : (S.fails > 2 ? "unreachable" : "connecting");
    const word = $("state-word");
    M.decode(word, STATE_WORD[state] || state);
    word.classList.toggle("bad", state === "failed" || state === "unreachable");
    $("state-reason").textContent = reasonText(st, state);
    renderTeamWho(st);
    boxesState(st);
    if (!st) { renderBox(state); return; }
    const p = st.profile;
    $("f-profile").textContent = p ? p.name : "–";
    $("f-model").textContent = p ? `${p.model}${p.quant ? " · " + p.quant : ""}` : "DeepSeek-V4.1-Flash-1017 · EXL3 2.9 bpw";
    const mm = S.models.find((m) => m.id === (p ? p.model_id : currentModel()));
    $("f-warn").hidden = !(p && mm && mm.warning);
    $("f-warn").textContent = mm && mm.warning ? mm.warning : "";
    const now = st.t;
    $("f-uptime").textContent = st.state === "ready" && st.ready ? dur(now - st.ready) : (st.started && ["waiting", "loading", "linking"].includes(st.state) ? `starting for ${dur(now - st.started)}` : "–");
    $("f-endpoint").textContent = endpointNow();
    const remote = !/^(127\.0\.0\.1|localhost)(:|$)/.test(location.host);
    $("f-endpoint").title = remote ? "Mac only: the model endpoint answers on the Mac, not on this device" : "";
    document.querySelectorAll(".f-endpoint .mac-only").forEach((x) => x.remove());
    if (remote) { const t = document.createElement("span"); t.className = "mac-only"; t.textContent = " Mac only"; $("f-endpoint").parentElement.appendChild(t); }
    if (st.state === "ready" && st.idle) {
      $("f-idle").textContent = st.idle.hold ? "off: keep running is on" : (st.idle.stop_at ? `in ${dur(st.idle.stop_at - now)} with no requests (${st.idle.idle_min} min setting)` : "never (0 min setting)");
    } else if (st.state === "ready") {
      $("f-idle").textContent = "none for this engine: stop it when you are done";
    } else $("f-idle").textContent = p && p.settings ? `${p.settings.idle_min ? p.settings.idle_min + " min with no requests" : "never"}` : "–";
    $("f-deploy").textContent = st.deploy === "external" ? "its own (started by halo-qwen)" : st.deploy ? st.deploy.slice(0, 12) : "–";
    // actions
    const prim = $("primary"), label = $("primary-label");
    prim.classList.toggle("stop", ["waiting", "loading", "linking", "ready"].includes(st.state));
    prim.disabled = st.state === "stopping";
    const startId = startDefault();
    const startProf = S.profiles.find((x) => x.id === startId);
    label.textContent = st.state === "ready" ? "Stop" : st.state === "stopping" ? "Stopping…" :
      ["waiting", "loading", "linking"].includes(st.state) ? "Cancel the start" : `Start ${startProf ? startProf.name : "Agent coding"}`;
    $("restart").hidden = !(st.state === "ready" || (st.state === "failed" && st.profile));
    $("hold-wrap").hidden = st.state !== "ready" || !st.idle;
    $("hold").checked = !!st.hold;
    renderSequence(st);
    renderBox(state);
    renderBoxes(st);
    renderTeam(st);
    renderConnect();
  }

  // the profile the big Start button starts: the last one the user started here (if it still exists), else Agent coding
  function startDefault() {
    const last = S.lastProfile || store.get("lastProfile");
    const p = last && S.profiles.find((x) => x.id === last && x.installed);
    return p ? p.id : "agent-coding";
  }
  // the one place that says whether the team is using the GPUs (hero, box line, header chip, start bar all ask
  // this): a lock someone else holds, or a GPU busy without StrixFold UI. Who: names and since, for the details.
  function teamBusy(st) {
    const who = [];
    let any = false;
    for (const [name, b] of Object.entries((st && st.boxes) || {})) {
      const h = ((b.lock || {}).holders || []).filter((x) => !x.mine)[0];
      const t = b.telemetry || {};
      const gpu = b.fresh && (t.gpu_busy_pct || 0) > 20;
      if (h) { const x = holderText(h); who.push(`${name}: ${x.label}${x.since}`); any = true; }
      else if (gpu && !(st && ["waiting", "loading", "linking", "ready", "stopping"].includes(st.state))) { who.push(`${name}: GPU ${fmtN(t.gpu_busy_pct)}% busy, no lock holder seen`); any = true; }
      const ws = ((b.lock || {}).waiters || []).filter((w) => !w.mine);
      if (ws.length) who.push(`${name}: ${ws.length} waiting (${ws.map((w) => holderText(w).label).join(", ")})`);
    }
    return { busy: any, who };
  }
  window.HaloTeamBusy = teamBusy;
  function renderTeamWho(st) {
    const tb = teamBusy(st);
    const show = tb.busy && st && st.state !== "ready";
    $("team-who").hidden = !show;
    if (show) $("team-who-list").textContent = tb.who.join("\n");
  }

  function reasonText(st, state) {
    if (!st) return state === "unreachable" ? `The dashboard cannot reach halod on ${NAMES[1]}. The tunnels reconnect by themselves; the boxes keep running.` : "";
    if (st.state === "waiting") {
      const parts = [];
      for (const [name, b] of Object.entries(st.boxes)) {
        const hs = ((b.lock || {}).holders || []).filter((h) => !h.mine);
        if (hs.length) { const h = holderText(hs[0]); parts.push(`${name} is in use by ${h.label}${h.since}`); }
        const ws = ((b.lock || {}).waiters || []).filter((w) => !w.mine);
        if (ws.length) parts.push(`${ws.length} ahead in ${name}'s queue`);
      }
      return parts.length ? "The GPUs are busy with other work. StrixFold UI waits its turn in the lock queue." : (st.reason || "");
    }
    if (st.state === "idle" || st.state === "failed") {
      if (teamBusy(st).busy) return "Nothing of StrixFold UI's is running; the GPUs are busy with other work. A start waits in line for both locks." +
        (st.state === "failed" && st.reason ? ` Last run: ${st.reason}.` : "");
      if (st.state === "failed") return st.reason || "";
      return st.reason && !/^stopped from the dashboard$/.test(st.reason) ? `Nothing is running; the GPUs are free. Last run: ${st.reason}.` : "Nothing is running. The GPUs are free.";
    }
    return st.reason || "";
  }

  // the captions of the start sequence: DeepSeek's two ranks, or an engine started by its own command (halo-qwen)
  const SEQ_EXT = { waiting: "halo-qwen's node waits for box1's GPU lock; box2 stays free.", loading: "Gufo loads Qwen3.8 on box1.",
    linking: "One box: nothing to link.", ready: "A first request must answer correctly before agents get the endpoint." };
  const SEQ_DEFAULT = {};
  document.querySelectorAll(".seq-step").forEach((el) => { SEQ_DEFAULT[el.dataset.step] = el.querySelector(".seq-d").textContent; });
  function renderSequence(st) {
    const ext = st.deploy === "external" || !!(st.profile && st.profile.fixed);
    document.querySelectorAll(".seq-step").forEach((el) => {
      const t = (ext ? SEQ_EXT : SEQ_DEFAULT)[el.dataset.step];
      if (el.querySelector(".seq-d").textContent !== t) el.querySelector(".seq-d").textContent = t;
    });
    const steps = st.steps || {};
    const order = ["waiting", "loading", "linking", "ready"];
    const items = document.querySelectorAll(".seq-step");
    const idx = STEP_OF[st.state] !== undefined ? STEP_OF[st.state] : -1;
    order.forEach((k, i) => {
      const t0 = steps[k], t1 = steps[order[i + 1]] || (st.state === k ? st.t : null);
      const el = items[i].querySelector(".seq-t");
      if (k === "ready") el.textContent = steps.ready ? `at ${clock(steps.ready)} · first request ok` : "–";
      else el.textContent = t0 && t1 ? mmss(t1 - t0) + (st.state === k ? " so far" : "") : "–";
    });
    let shown = idx, bad = false, live = idx >= 0 && idx < 3;
    if (st.state === "failed" || st.state === "stopping" || st.state === "idle") {
      // show where the last run got to
      shown = steps.ready ? 3 : steps.linking ? 2 : steps.loading ? 1 : steps.waiting ? 0 : -1;
      bad = st.state === "failed"; live = false;
    }
    M.setSequence(shown, { live, bad });
    $("seq-count").textContent = shown >= 0 ? `Step ${shown + 1} of 4${st.state === "ready" ? "" : st.state === "failed" ? " · failed" : ["idle", "stopping"].includes(st.state) ? " · last run" : ""}` : "";
  }

  function renderBox(state) {
    const st = S.st;
    const held = [false, false], down = [false, false];
    let rate = 0, failRank = null;
    if (st) {
      boxNames(st).forEach((n, i) => {
        const b = st.boxes[n] || {};
        held[i] = ((b.lock || {}).holders || []).some((h) => !h.mine);
        const links = ((b.telemetry || {}).usb4 || {}).links || [];
        links.forEach((l, c) => { if (!(l.up && l.carrier)) down[c] = true; });
      });
      const live = st.live || {};
      if (live.connections) rate = (live.decode_tps || 0) + (live.prefill_tps || 0) / 40;
      const m = /rank (\d)/.exec(st.reason || "");
      if (st.state === "failed" && m) failRank = Number(m[1]);
    }
    const mode = { connecting: "idle", unreachable: "failed" }[state] || state;
    M.setBox({ mode, held, down, rate, failRank });
    // the caption under the box
    let cap = "", hot = true;
    if (!st) { cap = state === "unreachable" ? "No contact with halod." : "Connecting to halod…"; }
    else if (down.some(Boolean)) cap = `USB4 cable ${down[0] ? "A (thunderbolt0)" : "B (thunderbolt1)"} is down.`;
    else if (state === "ready") {
      const live = st.live || {};
      cap = live.connections ? (live.decode_tps ? `Generating at ${fmtN(live.decode_tps, 1)} tok/s.` : `Reading a prompt at ${fmtN(live.prefill_tps)} tok/s.`) : "Serving. No request running.";
      hot = !!live.connections;
    } else if (state === "waiting") cap = held.some(Boolean) ? `Lock held by someone else on ${held.map((h, i) => h ? NAMES[i] : null).filter(Boolean).join(" and ")}.` : "Taking the locks.";
    else if (state === "loading") cap = "Each box reads its 95 GiB rank cache.";
    else if (state === "linking") cap = "The ranks link over both USB4 cables.";
    else if (state === "stopping") cap = "Stopping both ranks, then releasing both locks.";
    else if (state === "failed") cap = "Both ranks stopped and both locks released.";
    else {
      // halod is not serving, but the boxes may be busy with someone else's run
      const busy = teamBusy(st).busy;
      const pct = boxNames(st).map((n) => (st.boxes[n] || {}).telemetry || {}).map((t) => fmtN(t.gpu_busy_pct)).join(" / ");
      cap = busy ? `Busy with other work (GPU ${pct}% busy).` : "Both boxes idle. Two USB4 cables, 20 Gb/s x2 each.";
      hot = busy;
    }
    $("cap-text").textContent = cap;
    $("cap-dot").className = "dot" + (hot ? "" : " off");
  }

  // ---------------------------------------------------------------- boxes and team
  const TROWS = [
    ["Package power", (t) => t.ppt_w, (v) => `${fmtN(v)} W`, "ppt_w"],
    ["GPU temperature", (t) => t.gpu_temp_c, (v) => `${fmtN(v)} °C`, "gpu_temp_c"],
    ["GPU clock", (t) => t.sclk_mhz, (v) => `${fmtN(v)} MHz`, "sclk_mhz"],
    ["GPU busy", (t) => t.gpu_busy_pct, (v) => `${fmtN(v)} %`, "gpu_busy_pct"],
    ["Memory free", (t) => t.mem_available_gib, (v) => `${fmtN(v, 1)} GiB`, "mem_available_gib"],
    ["GPU memory (GTT)", (t) => t.gtt_used_gib, (v) => `${fmtN(v, 1)} GiB`, "gtt_used_gib"],
  ];
  function renderBoxes(st) {
    const grid = $("box-grid");
    const series = (S.metrics && S.metrics.boxes) || {};
    if (!grid.dataset.built) {
      grid.innerHTML = boxNames(st).map((n, i) => `<div class="box-col" id="bc-${n}"><h3><span>${n}</span><small>rank ${i}${i ? "" : " · the server"}</small></h3><dl>` +
        TROWS.map((r, k) => `<div class="trow"><dt>${r[0]}</dt><canvas id="sp-${n}-${k}" aria-hidden="true"></canvas><dd id="tv-${n}-${k}">–</dd></div>`).join("") +
        `<div class="trow wide"><dt>USB4 link</dt><dd id="tv-${n}-link">–</dd></div><div class="trow wide"><dt>Power profile</dt><dd id="tv-${n}-pp">–</dd></div>` +
        `<div class="trow wide"><dt>Load</dt><dd id="tv-${n}-load">–</dd></div></dl></div>`).join("");
      grid.dataset.built = "1";
    }
    for (const n of boxNames(st)) {
      const b = st.boxes[n] || {}, t = b.telemetry || {};
      const stale = !b.fresh;
      TROWS.forEach((r, k) => {
        const v = r[1](t);
        $(`tv-${n}-${k}`).textContent = stale ? "–" : r[2](v);
        const pts = (series[n] || []).map((p) => p[1][r[3]]);
        M.sparkline($(`sp-${n}-${k}`), pts.slice(-150), r[3] === "mem_available_gib" ? { min: 0 } : {});
      });
      const u = t.usb4 || {};
      const links = (u.links || []).map((l, i) => `${i ? "B" : "A"} ${l.up && l.carrier ? "up" : "down"}`);
      const peer = (u.peers || [])[0];
      $(`tv-${n}-link`).innerHTML = stale ? "–" : `<span class="${(u.links || []).some((l) => !(l.up && l.carrier)) ? "bad-t" : ""}">${esc(links.join(", ") || "no cables")}</span>${peer ? ` · ${esc(peer.rx)} x${peer.rx_lanes} to ${esc(peer.name)}` : ""}`;
      $(`tv-${n}-pp`).textContent = stale ? "–" : `${t.power_profile || "unknown"}${t.gpu_clock_policy ? ", GPU clocks " + t.gpu_clock_policy : ""}` +
        (S.st && S.st.state === "ready" && S.st.settings && S.st.settings.power === "max" ? " (Max speed profile)" : "");
      $(`tv-${n}-load`).textContent = stale ? (b.error ? `no contact: ${b.error}` : "no contact") : (t.load || []).map((x) => x.toFixed(2)).join("  ");
    }
  }

  function renderTeam(st) {
    const grid = $("team-grid");
    grid.innerHTML = boxNames(st).map((n) => {
      const b = st.boxes[n] || {}, lk = b.lock || {}, q = b.quiet || {};
      const hs = lk.holders || [], ws = lk.waiters || [];
      let who, meta = "";
      if (!b.fresh) { who = `<p class="lock-who free">No contact</p>`; }
      else if (!hs.length) { who = `<p class="lock-who free">Free</p>`; }
      else {
        const h = holderText(hs[0]);
        who = `<p class="lock-who">${h.mine ? "StrixFold UI (this run)" : esc(h.label)}</p>`;
        meta = `${h.mine ? "" : esc(h.kind === "run" ? "a served run" : h.kind === "halo-control" ? "an earlier StrixFold UI run" : "a GPU job")}${esc(h.since)}`;
      }
      const queue = ws.length ? `<ul class="queue">${ws.map((w, i) => { const x = holderText(w); return `<li><span>${i + 1}. ${x.mine ? "StrixFold UI" : esc(x.label)}</span><span>waiting${w.since ? " " + dur(Date.now() / 1000 - w.since) : ""}</span></li>`; }).join("")}</ul>` : "";
      const quiet = (q.requests || []).length ? `<p class="lock-meta">Quiet window open: ${(q.requests || []).map((r) => esc(r.tag)).join(", ")} (background jobs paused)</p>` : "";
      return `<div class="lock-col"><h3>${n} GPU lock</h3>${who}<p class="lock-meta">${meta}</p>${queue}${quiet}</div>`;
    }).join("");
  }

  // ---------------------------------------------------------------- metrics and requests
  function renderMetrics() {
    const m = S.metrics;
    if (!m) return;
    M.flap($("m-decode"), fmtN(m.decode_tps, 1));
    M.flap($("m-prefill"), fmtN(m.prefill_tps));
    M.flap($("m-ttft"), fmtN(m.ttft_s, 2));
    M.flap($("m-cache"), m.cache_hit === null ? "–" : fmtN(m.cache_hit * 100));
    M.flap($("m-accept"), m.acceptance === null ? "–" : fmtN(m.acceptance * 100));
    const live = (S.st && S.st.live) || {};
    const busy = S.st && S.st.state === "ready" && live.connections;
    $("m-decode-live").textContent = busy && live.decode_tps ? `now ${fmtN(live.decode_tps, 1)}` : "";
    $("m-decode-live").className = "m-sub" + (busy ? " live" : "");
    $("m-prefill-live").textContent = busy && live.prefill_tps ? `now ${fmtN(live.prefill_tps)}` : "";
    $("m-prefill-live").className = "m-sub" + (busy ? " live" : "");
    const extRun = S.st && S.st.deploy === "external" && ["waiting", "loading", "linking", "ready"].includes(S.st.state);
    const serving = S.st && S.st.state === "ready" && !extRun;
    const lastT = (m.requests || []).length ? m.requests[m.requests.length - 1].t : null;
    $("metrics").classList.toggle("stale", !serving);
    $("metrics-note").textContent = extRun ? "Qwen3.8 on Gufo reports no per-request metrics to StrixFold UI; these are DeepSeek's last requests." :
      !m.recent_n ? "No requests yet." :
      serving ? `Medians of the last ${m.recent_n} request${m.recent_n === 1 ? "" : "s"}.` :
      `Not serving. These are from the last run: medians of its last ${m.recent_n} requests, the latest at ${clock(lastT)}.`;
    M.requestChart($("spark"), m.requests || []);
  }

  function renderRequests() {
    const body = $("req-body");
    const rows = S.requests;
    if (!rows.length) { body.innerHTML = `<tr><td colspan="9" class="empty">No requests yet.</td></tr>`; return; }
    body.innerHTML = rows.map((r) => {
      const isNew = S.reqSeen.size && !S.reqSeen.has(r.id);
      return `<tr class="${isNew ? "new" : ""}"><td>${new Date(r.t * 1000).toLocaleTimeString("en-GB")}</td><td>${fmtN(r.prompt)}</td><td>${fmtN(r.cached)}</td><td>${fmtN(r.tokens)}</td>` +
        `<td>${r.ttft_s === null ? "–" : fmtN(r.ttft_s, 2) + " s"}</td><td>${fmtN(r.prefill_tps)}</td><td>${fmtN(r.decode_tps, 1)}</td>` +
        `<td>${r.acceptance === null ? "–" : fmtN(r.acceptance * 100) + " %"}</td><td>${esc(r.finish)}${r.approx ? " *" : ""}</td></tr>`;
    }).join("");
    rows.forEach((r) => S.reqSeen.add(r.id));
  }

  // ---------------------------------------------------------------- profiles
  function chips(s, p) {
    const advs = (p && p.advanced) || {};
    const split = advs.PREFILL_SPLIT_LARGE === "1" && s.context > 16384;
    const EXP = { L2_PREFETCH: "decode boost (experimental)", TF_HC_FUSE: "fused hc chain (experimental)" };  // switches on
    const adv = Object.keys(advs).filter((k) => !(split && k === "PREFILL_SPLIT_LARGE") && !(k in EXP && advs[k] === "1"));
    return [`${fmtN(s.context)} ctx`, `reply cap ${fmtN(s.max_tokens)}`, s.drafts ? "drafts on" : "drafts off", s.thinking ? "thinking on" : "thinking off",
      s.prompt_cache_gib ? `cache ${s.prompt_cache_gib} GiB` : "no cache", (s.streams || 1) > 1 ? `${s.streams} parallel` : "1 at a time",
      s.idle_min ? `idle stop ${s.idle_min} min` : "no idle stop", `deploy ${s.deploy === "conf" ? "of its conf" : s.deploy}`]
      .concat(split ? ["prefill split"] : [])
      .concat(Object.keys(EXP).filter((k) => advs[k] === "1").map((k) => EXP[k]))
      .concat(s.power === "max" ? ["Max speed"] : [])
      .concat(adv.length ? [`${adv.length} advanced: ${adv.slice(0, 3).join(", ")}${adv.length > 3 ? "…" : ""}`] : [])
      .concat(p && p.expert ? ["expert"] : []).map((x) => `<span>${esc(x)}</span>`).join(" ");
  }
  function currentModel() {
    const st = S.st;
    if (S.model) return S.model;
    if (st && st.profile && ["waiting", "loading", "linking", "ready"].includes(st.state)) return st.profile.model_id;
    return store.get("model") || "dsv41";
  }
  function renderModels() {
    const st = S.st, cur = currentModel();
    const runModel = st && st.profile && ["waiting", "loading", "linking", "ready"].includes(st.state) ? st.profile.model_id : null;
    $("model-list").innerHTML = S.models.map((m) => {
      const sel = m.id === cur;
      const state = m.installed === true ? (runModel === m.id ? `<span class="m-state on"><i class="dot"></i>${esc(st.state)}</span>` : `<span class="m-state">Installed</span>`)
        : m.installed === false ? `<span class="m-state off">${esc(m.missing ? "Pending" : "Not startable")}</span>` : `<span class="m-state">Checking…</span>`;
      const why = m.installed === false ? `<p class="m-why">${esc(m.missing || m.why || "")}</p>` : "";
      return `<li><button type="button" role="radio" aria-checked="${sel}" class="model${sel ? " sel" : ""}${m.installed === false ? " absent" : ""}" data-model="${esc(m.id)}">` +
        `<span class="m-head"><span class="m-name">${esc(m.name)}</span>${state}</span>` +
        `<span class="m-variant">${esc(m.variant || "")}${m.quant ? " · " + esc(m.quant) : ""}</span>` +
        `<span class="m-about">${esc(m.about || "")}</span>` +
        (m.warning ? `<span class="m-warn">${esc(m.warning)}</span>` : "") +
        ((m.facts || []).length ? `<span class="m-facts">${m.facts.map((f) => `<span>${esc(f)}</span>`).join("")}</span>` : "") + why + `</button></li>`;
    }).join("");
  }
  $("model-list").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-model]");
    if (!b) return;
    S.model = b.dataset.model; store.set("model", S.model);
    renderModels(); renderProfiles();
  });
  $("model-list").addEventListener("keydown", (e) => {
    if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(e.key)) return;
    const bs = [...$("model-list").querySelectorAll("button[data-model]")];
    const i = bs.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    const n = bs[(i + (e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : bs.length - 1)) % bs.length];
    n.focus(); n.click();
  });

  function renderProfiles() {
    const st = S.st;
    const runId = st && ["waiting", "loading", "linking", "ready"].includes(st.state) && st.profile ? st.profile.id : null;
    const mid = currentModel();
    const shown = S.profiles.filter((p) => p.model_id === mid || p.id === runId);
    const mm = S.models.find((m) => m.id === mid);
    $("profiles-n").textContent = shown.length;
    $("profiles-h").firstChild.textContent = mm ? `Profiles for ${mm.name}` : "Profiles";
    renderAgentProfiles();
    $("prof-list").innerHTML = shown.map((p) => {
      const running = p.id === runId;
      const absent = !p.installed;
      const go = absent ? `<button type="button" class="go" disabled>Not installed</button>` :
        running ? `<button type="button" class="go stop" data-act="stop">${st.state === "ready" ? "Stop" : "Cancel"}</button>` :
        `<button type="button" class="go" data-act="start" data-id="${esc(p.id)}">Start</button>`;
      const small = absent || p.fixed ? "" : `<div class="p-small"><button type="button" class="textbtn" data-act="dup" data-id="${esc(p.id)}">Duplicate</button>${p.builtin ? "" : `<button type="button" class="textbtn" data-act="edit" data-id="${esc(p.id)}">Edit</button>`}</div>`;
      return `<li class="${running ? "running" : ""} ${absent ? "absent" : ""}" data-id="${esc(p.id)}">` +
        `<h3 class="p-name">${running ? '<i class="dot" aria-hidden="true"></i>' : ""}${esc(p.name)}${running ? ` <span class="p-tag">${esc(st.state)}</span>` : ""}${p.builtin ? "" : ' <span class="p-tag muted">custom</span>'}</h3>` +
        `<p class="p-use">${esc(p.use_for)}</p>` +
        (absent ? `<p class="p-missing">${esc(p.missing || "Not installed yet.")}</p>` : `<p class="p-meta">${p.chips ? p.chips.map((x) => `<span>${esc(x)}</span>`).join(" ") : chips(p.settings, p)}</p><p class="p-trade">${esc(p.tradeoffs || "")}${p.limits && !p.fixed ? ` Prompts up to ${fmtN(p.limits.max_prompt_default)} tokens with the default reply cap.` : ""}</p>`) +
        `<div class="p-act">${go}${small}</div></li>`;
    }).join("");
  }

  $("prof-list").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const act = b.dataset.act, id = b.dataset.id;
    if (act === "start") return startProfile(id);
    if (act === "stop") return stopRun();
    if (act === "dup") return openEditor(id, false);
    if (act === "edit") return openEditor(id, true);
  });

  async function startProfile(id, opts) {
    const st = S.st, p = S.profiles.find((x) => x.id === id);
    const active = st && ["waiting", "loading", "linking", "ready"].includes(st.state) && st.profile;
    let replace = false;
    if (active && st.profile.id !== id) {
      if (!(opts && opts.confirmed) && !confirm(`${st.profile.name} is ${st.state}. Stop it and start ${p ? p.name : id}?`)) return false;
      replace = true;
    }
    try {
      const r = await api("POST", "/api/start", { profile: id, replace });
      S.lastProfile = id; store.set("lastProfile", id);
      toast(r.note ? `${p ? p.name : id}: ${r.note}` : `Starting ${p ? p.name : id}`);
      poll();
      return true;
    } catch (err) { toast(err.message, true); return false; }
  }
  async function stopRun() {
    const st = S.st;
    if (st && st.state === "ready" && st.live && st.live.connections && !confirm("A request is running. Stop anyway?")) return;
    try { await api("POST", "/api/stop", { reason: "stopped from the dashboard" }); toast("Stopping"); poll(); }
    catch (err) { toast(err.message, true); }
  }

  $("primary").addEventListener("click", () => {
    const st = S.st;
    if (!st) return;
    if (["waiting", "loading", "linking", "ready"].includes(st.state)) return stopRun();
    startProfile(startDefault());
  });
  $("restart").addEventListener("click", async () => {
    if (!confirm("Restart the model? Both ranks stop and load again (about 30 s plus any wait for the GPUs).")) return;
    try { await api("POST", "/api/restart", {}); toast("Restarting"); poll(); } catch (err) { toast(err.message, true); }
  });
  $("hold").addEventListener("change", async (e) => {
    try { await api("POST", "/api/hold", { on: e.target.checked }); poll(); }
    catch (err) { toast(err.message, true); e.target.checked = !e.target.checked; }
  });

  // ---------------------------------------------------------------- the profile editor
  const ED = { base: null, editing: null, timer: 0, deploys: [], knobs: [], expert: false };
  const CONTEXTS = []; for (let c = 4096; c <= 63488; c += 2048) CONTEXTS.push(c);
  const GROUPS = [["experimental", "Experimental"], ["engine", "The engine"], ["server", "The server"], ["transport", "Transport (expert)"], ["lock", "GPU lock (expert)"]];
  async function openEditor(id, editing) {
    const p = S.profiles.find((x) => x.id === id);
    if (!p) return;
    ED.base = p; ED.editing = editing ? p : null; ED.expert = !!p.expert;
    try { const d = await api("GET", `/api/deploys?model=${encodeURIComponent(p.model_id)}`); ED.deploys = Object.keys(d.deploys); }
    catch (err) { ED.deploys = [p.settings.deploy]; }
    if (p.base_conf && !ED.deploys.includes("conf")) ED.deploys.unshift("conf");
    const mm = S.models.find((m) => m.id === p.model_id);
    $("ed-h").textContent = editing ? `Edit ${p.name}` : `New profile from ${p.name}`;
    $("ed-base").textContent = `${mm ? mm.name : p.model}${p.quant ? " · " + p.quant : ""}. Engine: ${p.engine_name || p.engine}.`;
    $("ed-name").value = editing ? p.name : `${p.name} (copy)`;
    $("ed-context").innerHTML = CONTEXTS.map((c) => `<option value="${c}">${fmtN(c)}</option>`).join("");
    $("ed-context").value = String(p.settings.context);
    $("ed-max").value = p.settings.max_tokens;
    $("ed-cache").value = p.settings.prompt_cache_gib;
    $("ed-idle").value = p.settings.idle_min;
    $("ed-deploy").innerHTML = ED.deploys.map((d) => `<option value="${esc(d)}">${d === "conf" ? "the one its conf names" : esc(d)}</option>`).join("");
    $("ed-deploy").value = p.settings.deploy;
    $("ed-streams").innerHTML = Array.from({ length: S.streamsMax }, (_, k) => `<option value="${k + 1}">${k + 1}${k ? " at once" : " at a time"}</option>`).join("");
    $("ed-streams").value = String(p.settings.streams || 1);
    $("ed-streams-note").textContent = S.streamsNote;
    $("ed-drafts").checked = !!p.settings.drafts;
    $("ed-power").value = p.settings.power || "default";
    $("ed-power-note").textContent = S.powerNote || "";
    $("ed-thinking").checked = !!p.settings.thinking;
    $("ed-expert").checked = ED.expert;
    $("ed-delete").hidden = !editing;
    $("ed-general").textContent = "";
    $("adv-body").innerHTML = `<p class="adv-intro">Reading the conf…</p>`;
    $("editor").showModal();
    await loadKnobs();
    validate();
  }
  async function loadKnobs() {
    const p = ED.base;
    try {
      const d = await api("GET", `/api/conf?profile=${encodeURIComponent(p.id)}&deploy=${encodeURIComponent($("ed-deploy").value)}`);
      $("adv-path").textContent = d.path;
      const prev = ED.knobs.length ? advValues() : (p.advanced || {});
      ED.knobs = d.knobs;
      renderKnobs(prev);
    } catch (err) { $("adv-body").innerHTML = `<p class="err">${esc(err.message)}</p>`; ED.knobs = []; }
  }
  function renderKnobs(adv) {
    const exp = $("ed-expert").checked;
    const editable = (k) => k.level === "safe" || (k.level === "expert" && exp);
    const row = (k) => {
      const val = adv[k.key] !== undefined ? adv[k.key] : k.base;
      if (k.label && editable(k)) {  // a named on/off switch (0/1), off unless the profile turns it on
        const on = val === "1", changed = adv[k.key] !== undefined && adv[k.key] !== (k.base || "0");
        return `<div class="knob knob-switch${changed ? " changed" : ""}" data-key="${esc(k.key)}"><label for="kn-${esc(k.key)}">` +
          `<input type="checkbox" id="kn-${esc(k.key)}" data-base="${esc(k.base || "0")}"${on ? " checked" : ""} aria-describedby="kh-${esc(k.key)}"> ` +
          `<span>${esc(k.label)}</span> <code>${esc(k.key)}</code>${changed ? ' <span class="p-tag">changed</span>' : ""}</label>` +
          `<p class="knob-help" id="kh-${esc(k.key)}">${esc(k.help || "")}${k.tested ? ` <span class="knob-tested">${esc(k.tested)}</span>` : ""}</p>` +
          `<em class="err" data-err="adv:${esc(k.key)}"></em></div>`;
      }
      const changed = adv[k.key] !== undefined && adv[k.key] !== k.base;
      return `<div class="knob${changed ? " changed" : ""}" data-key="${esc(k.key)}"><label for="kn-${esc(k.key)}"><code>${esc(k.key)}</code>${changed ? ' <span class="p-tag">changed</span>' : ""}</label>` +
        `<input id="kn-${esc(k.key)}" data-base="${esc(k.base)}" value="${esc(val)}" spellcheck="false" autocomplete="off" aria-describedby="kh-${esc(k.key)}">` +
        `<p class="knob-help" id="kh-${esc(k.key)}">${esc(k.help || "")}${k.tested ? ` <span class="knob-tested">${esc(k.tested)}</span>` : ""}${k.hint ? ` <span class="knob-hint">${esc(k.hint)}. Conf: ${k.base === "" ? "empty" : esc(k.base)}.</span>` : ""}</p>` +
        `<em class="err" data-err="adv:${esc(k.key)}"></em></div>`;
    };
    let html = "";
    for (const [g, title] of GROUPS) {
      const ks = ED.knobs.filter((k) => k.group === g && (k.level === "safe" || k.level === "expert"));
      if (!ks.length) continue;
      const on = ks.filter(editable), off = ks.filter((k) => !editable(k));
      html += `<h3 class="adv-h">${esc(title)}</h3>` + on.map(row).join("") +
        (off.length ? `<p class="adv-locked">${off.map((k) => `<code>${esc(k.key)}</code> ${esc(k.base) || "empty"}`).join(" · ")} <span>(the conf's; turn on Expert to change)</span></p>` : "");
    }
    const fixed = ED.knobs.filter((k) => k.level !== "safe" && k.level !== "expert");
    html += `<h3 class="adv-h">Set elsewhere or never changed</h3><dl class="adv-fixed">` + fixed.map((k) =>
      `<div><dt><code>${esc(k.key)}</code></dt><dd><span class="adv-v">${esc(k.value) || "empty"}</span> <span class="adv-why">${esc(k.note || "")}</span></dd></div>`).join("") + "</dl>";
    $("adv-body").innerHTML = html;
    $("adv-count").textContent = `(${Object.keys(adv).length} changed)`;
  }
  function advValues() {
    const out = {};
    document.querySelectorAll("#adv-body .knob input").forEach((i) => {
      const k = i.closest(".knob").dataset.key, v = i.type === "checkbox" ? (i.checked ? "1" : "0") : i.value.trim();
      if (v !== i.dataset.base) out[k] = v;
    });
    return out;  // with Expert off, transport and lock settings go back to the conf's
  }
  function edSettings() {
    return { context: Number($("ed-context").value), max_tokens: $("ed-max").value.trim(), prompt_cache_gib: $("ed-cache").value.trim(),
      idle_min: $("ed-idle").value.trim(), deploy: $("ed-deploy").value, drafts: $("ed-drafts").checked, thinking: $("ed-thinking").checked,
      streams: Number($("ed-streams").value), power: $("ed-power").value };
  }
  async function validate() {
    const s = edSettings(), adv = advValues();
    document.querySelectorAll("#ed-form .err[data-err]").forEach((e) => { e.textContent = ""; });
    document.querySelectorAll("#ed-form [aria-invalid]").forEach((e) => e.removeAttribute("aria-invalid"));
    document.querySelectorAll("#adv-body .knob").forEach((k) => k.classList.toggle("changed", k.dataset.key in adv));
    $("adv-count").textContent = `(${Object.keys(adv).length} changed)`;
    $("ed-general").textContent = "";
    try {
      const r = await api("POST", "/api/profiles/validate", { settings: s, deploys: ED.deploys, advanced: adv, expert: $("ed-expert").checked, base: ED.base.id });
      if (r.ok) {
        $("ed-limits").textContent = `Prompts up to ${fmtN(r.limits.max_prompt_default)} tokens when a request leaves max_tokens out. ` +
          (r.limits.streams > 1 ? `${r.limits.streams} requests share the ${fmtN(s.context)}-token pool: each takes its prompt plus max_tokens while it runs. ` : "") +
          `Needs ${r.limits.mem_needed_gib} GiB free on each box to start.`;
        $("ed-save").disabled = false;
      } else {
        for (const [k, v] of Object.entries(r.errors)) {
          const slot = document.querySelector(`#ed-form .err[data-err="${CSS.escape(k)}"]`);
          if (slot) { slot.textContent = v; const input = slot.parentElement.querySelector("input, select"); if (input) input.setAttribute("aria-invalid", "true"); }
          else $("ed-general").textContent += `${k.replace(/^adv:/, "")}: ${v} `;
        }
        $("ed-limits").textContent = "";
        $("ed-save").disabled = true;
      }
    } catch (err) { $("ed-general").textContent = err.message; }
  }
  $("ed-form").addEventListener("input", () => { clearTimeout(ED.timer); ED.timer = setTimeout(validate, 220); });
  $("ed-form").addEventListener("change", (e) => {
    if (e.target.id === "ed-expert") { ED.expert = e.target.checked; renderKnobs(advValues()); }
    if (e.target.id === "ed-deploy") loadKnobs();
    clearTimeout(ED.timer); ED.timer = setTimeout(validate, 50);
  });
  $("ed-cancel").addEventListener("click", () => $("editor").close());
  $("ed-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const s = edSettings(), name = $("ed-name").value.trim(), body = { name, settings: s, advanced: advValues(), expert: $("ed-expert").checked };
    try {
      let r;
      if (ED.editing) r = await api("PUT", `/api/profiles/${encodeURIComponent(ED.editing.id)}`, body);
      else r = await api("POST", "/api/profiles", Object.assign(body, { base: ED.base.id }));
      $("editor").close();
      toast(r.running ? `Saved ${name}: the change applies at its next start` : `Saved ${name}`);
      if (r.profile) { S.model = r.profile.model_id; store.set("model", S.model); }
      loadProfiles();
    } catch (err) {
      if (err.data && err.data.errors) {
        for (const [k, v] of Object.entries(err.data.errors)) {
          const slot = document.querySelector(`#ed-form .err[data-err="${CSS.escape(k)}"]`) || document.querySelector(`#ed-form .err[data-err="adv:${CSS.escape(k)}"]`);
          if (slot) slot.textContent = v; else $("ed-general").textContent += `${k === "name" ? "Name" : k}: ${v} `;
        }
        if ($("ed-general").textContent === "" && !document.querySelector("#ed-form .err[data-err]:not(:empty)")) $("ed-general").textContent = err.message;
      } else $("ed-general").textContent = err.message;
    }
  });
  $("ed-delete").addEventListener("click", async () => {
    if (!ED.editing || !confirm(`Delete ${ED.editing.name}?`)) return;
    try { await api("DELETE", `/api/profiles/${encodeURIComponent(ED.editing.id)}`, {}); $("editor").close(); toast("Deleted"); loadProfiles(); }
    catch (err) { $("ed-general").textContent = err.message; }
  });
  $("editor").addEventListener("close", () => { ED.knobs = []; });

  // export and import of custom profiles
  $("prof-export").addEventListener("click", async () => {
    try {
      const d = await api("GET", "/api/profiles/export");
      if (!d.profiles.length) { toast("No custom profiles to export yet"); return; }
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 1)], { type: "application/json" }));
      a.download = `halo-control-profiles-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      toast(`Exported ${d.profiles.length} profile${d.profiles.length === 1 ? "" : "s"}`);
    } catch (err) { toast(err.message, true); }
  });
  $("prof-import").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      const r = await api("POST", "/api/profiles/import", { data });
      const errs = Object.entries(r.errors || {});
      toast(`Imported ${r.created.length}` + (errs.length ? `; refused ${errs.map(([n, m]) => `${n} (${m})`).join("; ")}` : ""), errs.length > 0);
      loadProfiles();
    } catch (err) { toast(err instanceof SyntaxError ? "That file is not JSON" : err.message, true); }
  });

  // ---------------------------------------------------------------- agents
  const AG_STATE = { queued: "Queued", running: "Running", done: "Done", failed: "Failed", stopped: "Stopped", interrupted: "Interrupted" };
  function renderAgentProfiles() {
    const sel = $("ag-profile"), cur = sel.value || store.get("agProfile") || "any";
    const ok = S.profiles.filter((p) => p.installed);
    sel.innerHTML = `<option value="any">Whatever is serving</option>` + S.models.filter((m) => ok.some((p) => p.model_id === m.id)).map((m) =>
      `<optgroup label="${esc(m.name)}">${ok.filter((p) => p.model_id === m.id).map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}</optgroup>`).join("");
    sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "any";
  }
  function agentAdapter() { return document.querySelector('input[name="ag-adapter"]:checked').value; }
  function agShowAdapter() {
    const cmd = agentAdapter() === "command";
    document.querySelectorAll(".ag-cmd").forEach((e) => { e.hidden = !cmd; });
    document.querySelectorAll(".ag-bi").forEach((e) => { e.hidden = cmd; });
    // a command agent may have tools that write; say so when the profile's model skips confirmations
    const pid = $("ag-profile").value, st = S.st;
    const prof = pid === "any" ? (st && st.profile) : S.profiles.find((x) => x.id === pid);
    const mm = prof && S.models.find((m) => m.id === prof.model_id);
    $("ag-startprof").closest(".check-s").hidden = pid === "any";
    $("ag-warn").hidden = !(cmd && mm && mm.warning);
    $("ag-warn").textContent = mm && mm.warning ? `${mm.name}: ${mm.warning}` : "";
  }
  $("ag-profile").addEventListener("change", agShowAdapter);
  document.querySelectorAll('input[name="ag-adapter"]').forEach((r) => r.addEventListener("change", agShowAdapter));
  function setAgForm(open) {
    $("ag-form").hidden = !open;
    $("ag-new").setAttribute("aria-expanded", String(open));
    if (open) {
      if (!$("ag-workdir").value) $("ag-workdir").value = store.get("agWorkdir") || "";
      $("ag-workdir").focus();
    }
  }
  $("ag-new").addEventListener("click", () => setAgForm($("ag-form").hidden));
  $("ag-cancel").addEventListener("click", () => setAgForm(false));
  $("ag-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    document.querySelectorAll("#ag-form .err").forEach((x) => { x.textContent = ""; });
    const spec = { name: $("ag-name").value, workdir: $("ag-workdir").value, task: $("ag-task").value, adapter: agentAdapter(),
      profile: $("ag-profile").value, start_profile: $("ag-startprof").checked, max_turns: $("ag-turns").value, max_tokens: $("ag-maxtok").value,
      command: $("ag-command").value, env: $("ag-env").value };
    try {
      const r = await api("POST", "/local/agents", spec);
      store.set("agWorkdir", spec.workdir); store.set("agProfile", spec.profile);
      $("ag-task").value = ""; $("ag-name").value = "";
      setAgForm(false);
      toast(`Queued ${r.session.spec.name}`);
      pollAgents();
      openAgLog(r.session.id);
    } catch (err) {
      const errs = err.data && err.data.errors;
      if (errs) for (const [k, v] of Object.entries(errs)) {
        const slot = document.querySelector(`#ag-form .err[data-agerr="${CSS.escape(k)}"]`);
        if (slot) slot.textContent = v; else $("ag-general").textContent += `${k}: ${v} `;
      } else $("ag-general").textContent = err.message;
    }
  });
  function renderAgents() {
    const d = S.agents;
    if (!d) return;
    const rows = d.sessions;
    $("agents-n").textContent = rows.length || "";
    const live = rows.filter((r) => !["done", "failed", "stopped", "interrupted"].includes(r.status)).length;
    $("ag-stop-all").hidden = !live;
    const nrun = rows.filter((r) => r.status === "running").length, nq = rows.filter((r) => r.status === "queued").length;
    $("f-agents").innerHTML = nrun || nq ? `<a href="#/agents">${nrun} running${nq ? `, ${nq} queued` : ""}</a>` : "none running";
    $("agents-note").textContent = d.parallel ? `${d.running} of ${d.parallel} running (the profile's parallel requests). Others queue.` :
      "Sessions wait until a profile is serving; then at most its parallel requests run at once.";
    if (!rows.length) { $("ag-body").innerHTML = `<tr><td colspan="8" class="empty">No sessions yet. Each one gets a work directory and a task.</td></tr>`; return; }
    $("ag-body").innerHTML = rows.map((r) => {
      const live = !["done", "failed", "stopped", "interrupted"].includes(r.status);
      const dot = r.status === "running" ? '<i class="dot"></i>' : r.status === "queued" ? '<i class="dot off"></i>' : r.status === "done" ? '<i class="dot ok"></i>' : "";
      const tok = (n) => n ? fmtN(n) : "–";
      return `<tr class="ag-row${S.agLog && S.agLog.id === r.id ? " sel" : ""}"><td><button type="button" class="textbtn ag-open" data-ag="${esc(r.id)}">${esc(r.spec.name)}</button>` +
        `<small class="ag-sub">${esc(r.spec.adapter === "builtin" ? "built-in" : "command")} · ${esc(r.spec.workdir.replace(/^\/Users\/[^/]+/, "~"))}${r.spec.profile !== "any" ? " · " + esc(r.spec.profile) : ""}</small></td>` +
        `<td class="ag-status"><span class="ag-st ${esc(r.status)}">${dot}${esc(AG_STATE[r.status] || r.status)}</span><small class="ag-sub">${esc(r.reason || "")}</small></td>` +
        `<td>${r.turns || "–"}</td><td>${tok(r.prompt_tokens)}</td><td>${tok(r.cached_tokens)}</td><td>${tok(r.completion_tokens)}</td><td>${r.elapsed_s === null ? "–" : dur(r.elapsed_s)}</td>` +
        `<td class="ag-act">${live || r.pid ? `<button type="button" class="textbtn danger" data-agstop="${esc(r.id)}">Stop</button>` :
          `<button type="button" class="textbtn" data-agagain="${esc(r.id)}">Again</button> <button type="button" class="textbtn" data-agrm="${esc(r.id)}">Remove</button>`}</td></tr>`;
    }).join("");
  }
  $("ag-body").addEventListener("click", async (e) => {
    const o = e.target.closest("[data-ag]"), st = e.target.closest("[data-agstop]"), rm = e.target.closest("[data-agrm]"), ag = e.target.closest("[data-agagain]");
    if (ag) {  // the same session again: its settings back in the form, to change or queue as they are
      const r = S.agents.sessions.find((x) => x.id === ag.dataset.agagain);
      if (!r) return;
      const sp = r.spec;
      $("ag-name").value = sp.name; $("ag-workdir").value = sp.workdir; $("ag-task").value = sp.task;
      document.querySelector(`input[name="ag-adapter"][value="${sp.adapter}"]`).checked = true;
      $("ag-profile").value = [...$("ag-profile").options].some((x) => x.value === sp.profile) ? sp.profile : "any";
      $("ag-startprof").checked = !!sp.start_profile; $("ag-turns").value = sp.max_turns || 40; $("ag-maxtok").value = sp.max_tokens || 2048;
      $("ag-command").value = sp.command || ""; $("ag-env").value = sp.env || "";
      agShowAdapter(); setAgForm(true); $("ag-task").focus();
      return;
    }
    try {
      if (o) openAgLog(o.dataset.ag);
      if (st) { await api("POST", `/local/agents/${encodeURIComponent(st.dataset.agstop)}/stop`, {}); toast("Stopping the session"); pollAgents(); }
      if (rm) { await api("DELETE", `/local/agents/${encodeURIComponent(rm.dataset.agrm)}`, {}); if (S.agLog && S.agLog.id === rm.dataset.agrm) closeAgLog(); pollAgents(); }
    } catch (err) { toast(err.message, true); }
  });
  $("ag-stop-all").addEventListener("click", async () => {
    if (!confirm("Stop every queued and running agent session?")) return;
    try { const r = await api("POST", "/local/agents/stop-all", {}); toast(`Stopping ${r.stopping}`); pollAgents(); } catch (err) { toast(err.message, true); }
  });
  function openAgLog(id) {
    S.agLog = { id, offset: 0, text: "", tab: "log" };
    const r = S.agents && S.agents.sessions.find((x) => x.id === id);
    $("ag-log-h").textContent = r ? r.spec.name : "Log";
    $("ag-log").hidden = false;
    selectAgTab("log");
    renderAgents();
  }
  function closeAgLog() { S.agLog = null; $("ag-log").hidden = true; renderAgents(); }
  $("ag-log-close").addEventListener("click", closeAgLog);
  function selectAgTab(t) {
    S.agLog.tab = t;
    $("ag-tab-log").setAttribute("aria-selected", String(t === "log")); $("ag-tab-result").setAttribute("aria-selected", String(t === "result"));
    $("ag-tab-log").tabIndex = t === "log" ? 0 : -1; $("ag-tab-result").tabIndex = t === "result" ? 0 : -1;
    $("ag-log-body").textContent = t === "log" ? S.agLog.text : "Loading…";
    loadAgLog();
  }
  $("ag-tab-log").addEventListener("click", () => selectAgTab("log"));
  $("ag-tab-result").addEventListener("click", () => selectAgTab("result"));
  async function loadAgLog() {
    const L = S.agLog;
    if (!L) return;
    const body = $("ag-log-body");
    try {
      if (L.tab === "result") {
        const d = await api("GET", `/local/agents/${encodeURIComponent(L.id)}/result`);
        body.textContent = d.text || "No answer yet. The built-in agent writes its answer to result.md when it finishes; a command session's output is its log.";
        return;
      }
      const atEnd = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
      const d = await api("GET", `/local/agents/${encodeURIComponent(L.id)}/log?offset=${L.offset}`);
      if (d.offset < L.offset) L.text = "";
      if (d.text) { L.text = (L.text + d.text).slice(-400000); body.textContent = L.text; if (atEnd) body.scrollTop = body.scrollHeight; }
      else if (!L.text) body.textContent = "Nothing logged yet.";
      L.offset = d.offset;
    } catch (err) { body.textContent = err.message; }
  }
  // Lab chat and the agents' lock (200 either way): asked first, so a locked page never requests their routes (no 401s)
  window.HaloPrivate = async function () {
    try { return await api("GET", "/local/private"); } catch (e) { return { open: true }; }  // unknown: the routes say
  };
  const agentsShown = () => !$("page-agents").hidden;  // the Lab runner's page is retired: read only while it shows
  async function pollAgents() {
    const priv = await window.HaloPrivate();
    try {
      if (!priv.open) { const e = new Error(priv.error || "locked"); e.status = priv.code; e.data = priv; throw e; }
      S.agents = await api("GET", "/local/agents"); S.agLock = null; renderAgents();
    } catch (err) {  // the sessions live in the private store: locked until the passcode (the fast poll reports contact)
      if ((err.status === 401 || err.status === 403) && window.HaloPrivateLock) {
        S.agLock = err.data || {};
        const box = $("ag-lock") || (() => { const d = document.createElement("div"); d.id = "ag-lock"; $("agents").insertBefore(d, $("agents").children[1]); return d; })();
        box.replaceChildren(window.HaloPrivateLock("Agent sessions", S.agLock));
        box.hidden = false;
        $("ag-new").hidden = true;
        return;
      }
    }
    if ($("ag-lock")) $("ag-lock").hidden = true;
    $("ag-new").hidden = false;
    loadAgLog();
  }

  // ---------------------------------------------------------------- connect
  function renderConnect() {
    const base = endpointNow(), model = modelNow();
    const st = S.st;
    const p = (st && st.profile) || S.profiles.find((x) => x.id === startDefault());
    const s = (st && st.settings) || (p && p.settings) || { context: 63488, max_tokens: 4096, thinking: false };
    $("c-base").textContent = base;
    $("c-model").textContent = model;
    $("c-context").textContent = p && p.fixed ? `${(p.chips || [])[0] || "its engine's"} (${p.name})` : `${fmtN(s.context)} tokens (${p ? p.name : "–"})`;
    $("c-max").textContent = p && p.fixed ? "its engine's own default: send max_tokens" : `${fmtN(s.max_tokens)} tokens when a request leaves max_tokens out`;
    const key = `${base}|${model}|${s.context}|${s.max_tokens}|${s.thinking}`;
    if (renderConnect.key === key) return;
    renderConnect.key = key;
    $("s-curl").textContent = `curl ${base}/chat/completions \\\n  -H 'Content-Type: application/json' \\\n  -d '{"model": "${model}", "max_tokens": 512, "temperature": 0,\n       "messages": [{"role": "user", "content": "Write a haiku about two linked boxes."}]}'`;
    $("s-py").textContent = `from openai import OpenAI\n\nclient = OpenAI(base_url="${base}", api_key="none")\nreply = client.chat.completions.create(\n    model="${model}",\n    max_tokens=1024,\n    temperature=0,  # above 0, also pass extra_body={"top_k": 40}\n    messages=[{"role": "user", "content": "Explain the extent rule in one paragraph."}],\n)\nprint(reply.choices[0].message.content)`;
    $("s-omp").textContent = `Provider:     OpenAI-compatible (custom)\nBase URL:     ${base}\nAPI key:      none (any text)\nModel:        ${model}\n` +
      `Context:      ${p && p.fixed ? "its engine's own" : fmtN(s.context) + " tokens"}\nMax output:   set it per request (default ${p && p.fixed ? "its engine's" : fmtN(s.max_tokens)})\n` +
      `Temperature:  0, or send top_k (e.g. 40) with any higher value`;
    $("s-env").textContent = `export OPENAI_BASE_URL=${base}\nexport OPENAI_API_KEY=none\nexport OPENAI_MODEL=${model}`;
  }

  // ---------------------------------------------------------------- tabs, copy, logs
  function wireTabs(container, onSelect) {
    const tabs = [...container.querySelectorAll('[role="tab"]')];
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => select(i));
      t.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); select((i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length, true); }
      });
    });
    function select(i, focus) {
      tabs.forEach((t, k) => { t.setAttribute("aria-selected", k === i ? "true" : "false"); t.tabIndex = k === i ? 0 : -1; const panel = t.getAttribute("aria-controls"); if (panel) $(panel).hidden = k !== i; });
      if (focus) tabs[i].focus();
      if (onSelect) onSelect(tabs[i]);
    }
  }
  wireTabs(document.querySelector(".snips .tabs"));
  wireTabs(document.querySelector(".logs .tabs"), (t) => { S.logTab = t.dataset.log; loadLogs(); });

  document.addEventListener("click", async (e) => {
    const b = e.target.closest(".copy");
    if (!b) return;
    const text = $(b.dataset.copyFrom).textContent;
    try { await navigator.clipboard.writeText(text); }
    catch (err) { const r = document.createRange(); r.selectNodeContents($(b.dataset.copyFrom)); const s = getSelection(); s.removeAllRanges(); s.addRange(r); document.execCommand("copy"); s.removeAllRanges(); }
    b.textContent = "Copied"; b.classList.add("done");
    setTimeout(() => { b.textContent = "Copy"; b.classList.remove("done"); }, 1400);
  });

  function setLogs(open) {
    $("logs").hidden = !open;
    $("open-logs").setAttribute("aria-expanded", String(open));
    if (open) { loadLogs(); $("logs-body").focus(); }
  }
  $("open-logs").addEventListener("click", () => setLogs($("logs").hidden));
  $("close-logs").addEventListener("click", () => setLogs(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("logs").hidden && !$("editor").open) setLogs(false); });
  async function loadLogs() {
    if ($("logs").hidden) return;
    const body = $("logs-body");
    const atEnd = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
    try {
      if (S.logTab === "events") {
        const d = await api("GET", "/api/events?limit=200");
        body.innerHTML = d.events.slice().reverse().map((e) => `<span class="ev-t">${new Date(e.t * 1000).toLocaleTimeString("en-GB")}</span> ` +
          (e.kind === "state" ? `<span class="ev-state">${esc(e.state_from)} → ${esc(e.state_to)}</span> ` : `${esc(e.kind)} `) + `${esc(e.actor && e.actor !== "halod" ? "[" + e.actor + "] " : "")}${esc(e.detail)}`).join("\n");
      } else {
        const d = await api("GET", `/api/logs?rank=${S.logTab}&n=400`);
        body.textContent = d.run_id ? `${d.box} · ${d.run_id}/rank${d.rank}.log\n\n${d.lines.join("\n")}` : "No run yet.";
      }
      if (atEnd) body.scrollTop = body.scrollHeight;
    } catch (err) { body.textContent = err.message; }
  }

  $("tunnel-restart").addEventListener("click", async () => {
    try { await api("POST", "/local/tunnels/restart", {}); toast("Reconnecting the tunnels"); } catch (err) { toast(err.message, true); }
  });

  // ---------------------------------------------------------------- polling
  async function poll() {
    try {
      S.st = await api("GET", "/api/state");
      S.fails = 0;
    } catch (err) {
      S.fails++;
      if (S.fails > 2) S.st = null;
    }
    render();
    if (S.profiles.length) {
      const key = S.st ? `${S.st.state}|${S.st.profile && S.st.profile.id}` : "none";
      if (key !== poll.key) { poll.key = key; renderModels(); }
      renderProfiles();
    }
    document.dispatchEvent(new CustomEvent("halo:state"));
  }
  async function pollSlow() {
    try {
      // the Requests page is retired: its list is read only while it shows
      const reqs = !$("page-requests").hidden;
      const [m, r] = await Promise.all([api("GET", "/api/metrics"), reqs ? api("GET", "/api/requests?limit=30") : Promise.resolve({ requests: S.requests || [] })]);
      S.metrics = m; S.requests = r.requests;
      renderMetrics(); renderRequests();
      if (S.st) renderBoxes(S.st);
    } catch (err) { /* the fast poll reports contact */ }
    loadLogs();
  }
  async function pollLocal() {
    try {
      S.local = await api("GET", "/local/status");
      $("fakebar").hidden = !S.local.fake;
      $("ft-version").textContent = S.local.version;
      const lan = S.local.lan || [];
      $("c-lan").textContent = lan.length ? `This dashboard and its chat on a phone or computer at home: ${lan.join(" or ")}` : "Home-network access is off (halo-control serve --no-lan).";
      $("set-lan").textContent = lan.length ? `On: ${lan.join(", ")}. Home-network devices only; no login. The model endpoint stays on the Mac.` : "Off.";
      const ts = S.local.tunnels;
      const down = ts.filter((t) => t.state !== "up");
      const conn = $("hd-conn");
      if (S.local.fake) { conn.textContent = "Fake backend on this Mac"; conn.classList.remove("warn"); $("ft-tunnels").textContent = "no tunnels (fake backend)"; }
      else if (!down.length) { conn.textContent = S.st && S.st.connected ? (S.st.state === "ready" ? `Connected to ${S.st.connected}` : `StrixFold isn't answering on ${S.st.connected}`) : `Tunnels up: halod via ${NAMES[1]}, model via ${NAMES[0]}`; conn.classList.remove("warn"); $("ft-tunnels").textContent = "tunnels up"; }
      else {
        conn.textContent = down.map((t) => `${t.name} tunnel ${t.state}${t.error ? ": " + t.error : ""}`).join(" · ");
        conn.classList.add("warn");
        $("ft-tunnels").textContent = down.map((t) => `${t.name} ${t.state}`).join(", ");
      }
    } catch (err) { $("hd-conn").textContent = "The local server is not answering."; $("hd-conn").classList.add("warn"); }
  }
  // the chat module (chat.js) reads this state and starts profiles through the same calls
  window.Halo = { S, api, toast, startProfile: (id, opts) => startProfile(id, opts), stopRun: () => stopRun(), endpointNow: () => endpointNow(),
    store, esc, fmtN, dur, clock, holderText };

  async function loadProfiles() {
    try {
      const d = await api("GET", "/api/profiles");
      S.profiles = d.profiles; S.models = d.models || []; S.streamsMax = d.streams_max || 1; S.streamsNote = d.streams_note || "";
      S.powerNote = d.power_note || "";
      renderModels(); renderProfiles(); render();
      document.dispatchEvent(new CustomEvent("halo:profiles"));
    } catch (err) { setTimeout(loadProfiles, 3000); }
  }

  let fast = 0, slow = 0, local = 0, ag = 0, profs = 0;
  // a poller never starts again while its last request is open (they piled up behind a frozen halod)
  // and while this Mac's server doesn't answer, each runs at a fifth of its pace (~2 errors a second a page)
  const one = (fn) => { let busy = false, n = 0; return async () => {
    if (busy) return;
    if (S.fails > 2) { if (n++ % 5) return; } else n = 0;
    busy = true; try { await fn(); } finally { busy = false; }
  }; };
  const P1 = one(poll), P2 = one(pollSlow), P3 = one(pollLocal), P4 = one(() => (agentsShown() ? pollAgents() : null));
  function schedule() {
    const hidden = document.hidden;
    clearInterval(fast); clearInterval(slow); clearInterval(local); clearInterval(ag); clearInterval(profs);
    fast = setInterval(P1, hidden ? 5000 : 1000);
    slow = setInterval(P2, hidden ? 15000 : 3000);
    local = setInterval(P3, hidden ? 15000 : 3000);
    ag = setInterval(P4, hidden ? 10000 : 2000);
    profs = setInterval(() => { if (!$("editor").open) loadProfiles(); }, 60000);  // install checks change slowly
  }
  document.addEventListener("visibilitychange", () => { schedule(); if (!document.hidden) { P1(); P2(); P4(); } });
  agShowAdapter();
  pollLocal(); loadProfiles(); poll(); pollSlow(); pollAgents(); schedule();
})();
