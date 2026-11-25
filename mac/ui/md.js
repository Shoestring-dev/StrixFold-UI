// A small Markdown renderer for model output. It builds DOM nodes with textContent only: no HTML from the model is
// ever parsed, so a reply cannot inject markup or script. Covers what chat replies use: paragraphs, headings, fenced
// code (with a Copy button), inline code, bold, italic, strikethrough, links (http/https/mailto only), lists (nested
// by indent), block quotes, GFM tables and rules. Unclosed fences render as code while a reply is still streaming.
(() => {
  "use strict";
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };

  // ---- inline: code spans first, then links, then emphasis, over plain text runs
  function inline(parent, text) {
    let i = 0;
    const flush = (s) => { if (s) emph(parent, s); };
    let buf = "";
    while (i < text.length) {
      const c = text[i];
      if (c === "`") {
        const n = text.slice(i).match(/^`+/)[0].length;
        const end = text.indexOf("`".repeat(n), i + n);
        if (end > 0) { flush(buf); buf = ""; parent.appendChild(el("code", "md-ic", text.slice(i + n, end).replace(/^ (.*) $/, "$1"))); i = end + n; continue; }
      }
      if (c === "[") {
        const m = text.slice(i).match(/^\[([^\]\n]{1,300})\]\(([^)\s]{1,2000})\)/);
        if (m && /^(https?:\/\/|mailto:)/i.test(m[2])) {
          flush(buf); buf = "";
          const a = el("a", "md-a"); a.href = m[2]; a.rel = "noopener noreferrer"; a.target = "_blank";
          emph(a, m[1]); parent.appendChild(a); i += m[0].length; continue;
        }
      }
      if (c === "h" && /^https?:\/\/[^\s<>()]+/.test(text.slice(i)) && (i === 0 || /[\s(]/.test(text[i - 1]))) {
        let url = text.slice(i).match(/^https?:\/\/[^\s<>()]+/)[0].replace(/[.,;:!?]+$/, "");
        flush(buf); buf = "";
        const a = el("a", "md-a", url); a.href = url; a.rel = "noopener noreferrer"; a.target = "_blank";
        parent.appendChild(a); i += url.length; continue;
      }
      buf += c; i++;
    }
    flush(buf);
  }
  function emph(parent, text) {
    const re = /(\*\*|__)(?=\S)([\s\S]*?\S)\1|(\*|_)(?=\S)([\s\S]*?\S)\3(?![\w*])|~~(?=\S)([\s\S]*?\S)~~/;
    while (text) {
      const m = re.exec(text);
      if (!m) { parent.appendChild(document.createTextNode(text)); return; }
      if (m.index) parent.appendChild(document.createTextNode(text.slice(0, m.index)));
      if (m[2] !== undefined) { const b = el("strong"); emph(b, m[2]); parent.appendChild(b); }
      else if (m[4] !== undefined) {
        // an underscore inside a word (snake_case) is not emphasis
        const before = text[m.index - 1] || " ";
        if (m[3] === "_" && /\w/.test(before)) { parent.appendChild(document.createTextNode(m[0][0])); text = text.slice(m.index + 1); continue; }
        const e = el("em"); emph(e, m[4]); parent.appendChild(e);
      } else { const s = el("s"); emph(s, m[5]); parent.appendChild(s); }
      text = text.slice(m.index + m[0].length);
    }
  }

  // ---- blocks
  function codeBlock(lang, code, onCopy) {
    const wrap = el("div", "md-code");
    const bar = el("div", "md-code-bar");
    bar.appendChild(el("span", "md-lang", lang || "text"));
    const b = el("button", "md-copy", "Copy"); b.type = "button";
    b.addEventListener("click", () => onCopy(code, b));
    bar.appendChild(b);
    wrap.appendChild(bar);
    const pre = el("pre"); pre.appendChild(el("code", "", code));
    wrap.appendChild(pre);
    return wrap;
  }
  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));

  function render(target, src, opts) {
    opts = opts || {};
    const onCopy = opts.onCopy || ((t, b) => { navigator.clipboard && navigator.clipboard.writeText(t); b.textContent = "Copied"; setTimeout(() => { b.textContent = "Copy"; }, 1200); });
    const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
    const out = document.createDocumentFragment();
    let i = 0;
    const listStack = [];
    const closeLists = () => { listStack.length = 0; };
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)/);
      if (fence) {
        const mark = fence[1], body = [];
        i++;
        while (i < lines.length && !new RegExp("^\\s*" + mark[0] + "{" + mark.length + ",}\\s*$").test(lines[i])) { body.push(lines[i]); i++; }
        i++;
        closeLists();
        out.appendChild(codeBlock(fence[2], body.join("\n"), onCopy));
        continue;
      }
      if (!line.trim()) { closeLists(); i++; continue; }
      const h = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (h) { closeLists(); const e = el("h" + Math.min(6, h[1].length + 1), "md-h"); inline(e, h[2]); out.appendChild(e); i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { closeLists(); out.appendChild(el("hr", "md-hr")); i++; continue; }
      if (/^\s*>/.test(line)) {
        closeLists();
        const body = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { body.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
        const q = el("blockquote", "md-q"); render(q, body.join("\n"), opts); out.appendChild(q);
        continue;
      }
      if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        closeLists();
        const head = cells(line);
        const aligns = cells(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
        const wrap = el("div", "md-table"), t = el("table"), thead = el("thead"), tr = el("tr");
        head.forEach((c, k) => { const th = el("th"); if (aligns[k]) th.style.textAlign = aligns[k]; inline(th, c); tr.appendChild(th); });
        thead.appendChild(tr); t.appendChild(thead);
        const tb = el("tbody");
        i += 2;
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          const r = el("tr");
          cells(lines[i]).forEach((c, k) => { const td = el("td"); if (aligns[k]) td.style.textAlign = aligns[k]; inline(td, c); r.appendChild(td); });
          tb.appendChild(r); i++;
        }
        t.appendChild(tb); wrap.appendChild(t); out.appendChild(wrap);
        continue;
      }
      const li = line.match(/^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/);
      if (li) {
        const indent = li[1].replace(/\t/g, "    ").length, ordered = /\d/.test(li[2]);
        while (listStack.length && (indent < listStack[listStack.length - 1].indent || (indent === listStack[listStack.length - 1].indent && listStack[listStack.length - 1].ordered !== ordered))) listStack.pop();
        let top = listStack[listStack.length - 1];
        if (!top || indent > top.indent) {
          const list = el(ordered ? "ol" : "ul", "md-list");
          if (ordered && parseInt(li[2], 10) > 1) list.start = parseInt(li[2], 10);
          if (top && top.lastItem) top.lastItem.appendChild(list); else out.appendChild(list);
          top = { indent, ordered, list, lastItem: null };
          listStack.push(top);
        }
        const item = el("li");
        const task = li[3].match(/^\[([ xX])\]\s+(.*)$/);
        if (task) { item.appendChild(document.createTextNode(task[1] === " " ? "☐ " : "☑ ")); inline(item, task[2]); } else inline(item, li[3]);
        top.list.appendChild(item); top.lastItem = item;
        i++;
        // a continuation line of the same item (indented, not a new item or block)
        while (i < lines.length && lines[i].trim() && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d{1,9}[.)])\s+/.test(lines[i]) && !/^\s*(`{3,}|~{3,})/.test(lines[i])) {
          item.appendChild(document.createTextNode(" ")); inline(item, lines[i].trim()); i++;
        }
        continue;
      }
      closeLists();
      const p = el("p", "md-p"), para = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !/^\s*(`{3,}|~{3,}|#{1,6}\s|>|([-*+]|\d{1,9}[.)])\s+)/.test(lines[i]) &&
             !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))) { para.push(lines[i]); i++; }
      para.forEach((l, k) => { if (k) p.appendChild(el("br")); inline(p, l.replace(/\s+$/, "")); });
      out.appendChild(p);
    }
    target.replaceChildren(out);
  }

  window.HaloMD = { render };
})();
