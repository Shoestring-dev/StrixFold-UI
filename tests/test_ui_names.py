"""The pages' scripts call only what is there. A copy of the app with parts left out kept HaloStudioKit members whose
functions went with them: halo-chat.js's onState called one on every state poll, it threw, the poll's try/catch swallowed
it, and an open chat never followed a goal, a queued answer or another device. The names are read by node's own parser
(every script here, whichever parts this copy has); onState runs in node on the page's own kit."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "mac", "ui")
NODE = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if os.path.exists(p)), None)
SCRIPTS = sorted(f for f in os.listdir(UI) if f.endswith(".js"))
# what the browser itself has: a name a script uses and neither declares nor finds here is a name nothing defines
BROWSER = """window document location navigator history localStorage sessionStorage fetch console setTimeout clearTimeout setInterval
clearInterval requestAnimationFrame cancelAnimationFrame queueMicrotask performance URL URLSearchParams Blob File FormData Response
AbortController Event CustomEvent MutationObserver ResizeObserver IntersectionObserver Notification MediaRecorder AudioContext CSS
matchMedia getComputedStyle getSelection innerWidth innerHeight devicePixelRatio confirm alert Option Image Audio TextDecoder
TextEncoder crypto atob btoa Math JSON Object Array Number String Boolean Date Map Set WeakMap WeakSet Promise RegExp Error TypeError
SyntaxError RangeError Symbol Infinity NaN undefined isNaN isFinite parseInt parseFloat encodeURIComponent decodeURIComponent Intl
Proxy Reflect globalThis module ArrayBuffer DataView Uint8Array Float32Array Path2D Node Element HTMLElement DOMParser DataTransfer
structuredClone arguments self addEventListener""".split()

# every name each script uses and nothing in it declares (block scopes, hoisting, parameters, destructuring), and what it
# reads of and puts on the objects the scripts share (window.HaloStudioKit and the like)
SCOPE = r"""
let acorn;
try { acorn = require("internal/deps/acorn/acorn/dist/acorn"); } catch (e) { console.log("no parser"); process.exit(0); }
const fs = require("fs"), BROWSER = new Set(JSON.parse(process.argv[2]));
const out = { undefined: [], used: [], defined: {} };
for (const file of process.argv.slice(3)) {
  const name = file.split("/").pop(), ast = acorn.parse(fs.readFileSync(file, "utf8"), { ecmaVersion: "latest", locations: true });
  class Scope {
    constructor(up, fn) { this.up = up; this.fn = fn; this.names = new Set(); }
    has(n) { for (let s = this; s; s = s.up) if (s.names.has(n)) return true; return false; }
    top() { let s = this; while (!s.fn) s = s.up; return s; }
  }
  const ids = (n, f) => {  // the names a pattern binds
    if (!n) return;
    if (n.type === "Identifier") f(n.name);
    else if (n.type === "ObjectPattern") n.properties.forEach((p) => ids(p.type === "RestElement" ? p.argument : p.value, f));
    else if (n.type === "ArrayPattern") n.elements.forEach((e) => ids(e, f));
    else if (n.type === "AssignmentPattern") ids(n.left, f);
    else if (n.type === "RestElement") ids(n.argument, f);
  };
  const hoist = (body, sc) => { for (const s of body) {
    if ((s.type === "FunctionDeclaration" || s.type === "ClassDeclaration") && s.id) sc.names.add(s.id.name);
    if (s.type === "VariableDeclaration") for (const d of s.declarations) ids(d.id, (n) => (s.kind === "var" ? sc.top() : sc).names.add(n));
  } };
  const pattern = (n, sc) => {  // a declared pattern's own expressions: defaults and computed keys
    if (!n) return;
    if (n.type === "ObjectPattern") n.properties.forEach((p) => { if (p.computed) walk(p.key, sc); pattern(p.type === "RestElement" ? p.argument : p.value, sc); });
    else if (n.type === "ArrayPattern") n.elements.forEach((e) => pattern(e, sc));
    else if (n.type === "AssignmentPattern") { walk(n.right, sc); pattern(n.left, sc); }
    else if (n.type === "RestElement") pattern(n.argument, sc);
  };
  function func(n, sc) {
    const s = new Scope(sc, true);
    if (n.type === "FunctionExpression" && n.id) s.names.add(n.id.name);
    n.params.forEach((p) => ids(p, (x) => s.names.add(x)));
    n.params.forEach((p) => pattern(p, s));
    if (n.body.type === "BlockStatement") { hoist(n.body.body, s); n.body.body.forEach((x) => walk(x, s)); } else walk(n.body, s);
  }
  function walk(n, sc) {
    if (!n) return;
    if (Array.isArray(n)) { n.forEach((x) => walk(x, sc)); return; }
    switch (n.type) {
      case "Program": case "BlockStatement": { const s = new Scope(sc, n.type === "Program"); hoist(n.body, s); walk(n.body, s); return; }
      case "FunctionDeclaration": case "FunctionExpression": case "ArrowFunctionExpression": func(n, sc); return;
      case "ClassDeclaration": case "ClassExpression": walk(n.superClass, sc); n.body.body.forEach((m) => { if (m.computed) walk(m.key, sc); walk(m.value, sc); }); return;
      case "VariableDeclaration": n.declarations.forEach((d) => { pattern(d.id, sc); walk(d.init, sc); }); return;
      case "ForStatement": { const s = new Scope(sc, false); if (n.init && n.init.type === "VariableDeclaration") hoist([n.init], s); [n.init, n.test, n.update, n.body].forEach((x) => walk(x, s)); return; }
      case "ForOfStatement": case "ForInStatement": { const s = new Scope(sc, false); if (n.left.type === "VariableDeclaration") { hoist([n.left], s); pattern(n.left.declarations[0].id, s); } else walk(n.left, s); walk(n.right, s); walk(n.body, s); return; }
      case "CatchClause": { const s = new Scope(sc, false); ids(n.param, (x) => s.names.add(x)); walk(n.body, s); return; }
      case "SwitchStatement": { walk(n.discriminant, sc); const s = new Scope(sc, false); n.cases.forEach((c) => hoist(c.consequent, s)); n.cases.forEach((c) => { walk(c.test, s); walk(c.consequent, s); }); return; }
      case "Identifier": if (!sc.has(n.name) && !BROWSER.has(n.name)) out.undefined.push(`${name}:${n.loc.start.line} ${n.name}`); return;
      case "MemberExpression": walk(n.object, sc); if (n.computed) walk(n.property, sc); return;
      case "Property": if (n.computed) walk(n.key, sc); walk(n.value, sc); return;
      case "LabeledStatement": walk(n.body, sc); return;
      case "BreakStatement": case "ContinueStatement": return;
    }
    for (const k of Object.keys(n)) { const v = n[k]; if (k === "loc" || !v || typeof v !== "object") continue; if (Array.isArray(v)) walk(v.filter((x) => x && x.type), sc); else if (v.type) walk(v, sc); }
  }
  walk(ast, null);
  // the shared objects: window.X = { … } defines, `const K = window.X` (or () => window.X) then K.y reads
  const isWin = (n) => n && n.type === "MemberExpression" && !n.computed && n.object.name === "window" ? n.property.name : null;
  const alias = {}, getter = {};
  const each = (n, f) => { if (!n || typeof n !== "object") return; if (Array.isArray(n)) { n.forEach((x) => each(x, f)); return; } if (n.type) f(n); for (const k of Object.keys(n)) if (k !== "loc") each(n[k], f); };
  each(ast, (n) => {
    if (n.type === "AssignmentExpression" && isWin(n.left) && n.right.type === "ObjectExpression") { const d = out.defined[isWin(n.left)] = out.defined[isWin(n.left)] || []; n.right.properties.forEach((p) => { if (p.key) d.push(p.key.name || p.key.value); }); }
    if (n.type === "VariableDeclarator" && n.id.type === "Identifier" && isWin(n.init)) alias[n.id.name] = isWin(n.init);
    if (n.type === "VariableDeclarator" && n.id.type === "Identifier" && n.init && n.init.type === "ArrowFunctionExpression" && isWin(n.init.body)) getter[n.id.name] = isWin(n.init.body);
  });
  each(ast, (n) => {
    const of = (o) => o.type === "Identifier" ? alias[o.name] : o.type === "CallExpression" && o.callee.type === "Identifier" ? getter[o.callee.name] : isWin(o);
    if (n.type === "MemberExpression" && !n.computed && of(n.object)) out.used.push([of(n.object), n.property.name, `${name}:${n.loc.start.line}`]);
    if (n.type === "VariableDeclarator" && n.id.type === "ObjectPattern" && n.init && of(n.init)) n.id.properties.forEach((p) => { if (p.key) out.used.push([of(n.init), p.key.name, `${name}:${p.loc.start.line}`]); });
  });
}
console.log(JSON.stringify(out));
"""

# onState (halo-chat.js) on the kit studio.js gives it: a name the file declares is a stand-in, one it doesn't throws as
# the browser would
ONSTATE = r"""
const fs = require("fs"), studio = fs.readFileSync(process.argv[2], "utf8"), chat = fs.readFileSync(process.argv[3], "utf8");
const part = (s, from, to) => { const a = s.indexOf(from); return s.slice(a, s.indexOf(to, a)); };
const declares = (s, n) => new RegExp(`\\b(?:function|const|let|class)\\s+${n.replace(/\$/g, "\\$")}(?![\\w$])|,\\s*${n.replace(/\$/g, "\\$")}\\s*=[^=]`).test(s);
const run = (s, code, given) => new Function("scope", `with (scope) { return (${code}); }`)(new Proxy({}, {
  has: (t, k) => typeof k === "string" && !(k in globalThis && !(k in given)),
  get: (t, k) => { if (k === Symbol.unscopables) return undefined; if (k in given) return given[k]; if (declares(s, k)) return () => undefined; throw new ReferenceError(`${k} is not defined`); },
}));
const kitText = part(studio, "window.HaloStudioKit = {", "\n  // ----").replace(/^window\.HaloStudioKit = /, "").replace(/;\s*$/, "");
const K = run(studio, kitText, {});
const n = { reload: 0, bar: 0, chip: 0 };
const C = { page: "talk", chat: { id: "c", rev: "a" }, streaming: null, optSig: undefined };
const onState = run(chat, part(chat, "function onState(st, rev) {", "\n  async function refresh() {"),
  { C, K, $: () => ({}), reload: () => { n.reload++; }, syncBar: () => { n.bar++; }, syncChip: () => { n.chip++; } });
const out = [], step = (rev) => { try { onState({}, rev); out.push(Object.values(n).join(" ")); } catch (e) { out.push(String(e)); } };
step({ chat: "a" });  // nothing new
step({ chat: "b" });  // the chat changed on the Mac: read again
C.streaming = {}; step({ chat: "c" });  // this page is streaming it: its own events draw it
C.streaming = null; C.page = "projects"; step({ chat: "d" });  // another page
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "needs node")
class Names(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run([NODE, "--expose-internals", "-e", SCOPE, "-", json.dumps(BROWSER)] + [os.path.join(UI, f) for f in SCRIPTS],
                           capture_output=True, text=True, timeout=120)
        if r.stdout.strip() == "no parser":
            raise unittest.SkipTest("this node has no parser of its own to lend")
        if r.returncode:
            raise AssertionError(r.stderr)
        cls.seen = json.loads(r.stdout)

    def test_no_script_uses_a_name_nothing_defines(self):
        self.assertEqual(self.seen["undefined"], [])

    def test_the_shared_objects_have_what_the_scripts_read_of_them(self):
        """The objects every page has (the kit, the chat, the dashboard, motion, Markdown); the optional hooks
        (window.HaloCousteau and the like) are checked for before each use."""
        have = {k: set(v) for k, v in self.seen["defined"].items()}
        core = [x.split(".")[1] for x in ("window.Halo", "window.HaloStudioKit", "window.HaloStudio", "window.HaloChat", "window.HaloMotion",
                                          "window.HaloMD")]
        for name in core:
            self.assertIn(name, have)
        self.assertEqual([f"{at} window.{k}.{m}" for k, m, at in self.seen["used"] if k in core and m not in have[k]], [])


@unittest.skipUnless(NODE, "needs node")
class OnState(unittest.TestCase):
    def test_a_chat_that_changed_on_the_mac_is_read_again(self):
        r = subprocess.run([NODE, "-e", ONSTATE, "-", os.path.join(UI, "studio.js"), os.path.join(UI, "halo-chat.js")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        # reload, the bar, the chip: only a new rev reloads, not while this page streams it; the bar and chip every poll
        self.assertEqual(json.loads(r.stdout), ["0 1 1", "1 2 2", "1 3 3", "1 3 3"])


if __name__ == "__main__":
    unittest.main()
