// StrixFold UI's motion: a line that resolves behind a caret, split-flap digits, a signal that runs the start sequence,
// and the link box drawn on a canvas, driven by what the boxes do: a state change decodes the state word, a new
// measurement flips its digits, the start sequence's signal travels to the step the run is in, and the link box shows the
// two ranks and their two cables doing what they do now. Reduced motion: text changes in place and the box draws one
// frame.
(() => {
  "use strict";
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
  const lerp = (a, b, t) => a + (b - a) * t;
  const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
  const seg = (p, a, b) => clamp((p - a) / (b - a));
  const TAU = Math.PI * 2;
  const RED = "#e4322b";
  const red = (a = 1) => `rgba(228,50,43,${clamp(a).toFixed(3)})`;
  const white = (a = 1) => `rgba(255,255,255,${clamp(a).toFixed(3)})`;
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---------------------------------------------------------------- decode
  // A line resolves left to right behind a thin signal caret: cells ahead show signal glyphs, the rest of the line
  // waits as faint ghost glyphs, letters land in ink with a brief signal tint. Each stand-in glyph is picked to fit
  // its cell's width, so nothing shifts. The real text stays in the DOM for screen readers.
  const POOLS = { upper: "ABCDEFGHJKLMNOPRSTUVXYZ0123456789", lower: "abcdefghiknoprstuvxz", thin: "itfr1", other: "0123456789" };
  const TICK = 72, SWEEP = 760, LEAD = 2, LAND = 380;
  const cv = document.createElement("canvas").getContext("2d");
  const wcache = new Map();
  const glyphW = (font, ch) => { const k = font + "|" + ch; if (!wcache.has(k)) { cv.font = font; wcache.set(k, cv.measureText(ch).width); } return wcache.get(k); };

  function decode(el, text) {
    if (el.dataset.text === text) return;
    el.dataset.text = text;
    if (reduce.matches || !text) { el.textContent = text; return; }
    const cs = getComputedStyle(el);
    const font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    el.textContent = "";
    const sr = document.createElement("span"); sr.className = "sr"; sr.textContent = text;
    const vis = document.createElement("span"); vis.className = "dvis"; vis.setAttribute("aria-hidden", "true");
    const cells = [];
    for (const ch of text) {
      const c = document.createElement("span"); c.className = "dch ghost"; c.textContent = ch;
      const g = document.createElement("span"); g.className = "dch-g"; c.append(g); vis.append(c);
      const set = /[A-Z]/.test(ch) ? POOLS.upper : /[a-z]/.test(ch) ? POOLS.lower : POOLS.other;
      const w0 = glyphW(font, ch);
      let pool = [...set].filter((x) => x !== ch && glyphW(font, x) <= w0 * 1.04 && glyphW(font, x) >= w0 * 0.7);
      if (pool.length < 3) pool = [...POOLS.thin].filter((x) => x !== ch && glyphW(font, x) <= w0 * 1.3);
      cells.push({ c, g, ch, pool: ch === " " || /[.,·]/.test(ch) ? [] : pool, land: -1e9 });
    }
    const caret = document.createElement("span"); caret.className = "dcaret"; vis.append(caret);
    el.append(sr, vis);
    const seed = Math.random() * 1000, t0 = performance.now(), n = cells.length;
    const dur = Math.max(SWEEP, n * 34);
    const frame = (now) => {
      if (el.dataset.text !== text) return; // replaced by a newer text
      const p = clamp((now - t0) / dur), pos = p * (n + LEAD), edge = clamp(Math.floor(pos - LEAD), 0, n);
      let busy = p < 1;
      cells.forEach((cell, j) => {
        if (j < edge) {
          if (cell.c.classList.contains("ghost") || cell.c.classList.contains("hot")) cell.land = now;
          const landing = now - cell.land < LAND;
          cell.c.className = landing ? "dch land" : "dch";
          cell.g.textContent = "";
          if (landing) busy = true;
          return;
        }
        cell.c.className = j < pos ? "dch hot" : "dch ghost";
        const k = Math.floor((now + seed + j * 37) / TICK);
        cell.g.textContent = cell.pool.length ? cell.pool[(k * 7 + j * 13 + (seed | 0)) % cell.pool.length] : cell.ch;
      });
      const at = cells[edge] ? cells[edge].c : cells[n - 1] && cells[n - 1].c;
      if (at) caret.style.transform = `translate(${cells[edge] ? at.offsetLeft : at.offsetLeft + at.offsetWidth}px, ${at.offsetTop}px)`;
      caret.classList.toggle("on", p > 0 && p < 1);
      if (busy) requestAnimationFrame(frame);
      else { el.textContent = text; }
    };
    requestAnimationFrame(frame);
  }

  // ---------------------------------------------------------------- split-flap digits
  // Each changed character flips through a few glyphs of its class, left to right, mid-flip glyphs in the signal.
  const NUM = "0123456789", STEP = 42, FLIPS = 5;
  function flap(el, text) {
    text = String(text);
    const old = el.dataset.text;
    if (old === text) return;
    el.dataset.text = text;
    if (reduce.matches || old === undefined || old === "–" || text === "–") { el.textContent = text; return; }
    el.textContent = "";
    el.setAttribute("aria-label", text);
    const spans = [...text].map((ch, i) => {
      const s = document.createElement("span"); s.className = "fl"; s.textContent = ch; s.setAttribute("aria-hidden", "true");
      el.append(s);
      return { s, ch, changed: old[old.length - text.length + i] !== ch, i };
    });
    const t0 = performance.now();
    const frame = (now) => {
      if (el.dataset.text !== text) return;
      let busy = false;
      for (const x of spans) {
        if (!x.changed || !/[0-9]/.test(x.ch)) continue;
        const k = Math.floor((now - t0 - x.i * STEP) / STEP);
        if (k < 0) { busy = true; continue; }
        if (k < FLIPS) { x.s.textContent = NUM[(k * 3 + x.i * 7) % 10]; x.s.classList.add("hot"); busy = true; }
        else { x.s.textContent = x.ch; x.s.classList.remove("hot"); }
      }
      if (busy) requestAnimationFrame(frame); else { el.textContent = text; el.removeAttribute("aria-label"); }
    };
    requestAnimationFrame(frame);
  }

  // ---------------------------------------------------------------- the start sequence's signal
  // A dot runs the line from step to step as the run advances (eased), the line behind it fills, and each marker it
  // reaches pings like the radar and flips its title in. Phones: the line runs down the left through the markers.
  const seq = { pos: -1, target: -1, bad: false, pings: [], raf: 0, t: 0 };
  function seqDraw(now) {
    seq.raf = 0;
    const canvas = document.getElementById("seqcanvas");
    if (!canvas) return;
    const marks = [...document.querySelectorAll(".seq-mark")];
    const box = canvas.getBoundingClientRect();
    const dpr = devicePixelRatio || 1;
    if (canvas.width !== Math.round(box.width * dpr) || canvas.height !== Math.round(box.height * dpr)) {
      canvas.width = Math.round(box.width * dpr); canvas.height = Math.round(box.height * dpr);
    }
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, box.width, box.height);
    if (!marks.length) return;
    const pts = marks.map((m) => { const r = m.getBoundingClientRect(); return [r.left + r.width / 2 - box.left, r.top + r.height / 2 - box.top]; });
    const ink = css("--ink") || "#000", faint = css("--faint") || "rgba(0,0,0,.12)";
    // the track
    g.strokeStyle = faint; g.lineWidth = 1; g.beginPath(); g.moveTo(...pts[0]);
    for (const p of pts.slice(1)) g.lineTo(...p);
    g.stroke();
    // move toward the target step
    const dt = Math.min(64, now - (seq.t || now)); seq.t = now;
    if (seq.pos < 0 && seq.target >= 0) seq.pos = 0;
    if (seq.target >= 0) {
      const d = seq.target - seq.pos;
      seq.pos = Math.abs(d) < 0.002 ? seq.target : seq.pos + clamp(d * Math.min(1, dt / 220), -dt / 600, dt / 600);
    }
    const at = (p) => { const i = clamp(Math.floor(p), 0, pts.length - 1), f = p - i, a = pts[i], b = pts[Math.min(pts.length - 1, i + 1)]; return [lerp(a[0], b[0], f), lerp(a[1], b[1], f)]; };
    let busy = seq.target >= 0 && Math.abs(seq.target - seq.pos) > 0.002;
    if (seq.pos >= 0) {
      const head = at(seq.pos);
      // the wake: ink behind the dot
      g.strokeStyle = ink; g.lineWidth = 1.5; g.beginPath(); g.moveTo(...pts[0]);
      for (let i = 1; i <= Math.floor(seq.pos); i++) g.lineTo(...pts[i]);
      g.lineTo(...head); g.stroke();
      // the dot, pinging while the run waits in this step
      g.fillStyle = seq.bad ? RED : RED;
      g.beginPath(); g.arc(head[0], head[1], 4, 0, TAU); g.fill();
      if (!reduce.matches && seq.live) {
        const k = ((now / 1150) % 1);
        g.strokeStyle = red(0.8 * (1 - k)); g.lineWidth = 1; g.beginPath(); g.arc(head[0], head[1], 4 + 18 * ease(k), 0, TAU); g.stroke();
        busy = true;
      }
    }
    if (busy && !reduce.matches) seq.raf = requestAnimationFrame(seqDraw);
  }
  const seqKick = () => { if (!seq.raf) seq.raf = requestAnimationFrame(seqDraw); };
  addEventListener("resize", seqKick);

  function flipTitle(h) {
    if (reduce.matches || h.dataset.flipped) return;
    h.dataset.flipped = "1";
    const text = h.textContent;
    const GL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    h.textContent = "";
    const sr = document.createElement("span"); sr.className = "sr"; sr.textContent = text; h.append(sr);
    const chars = [...text].map((ch, i) => {
      const c = document.createElement("span"); c.className = "ech off"; c.textContent = ch; c.setAttribute("aria-hidden", "true");
      const f = document.createElement("span"); f.className = "ech-f"; c.append(f); h.append(c);
      return { c, f, ch, i };
    });
    const t0 = performance.now();
    const frame = (now) => {
      let busy = false;
      for (const x of chars) {
        const k = Math.floor((now - t0 - x.i * 30) / 52);
        if (x.ch === " ") { x.c.classList.remove("off"); continue; }
        if (k < 0) { busy = true; continue; }
        if (k < 4) { x.f.textContent = GL[(k * 11 + x.i * 7) % GL.length]; busy = true; }
        else { x.f.textContent = ""; x.c.classList.remove("off"); }
      }
      if (busy) requestAnimationFrame(frame); else h.textContent = text;
    };
    requestAnimationFrame(frame);
  }

  function setSequence(stepIndex, opts = {}) {
    // stepIndex: -1 none, 0 waiting, 1 loading, 2 linking, 3 ready; opts.live: the run is in that step now;
    // opts.bad: the run failed in that step
    const steps = [...document.querySelectorAll(".seq-step")];
    steps.forEach((s, i) => {
      s.classList.toggle("done", stepIndex >= 0 && i < stepIndex);
      s.classList.toggle("on", i === stepIndex && !opts.bad);
      s.classList.toggle("bad", i === stepIndex && !!opts.bad);
      if (i <= stepIndex && opts.live && i === stepIndex) flipTitle(s.querySelector(".seq-title"));
      if (i > stepIndex) delete s.querySelector(".seq-title").dataset.flipped;
    });
    if (stepIndex < seq.target) seq.pos = stepIndex; // a new run starts over
    seq.target = stepIndex; seq.live = !!opts.live; seq.bad = !!opts.bad;
    seqKick();
  }

  // ---------------------------------------------------------------- the link box
  // Black box, faint lines, one red. Two ranks (box1 left, box2 right), two USB4 cables between them.
  //  idle: both dim. waiting: a red square closes around each box whose lock someone else holds.
  //  loading: weights stream into each box. linking: pulses cross both cables.
  //  ready: packets cross the cables at a rate set by the live tok/s; a slow heartbeat while nothing runs.
  //  failed / a cable down: the failing part in red.
  const box = { mode: "idle", rate: 0, held: [false, false], down: [false, false], failRank: null, t0: performance.now(), raf: 0 };
  function dot(g, x, y, r, c) { g.fillStyle = c; g.beginPath(); g.arc(x, y, Math.max(0, r), 0, TAU); g.fill(); }
  function ring(g, x, y, r, c, lw = 1) { g.strokeStyle = c; g.lineWidth = lw; g.beginPath(); g.arc(x, y, Math.max(0, r), 0, TAU); g.stroke(); }
  function line(g, x1, y1, x2, y2, c, lw = 1) { g.strokeStyle = c; g.lineWidth = lw; g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke(); }
  function sq(g, x, y, s, c, lw = 1) { g.strokeStyle = c; g.lineWidth = lw; g.strokeRect(x - s / 2, y - s / 2, s, s); }
  function rrect(g, x, y, w, h, c, lw = 1) { g.strokeStyle = c; g.lineWidth = lw; g.strokeRect(x, y, w, h); }
  function hash(a) { const s = Math.sin(a * 127.1) * 43758.5453; return s - Math.floor(s); }

  function drawBox(now) {
    box.raf = 0;
    const canvas = document.getElementById("linkcanvas");
    if (!canvas) return;
    const r = canvas.getBoundingClientRect();
    if (!r.width) return;
    const dpr = devicePixelRatio || 1;
    if (canvas.width !== Math.round(r.width * dpr)) { canvas.width = Math.round(r.width * dpr); canvas.height = Math.round(r.height * dpr); }
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = r.width, H = r.height, t = reduce.matches ? 2600 : now - box.t0;
    g.fillStyle = "#000"; g.fillRect(0, 0, W, H);
    // faint grid, as the radar's rings
    for (let i = 1; i < 6; i++) line(g, (W * i) / 6, 0, (W * i) / 6, H, white(0.035));
    line(g, 0, H / 2, W, H / 2, white(0.035));
    const bw = Math.min(W * 0.2, 150), bh = bw * 0.62, y = H / 2;
    const A = [W * 0.2, y], B = [W * 0.8, y];
    const cab = [y - bh * 0.18, y + bh * 0.18]; // the two cables
    const mode = box.mode, on = mode === "ready" || mode === "linking";
    // the cables
    for (let c = 0; c < 2; c++) {
      const down = box.down[c];
      const col = down ? red(0.9) : white(on ? 0.32 : 0.12);
      if (down) { g.setLineDash([3, 4]); }
      line(g, A[0] + bw / 2, cab[c], B[0] - bw / 2, cab[c], col, 1);
      g.setLineDash([]);
    }
    // the boxes
    [A, B].forEach(([x, yy], i) => {
      const failed = box.failRank === i || (mode === "failed" && box.failRank === null);
      const lit = mode === "ready" ? 0.85 : mode === "idle" ? 0.28 : 0.6;
      rrect(g, x - bw / 2, yy - bh / 2, bw, bh, failed ? red(0.95) : white(lit), 1);
      // memory fill: loading fills each box from the bottom
      let fill = mode === "ready" || mode === "linking" ? 1 : mode === "loading" ? clamp(box.progress || seg(t, 0, 22000)) : 0;
      if (fill > 0) { g.fillStyle = white(mode === "ready" ? 0.1 : 0.07); g.fillRect(x - bw / 2 + 1, yy + bh / 2 - (bh - 2) * fill - 1, bw - 2, (bh - 2) * fill); }
      // GPU tiles: 8 cells pulse with activity
      for (let k = 0; k < 8; k++) {
        const cx = x - bw / 2 + bw * (0.16 + 0.68 * (k % 4) / 3), cy = yy - bh * 0.14 + bh * 0.28 * Math.floor(k / 4);
        const busy = mode === "ready" && box.rate > 0 ? 0.35 + 0.6 * Math.abs(Math.sin(t / (300 + 40 * k) + k)) : mode === "loading" ? 0.25 : 0.12;
        dot(g, cx, cy, 1.6, failed ? red(0.8) : white(busy));
      }
      if (failed) {
        line(g, x - 7, yy - bh / 2 - 16, x + 7, yy - bh / 2 - 2, red(0.95), 1.5);
        line(g, x + 7, yy - bh / 2 - 16, x - 7, yy - bh / 2 - 2, red(0.95), 1.5);
      }
      // waiting: someone else holds this box's lock: a red square closes around the box (closeBox motif)
      if (mode === "waiting") {
        if (box.held[i]) {
          const k = (t % 2400) / 2400;
          sq(g, x, yy, lerp(bw * 1.55, bw * 1.12, ease(seg(k, 0, 0.6))), red(0.85 * (1 - seg(k, 0.75, 1))), 1);
          dot(g, x + bw / 2 + 10, yy - bh / 2 - 10, 3, red(0.9));
        } else {
          ring(g, x, yy, bw * 0.75 * ((t % 1800) / 1800), white(0.3 * (1 - (t % 1800) / 1800)), 1);
        }
      }
    });
    // loading: weights stream in from below each box (the rank cache read)
    if (mode === "loading") {
      for (let i = 0; i < 2; i++) {
        const [x, yy] = i ? B : A;
        for (let k = 0; k < 14; k++) {
          const ph = ((t / 900 + k / 14) % 1), sx = x + (hash(k + i * 31) - 0.5) * bw * 0.9;
          dot(g, sx, lerp(H - 4, yy + bh / 2 + 4, ease(ph)), 1.4, white(0.55 * (1 - ph)));
        }
      }
    }
    // linking: handshake pulses cross both cables, both ways
    if (mode === "linking") {
      for (let c = 0; c < 2; c++) {
        const k = ((t / 700 + c * 0.5) % 1), x = lerp(A[0] + bw / 2, B[0] - bw / 2, c ? 1 - k : k);
        dot(g, x, cab[c], 2.4, red(0.95));
        ring(g, x, cab[c], 6 + 6 * k, red(0.5 * (1 - k)), 1);
      }
    }
    // ready: traffic in proportion to the live rate (decode or prompt tokens a second); a heartbeat when quiet
    if (mode === "ready") {
      const n = box.rate > 0 ? clamp(Math.round(box.rate / 6), 2, 26) : 0;
      for (let c = 0; c < 2; c++) {
        for (let k = 0; k < n; k++) {
          const ph = ((t / (1400 - Math.min(900, box.rate * 4)) + k / n + c * 0.37) % 1);
          const dir = (k + c) % 2 ? ph : 1 - ph;
          dot(g, lerp(A[0] + bw / 2, B[0] - bw / 2, dir), cab[c], 1.5, white(0.85));
        }
      }
      if (!n) {
        const k = (t % 4000) / 4000;
        [A, B].forEach(([x, yy]) => ring(g, x, yy, bw * 0.55 + bw * 0.5 * ease(seg(k, 0, 0.5)), white(0.22 * (1 - seg(k, 0, 0.5))), 1));
      }
    }
    if (mode === "stopping") {
      const k = seg(t, 0, 2200);
      for (let c = 0; c < 2; c++) line(g, A[0] + bw / 2, cab[c], lerp(B[0] - bw / 2, A[0] + bw / 2, ease(k)), cab[c], white(0.5), 1);
    }
    const moving = !reduce.matches && mode !== "idle" && !(mode === "failed");
    if (moving && !document.hidden) box.raf = requestAnimationFrame(drawBox);
  }
  const boxKick = () => { if (!box.raf) box.raf = requestAnimationFrame(drawBox); };
  addEventListener("resize", boxKick);
  document.addEventListener("visibilitychange", boxKick);
  function setBox(next) {
    if (next.mode && next.mode !== box.mode) box.t0 = performance.now();
    Object.assign(box, next);
    boxKick();
  }

  // ---------------------------------------------------------------- sparklines and the request chart
  function sparkline(canvas, values, opts = {}) {
    const r = canvas.getBoundingClientRect();
    if (!r.width) return;
    const dpr = devicePixelRatio || 1;
    canvas.width = Math.round(r.width * dpr); canvas.height = Math.round(r.height * dpr);
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, r.width, r.height);
    const v = values.filter((x) => x !== null && x !== undefined && !isNaN(x));
    if (v.length < 2) return;
    const lo = opts.min !== undefined ? opts.min : Math.min(...v), hi = opts.max !== undefined ? opts.max : Math.max(...v);
    const span = hi - lo || 1;
    g.strokeStyle = opts.color || css("--ink"); g.lineWidth = 1.2; g.beginPath();
    values.forEach((x, i) => {
      if (x === null || x === undefined) return;
      const px = (i / (values.length - 1)) * (r.width - 2) + 1, py = r.height - 2 - ((x - lo) / span) * (r.height - 4);
      i ? g.lineTo(px, py) : g.moveTo(px, py);
    });
    g.stroke();
    const last = v[v.length - 1];
    dot(g, r.width - 1.5, r.height - 2 - ((last - lo) / span) * (r.height - 4), 2, opts.dot || RED);
  }

  function requestChart(canvas, rows) {
    const r = canvas.getBoundingClientRect();
    if (!r.width) return;
    const dpr = devicePixelRatio || 1;
    canvas.width = Math.round(r.width * dpr); canvas.height = Math.round(r.height * dpr);
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, r.width, r.height);
    const W = r.width, H = r.height, n = Math.max(rows.length, 30);
    const dec = rows.map((x) => x.decode_tps || 0), tt = rows.map((x) => x.ttft_s || 0);
    const dmax = Math.max(10, ...dec), tmax = Math.max(1, ...tt);
    const bw = W / n;
    g.fillStyle = css("--faint"); g.fillRect(0, H - 1, W, 1);
    rows.forEach((x, i) => {
      const bx = W - (rows.length - i) * bw;
      const h = ((x.decode_tps || 0) / dmax) * (H - 14);
      g.fillStyle = css("--ink"); g.fillRect(bx + bw * 0.2, H - 1 - h, Math.max(1, bw * 0.45), h);
      const ty = H - 1 - ((x.ttft_s || 0) / tmax) * (H - 14);
      dot(g, bx + bw * 0.75, ty, 2, RED);
    });
    g.fillStyle = css("--muted"); g.font = "11px " + css("--mono");
    g.fillText(`${Math.round(dmax)} tok/s`, 2, 10);
    g.textAlign = "right"; g.fillStyle = RED; g.fillText(`${tmax.toFixed(1)} s`, W - 2, 10);
  }

  window.HaloMotion = { decode, flap, setSequence, setBox, sparkline, requestChart, reduce };
})();
