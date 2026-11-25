"""The Chat page's CSS and scripts as a browser reads them: the context ring on a phone (its top bar) and in the
composer (idle and while it answers), measured by headless Chrome in a frame of the right width with every stylesheet
each page loads; the scripts parse (node --check). Skipped where Chrome or node is missing."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "mac", "ui")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def measure(page, body, script, width):
    """What `script` (run in a frame `width` px wide holding `body` and every stylesheet of `page`) posts back."""
    html = open(os.path.join(UI, page), encoding="utf-8").read()
    links = "".join(f'<link rel="stylesheet" href="file://{UI}/{h}">' for h in re.findall(r'href="/ui/([^"]+\.css)"', html))
    d = tempfile.mkdtemp(prefix="hc-ui-")
    try:
        cls = ' class="studio-only"' if page == "studio-shell.html" else ""
        with open(os.path.join(d, "in.html"), "w", encoding="utf-8") as f:
            f.write(f'<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">{links}'
                    f'</head><body{cls}>{body}<script>parent.postMessage(String((() => {{ {script} }})()), "*");</script>'
                    '</body></html>')
        with open(os.path.join(d, "out.html"), "w", encoding="utf-8") as f:
            f.write(f'<!doctype html><pre id="r">-</pre><iframe src="file://{d}/in.html" width="{width}" height="400"></iframe>'
                    '<script>addEventListener("message", (e) => { r.textContent = "RESULT[" + e.data + "]"; });</script>')
        out = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                              f"--user-data-dir={d}/prof", "--allow-file-access-from-files", "--virtual-time-budget=3000",
                              "--dump-dom", f"file://{d}/out.html"], capture_output=True, text=True, timeout=60).stdout
    finally:
        shutil.rmtree(d, True)
    m = re.search(r"RESULT\[([^\]<]*)\]", out)
    return m.group(1) if m else None


@unittest.skipUnless(os.path.exists(CHROME), "needs Chrome for the computed styles")
class Ring(unittest.TestCase):
    def test_the_phones_ring_shows_in_the_top_bar_and_hides_when_hidden(self):
        """Review: `.t-ctx-top { display: inline-flex }` sat in the phone's media query above the unconditional
        `.t-ctx-top { display: none }`, the same specificity, so the ring never showed on a phone, on either page."""
        body = ('<header class="topbar"><div class="topbar-right"><button class="t-ctx-top" id="a">42%</button>'
                '<button class="t-ctx-top" id="b" hidden>42%</button></div></header>')
        script = "return [getComputedStyle(a).display, getComputedStyle(b).display].join(' ')"
        for page in ("index.html", "studio-shell.html"):
            shown, hidden = (measure(page, body, script, 390) or "? ?").split()
            self.assertNotEqual(shown, "none", f"{page}: the phone's ring never shows")
            self.assertEqual(hidden, "none", page)
            self.assertEqual(measure(page, body, script, 1200), "none none", f"{page}: a wide screen has the composer's")

    def test_the_composers_ring_while_idle_and_while_it_answers(self):
        """A wide screen: the window's size idle; the ring and its percent while it answers (none when nothing is
        measured yet). A phone: never in the composer (its top bar has it)."""
        html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
        start = html.index('<form class="t-composer" id="t-composer"')
        form = html[start:html.index("</form>", start) + len("</form>")]
        script = """const out = [];
            for (const steer of [false, true]) for (const nouse of [false, true]) {
              document.getElementById("t-composer").classList.toggle("steering", steer);
              document.getElementById("t-ctx").classList.toggle("no-use", nouse);
              document.getElementById("t-ring").toggleAttribute("hidden", nouse);
              const g = (id) => getComputedStyle(document.getElementById(id)).display !== "none" ? 1 : 0;
              out.push(`${g("t-ctx")}${g("t-ring")}${g("t-ctx-pct")}${g("t-ctx-k")}`);
            }
            return out.join(" ");"""
        body = f'<main id="page-talk" class="page-talk">{form}</main>'
        # chip, ring, percent, size: idle, idle with nothing measured, answering, answering with nothing measured
        self.assertEqual(measure("index.html", body, script, 1200), "1101 1001 1110 0010")
        self.assertEqual(measure("index.html", body, script, 390)[::5], "0000")  # each state's chip: hidden on a phone


VIEWER = r"""
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const JPEG = Uint8Array.from(atob("/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="), (c) => c.charCodeAt(0));
const seen = [], revoked = [];
window.IntersectionObserver = class {  // every page in view at once (a frame under virtual time draws none)
  constructor(cb) { this.cb = cb; this.on = true; }
  observe(t) { setTimeout(() => { if (this.on) this.cb([{ isIntersecting: true, target: t }]); }, 0); }
  unobserve() {}
  disconnect() { this.on = false; }
};
const revoke = URL.revokeObjectURL.bind(URL);
URL.revokeObjectURL = (u) => { revoked.push(u); revoke(u); };
window.fetch = async (url) => {
  url = String(url); seen.push(url);
  const json = (o, st) => new Response(JSON.stringify(o), { status: st || 200, headers: { "Content-Type": "application/json" } });
  if (url.includes("/x401")) return json({ error: "locked" }, 401);
  if (url.endsWith("/info")) return json({ pages: 2, sizes: [[612, 792], [612, 792]] });
  if (url.includes("/page/")) return new Response(new Blob([JPEG], { type: "image/jpeg" }));
  if (url.includes("?dl=1")) return new Response(new Blob(["%PDF-1.4"], { type: "application/pdf" }));
  if (url.includes("/studio/api/auth")) return json({ unlocked: true, set: true, local: true });
  return json({});
};
window.addEventListener("load", async () => {
  await wait(400);
  const K = window.HaloStudioKit, v = () => document.getElementById("viewer").hidden ? "closed" : "open";
  const out = [];
  K.openDoc({ id: "u0123456789", name: "lease.pdf" }, true);  // a Private chat's
  await wait(600);
  out.push(v(), document.querySelectorAll("#v-body img").length, seen.filter((u) => u.includes("?dl=1")).length);
  (K.forgetPrivate || (() => {}))();  // its own Lock (halo-chat.js lockPrivate)
  out.push(v());
  K.openDoc({ id: "u0123456789", name: "lease.pdf" }, true);
  await wait(600);
  await K.call("GET", "/studio/api/x401").catch(() => {});  // Studio's lock (a 401, the idle lock): dropPrivate
  out.push(v());
  K.openDoc({ id: "uaaaaaaaaaa", name: "a.pdf" }, false);  // a chat's that isn't Private stays open
  await wait(600);
  (K.forgetPrivate || (() => {}))();
  out.push(v());
  const before = revoked.length;
  K.openDoc({ id: "ubbbbbbbbbb", name: "b.pdf" }, false);  // a second document while one is open: its pages' images go
  await wait(600);
  out.push(revoked.length - before);
  parent.postMessage(out.join(" "), "*");
});
"""


@unittest.skipUnless(os.path.exists(CHROME), "needs Chrome")
class Viewer(unittest.TestCase):
    def test_a_private_chats_document_leaves_the_viewer_on_lock(self):
        """A Private chat's PDF stayed open in the viewer after its Lock, Studio's lock or the
        idle lock, and Save served its bytes from the cache for 120 s; opening a second document leaked the first one's
        page images; a wide screen fetched the whole file on every open for a Save it may never press. studio.js runs in
        the page with index.html's own markup and a stub fetch."""
        html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
        body = html[html.index(">", html.index("<body")) + 1:html.index('<script src="/ui/motion.js"')]
        d = tempfile.mkdtemp(prefix="hc-ui-")
        try:
            with open(os.path.join(d, "in.html"), "w", encoding="utf-8") as f:
                links = "".join(f'<link rel="stylesheet" href="file://{UI}/{h}">' for h in re.findall(r'href="/ui/([^"]+\.css)"', html))
                f.write(f'<!doctype html><html><head><meta name="halo-ui-token" content="t">{links}</head><body>{body}'
                        f'<script>{VIEWER}</script><script src="file://{UI}/md.js"></script>'
                        f'<script src="file://{UI}/studio.js"></script>'
                        '<script>addEventListener("load", () => 0);</script></body></html>')
            with open(os.path.join(d, "out.html"), "w", encoding="utf-8") as f:
                f.write(f'<!doctype html><pre id="r">-</pre><iframe src="file://{d}/in.html" width="1200" height="800"></iframe>'
                        '<script>addEventListener("message", (e) => { r.textContent = "RESULT[" + e.data + "]"; });</script>')
            out = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                  f"--user-data-dir={d}/prof", "--allow-file-access-from-files", "--virtual-time-budget=20000",
                                  "--dump-dom", f"file://{d}/out.html"], capture_output=True, text=True, timeout=90).stdout
        finally:
            shutil.rmtree(d, True)
        m = re.search(r'<pre id="r">RESULT\[([^\]<]*)\]', out)
        self.assertIsNotNone(m, out[-800:])
        # open with its 2 pages and no prefetch on a wide screen; closed by its Lock; closed by Studio's lock; another
        # chat's stays open; the second document took the first one's 2 page images with it
        self.assertEqual(m.group(1), "open 2 0 closed closed open 2")


PAGE = r"""
const wait = (ms) => new Promise((r) => setTimeout(r, ms)), $ = (id) => document.getElementById(id);
const errors = [], sent = [];
window.addEventListener("error", (e) => { if (!/^ResizeObserver loop/.test(e.message)) errors.push(`${e.message} ${String(e.filename).split("/").pop()}:${e.lineno}`); });  // (that one is a notice)
window.addEventListener("unhandledrejection", (e) => errors.push(String((e.reason && e.reason.stack) || e.reason).split("\n").slice(0, 2).join(" ")));
const MODELS = { models: [{ profile: "quick-chat", model_id: "dsv41", name: "DeepSeek V4.1", desc: "Fast", main: true, loaded: true, state: "ready", context: 65536,
  contexts: [{ profile: "quick-chat", k: "64K", ready: true, note: "Everyday" }] }, { profile: "look-chat", model_id: "qwen38", name: "Qwen 3.8", desc: "Looks", main: true,
  contexts: [] }], efforts: [{ id: "low", name: "Low", desc: "Short" }, { id: "high", name: "High", desc: "Long" }], legacy: {}, create: [], serving: { state: "ready", name: "DeepSeek V4.1" } };
const CHAT = { id: "0123456789ab", kind: "chat", title: "A chat", model: "quick-chat", effort: "low", messages: [], waiting: [], jobs: {}, items: {}, refs: {}, rev: "a" };
window.fetch = async (url, o) => {
  url = String(url); const method = (o && o.method) || "GET";
  const json = (x, st) => new Response(JSON.stringify(x), { status: st || 200, headers: { "Content-Type": "application/json" } });
  if (url.includes("/studio/api/auth")) return json({ unlocked: true, set: true, local: true, idle_lock_min: 0 });
  if (url.includes("/studio/api/state")) return json({ rev: { list: 1, chat: CHAT.rev }, llm: { state: "ready", model_id: "dsv41", model_name: "DeepSeek V4.1", pairs: [] }, settings: {}, jobs: [], batch: null, modes: {}, modes_rev: "r", toasts: [] });
  if (url.includes("/studio/api/chat/models")) return json(MODELS);
  if (url.includes("/studio/api/chats?kind=all")) return json({ chats: [Object.assign({ updated: 1 }, CHAT)] });
  if (url.includes("/send")) {
    sent.push(JSON.parse(o.body));
    const t = new TextEncoder(), ev = [{ type: "user", id: 1, content: sent[0].content, meta: { attachments: sent[0].attachments.map((a) => ({ id: a.id, kind: "image", name: "p.png" })) } },
      { type: "start", id: 2 }, { type: "delta", text: "Blue." }, { type: "done" }];
    CHAT.messages = [{ id: 1, role: "user", content: sent[0].content, meta: ev[0].meta }, { id: 2, role: "assistant", content: "Blue.", meta: { model: "DeepSeek V4.1", effort: "low" } }];
    return new Response(new ReadableStream({ start(c) { for (const e of ev) c.enqueue(t.encode(`data: ${JSON.stringify(e)}\n\n`)); c.close(); } }));
  }
  if (url.includes("/studio/api/chats/")) return json(CHAT);
  if (url.endsWith("/studio/api/chats") && method === "POST") return json(CHAT);
  if (url.includes("/studio/api/uploads")) return json({ id: "u0000000001", kind: "image", detail: "no text found" });
  if (url.includes("/studio/api/settings")) return json({ settings: {}, root: "/x", devices: [], profiles: [] });
  if (url.includes("/studio/api/projects")) return json({ projects: [{ id: "abcdef012345", name: "Acme", chats: 0 }] });
  if (url.includes("/studio/")) return json({});
  return json({ error: "away" }, 503);  // the dashboard's own routes: no contact with the boxes
};
window.addEventListener("load", async () => {
  const out = [];
  try {
    await wait(900);
    out.push($("page-talk").hidden ? "no chat page" : document.querySelector("#t-thread .t-hello h1").textContent, document.querySelectorAll("#s-list .h-item").length);
    $("t-model").click(); await wait(300);
    out.push(document.querySelectorAll("#t-sheet-body .t-opt").length);
    document.querySelectorAll("#t-sheet-body .t-opt")[1].click(); await wait(200);
    out.push($("t-model-name").textContent);
    document.querySelectorAll("#t-sheet-body .t-opt")[0].click(); await wait(100);
    $("t-sheet-x").click(); await wait(100);
    const dt = new DataTransfer(); dt.items.add(new File(["x"], "p.png", { type: "image/png" }));
    $("t-file").files = dt.files; $("t-file").dispatchEvent(new Event("change")); await wait(300);
    out.push(document.querySelectorAll("#t-atts .t-chip").length, $("t-send").disabled);
    $("t-prompt").value = "What color?"; $("t-prompt").dispatchEvent(new Event("input"));
    $("t-composer").requestSubmit(); await wait(900);
    out.push(JSON.stringify(Object.keys(sent[0] || {}).sort()), sent[0] && sent[0].attachments.length);
    out.push(document.querySelector("#t-thread .t-user .bubble").textContent, document.querySelector("#t-thread .t-asst .t-body").textContent, document.querySelectorAll("#t-thread .t-user .att-thumb").length);
    out.push(document.querySelectorAll('#t-thread [aria-label="Retry"]').length);
    CHAT.messages = CHAT.messages.concat([{ id: 3, role: "user", content: "And green?", meta: {} }, { id: 4, role: "assistant", content: "Green too.", meta: {} }]);
    CHAT.rev = "b"; await wait(4500);  // it went on elsewhere: the poll's rev says so, and the open chat follows by itself
    out.push(document.querySelectorAll("#t-thread .t-asst").length);
    for (const [h, page] of [["#/projects", "projects"], ["#/studio-settings", "studio-settings"], ["#/nowhere", "talk"]]) { location.hash = h; await wait(400); out.push($(`page-${page}`).hidden ? `no ${page}` : page); }
    out.push(document.querySelectorAll("#ss-body button").length > 5);
  } catch (e) { out.push(`stopped: ${e.message}`); }
  parent.postMessage(out.concat(errors).join("|").replace(/[\[\]<]/g, " "), "*");
});
"""


@unittest.skipUnless(os.path.exists(CHROME), "needs Chrome")
class Pages(unittest.TestCase):
    def test_both_pages_scripts_run_and_a_message_goes(self):
        """Every script of each page, with the page's own markup and a stub fetch: nothing throws, the chat shows, its model
        sheet works, a message with a photo goes and its answer is drawn, the open chat follows what happens to it
        elsewhere, the other pages open. The Command Centre's own scripts are left out."""
        index = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
        shell = open(os.path.join(UI, "studio-shell.html"), encoding="utf-8").read()
        for part in ("side", "pages", "viewer"):
            a, b = f"<!-- studio:{part} -->", f"<!-- /studio:{part} -->"
            shell = shell.replace(f"__STUDIO_{part.upper()}__", index[index.index(a):index.index(b)])
        for name, html in (("index.html", index), ("studio-shell.html", shell)):
            html = re.sub(r"<!-- addon:cousteau -->\n?(.*?)<!-- /addon:cousteau -->\n?", "", html, flags=re.S)
            html = html.replace("__HALO_UI_TOKEN__", "t").replace("__HALO_STUDIO_TOKEN__", "t").replace('"/ui/', f'"file://{UI}/')
            html = html.replace("</head>", f"<script>{PAGE}</script></head>", 1)
            d = tempfile.mkdtemp(prefix="hc-ui-")
            try:
                with open(os.path.join(d, "in.html"), "w", encoding="utf-8") as f:
                    f.write(html)
                with open(os.path.join(d, "out.html"), "w", encoding="utf-8") as f:
                    f.write(f'<!doctype html><pre id="r">-</pre><iframe src="file://{d}/in.html" width="1200" height="800"></iframe>'
                            '<script>addEventListener("message", (e) => { r.textContent = "RESULT[" + e.data + "]"; });</script>')
                out = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                      f"--user-data-dir={d}/prof", "--allow-file-access-from-files", "--virtual-time-budget=30000",
                                      "--dump-dom", f"file://{d}/out.html"], capture_output=True, text=True, timeout=90).stdout
            finally:
                shutil.rmtree(d, True)
            m = re.search(r'<pre id="r">RESULT\[([^\]<]*)\]', out)
            self.assertIsNotNone(m, name + out[-800:])
            self.assertEqual(m.group(1).replace("&quot;", '"'),
                             'How can I help?|1|2|Qwen 3.8|1|false| "attachments","content","effort","mode" |1|What color?|Blue.|1|1|2|projects|studio-settings|talk|true', name)


class Scripts(unittest.TestCase):
    def test_the_scripts_parse(self):
        node = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)
        if not node:
            self.skipTest("needs node")
        for name in ("halo-chat.js", "studio.js"):
            r = subprocess.run([node, "--check", os.path.join(UI, name)], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
