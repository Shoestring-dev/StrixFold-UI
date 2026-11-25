"""Conf resolution and validation (box/profiles.py) against the real shipped serve.conf (vendored in box/fake)."""
try:  # one temp folder per test run, removed when it ends (tests/_tmp.py)
    import _tmp  # noqa: F401
except ImportError:  # run as tests.<module> from the repo
    from tests import _tmp  # noqa: F401
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "box"))
import profiles as P  # noqa: E402

SHIPPED = open(os.path.join(ROOT, "box", "fake", "serve.conf")).read()
UNC = open(os.path.join(ROOT, "box", "fake", "serve-unc.conf")).read()
QA11 = open(os.path.join(ROOT, "box", "fake", "q-a11-on.conf")).read()


def settings(**kw):
    s = dict(P.BUILTIN[0]["settings"])
    s.update(kw)
    return s


class Validation(unittest.TestCase):
    def test_builtins_valid(self):
        for p in P.BUILTIN:  # Chat's long windows: valid where a deploy's kit serves them (1,114,112 rows: the team's)
            P.validate_settings(p["settings"], {"current", "conf", "a11df7c77f76", "8d4f195d5cfa", "500fb6efc137", "c0ed414133a9", "52d3c2b52963", "0d050c5b9b74", P.UNC_DEPLOY},
                                model_id=p["model_id"], max_rows=1114112 - P.KIT_PAD if p["settings"]["context"] > P.ROPE_ROWS else None)

    def test_every_kyojin_start_is_the_plain_model(self):
        """The official-weights gate (the lead): Kyojin's pack ships an opt-in refusal-ablation preset, so
        every profile that can start Kyojin must pass --no-uncensor, through the command halod runs (fill_up)."""
        kyojin = [p for p in P.BUILTIN if (P.model(p["model_id"]) or {}).get("engine") == "kyojin"]
        self.assertEqual(len(kyojin), 5)
        for p in kyojin:
            up = P.external_up(p)
            self.assertIn("KQ_EXTRA_ARGS=--no-uncensor ", up, p["id"])
            self.assertNotIn("{", up)  # every placeholder filled
        p = next(b for b in P.BUILTIN if b["id"] == "qwen38-kyojin-64k")
        self.assertIn("env KQ_CONTEXT=131072 KQ_SESSIONS=2 KQ_IDLE_MIN=45 KQ_EXTRA_ARGS=--no-uncensor", P.external_up(p))
        p = next(b for b in P.BUILTIN if b["id"] == "qwen38-kyojin-128k")  # one session: the pool qbench measured
        self.assertIn("env KQ_CONTEXT=131072 KQ_SESSIONS=1 ", P.external_up(p))

    def test_kyojin_pool_cap(self):
        """Kyojin's -c is window x requests at once; no profile may ask for more than 524,288."""
        for p in P.BUILTIN:
            if p["model_id"] == "qwen38-kyojin":
                self.assertLessEqual(p["settings"]["context"] * p["settings"]["streams"], 524288, p["id"])
        P.validate_settings(settings(context=131072, streams=4), {"current"}, model_id="qwen38-kyojin")
        with self.assertRaises(P.Invalid) as e:
            P.validate_settings(settings(context=262144, streams=4), {"current"}, model_id="qwen38-kyojin")
        self.assertIn("at most 2 at 262,144", str(e.exception))
        P.validate_settings(settings(context=262144, streams=4), {"current"}, model_id="qwen38-gufo21")  # per session
        self.assertTrue(P.external_up(next(b for b in P.BUILTIN if b["id"] == "qwen38-gufo21-agents")).startswith(
            "env KQ_CONTEXT=32768 KQ_SESSIONS=8 KQ_IDLE_MIN=45 "))

    def test_the_recommended_qwen_profile(self):
        b = [p for p in P.BUILTIN if p.get("recommended")]
        self.assertEqual([(p["id"], p["name"], p["proven"]) for p in b],
                         [("qwen38-halogen-agents", "Qwen 3.8 · agents (16 at once)", True)])  # Halogen
        qwen = [p["id"] for p in P.BUILTIN if p["model_id"] in ("qwen38-kyojin", "qwen38-gufo21", "qwen38-halogen")]
        self.assertEqual(qwen[:3], ["qwen38-halogen-agents", "qwen38-gufo21-agents", "qwen38-gufo21-agents-64k"])  # listed first
        self.assertTrue(P.external_up(b[0]).endswith("KQ_IDLE_MIN=45 $HOME/bin/halo-qwen-halogen up each"))
        b64 = next(p for p in P.BUILTIN if p["id"] == "qwen38-gufo21-agents-64k")  # proven by the landing smoke test;
        self.assertEqual((b64["settings"]["context"], b64["settings"]["streams"], b64["proven"], b64.get("recommended")),
                         (65536, 8, True, None))  # not the default: box2 had 14 GiB left after its read (rule: 15)
        P.validate_settings(b64["settings"], {"current"}, model_id="qwen38-gufo21")
        self.assertEqual((b[0]["settings"]["context"], b[0]["settings"]["streams"]), (32768, 8))
        P.validate_settings(b[0]["settings"], {"current"}, model_id="qwen38-halogen")

    def test_qwen_windows_are_the_engines_not_the_rope_kits(self):
        """The community Qwen engines declare their own windows (the lead: the RoPE-kit check is
        DeepSeek's): 131,072 and 262,144 on both, 524,288 on Kyojin only; DeepSeek still needs its kit."""
        for mid, ok, bad in (("qwen38-kyojin", (131072, 262144, 524288), (1048576,)),
                             ("qwen38-gufo21", (131072, 262144), (524288,))):
            for c in ok:
                n = 1 if mid == "qwen38-kyojin" else 8  # Kyojin's pool (window x streams) has its own cap
                self.assertEqual(P.validate_settings(settings(context=c, streams=n), {"current"}, model_id=mid)["context"], c)
            for c in bad:
                with self.assertRaises(P.Invalid) as e:
                    P.validate_settings(settings(context=c), {"current"}, model_id=mid)
                self.assertIn("the engine's longest window", str(e.exception))
        with self.assertRaises(P.Invalid) as e:
            P.validate_settings(settings(context=131072), {"current"}, model_id="dsv41")
        self.assertIn("RoPE kit", str(e.exception))
        b = {p["id"]: p for p in P.BUILTIN}
        self.assertEqual([b[f"qwen38-kyojin-{k}"]["settings"]["context"] for k in ("16k", "64k", "128k", "256k", "512k")],
                         [16384, 65536, 131072, 262144, 524288])
        self.assertNotIn("qwen38-gufo21-512k", b)
        # Gufo's halo-qwen (qwen38) keeps DeepSeek's stream rule; only the community engines take eight at any window
        self.assertEqual([P.stream_limit(settings(context=65536), m) for m in ("qwen38", "qwen38-kyojin", "qwen38-gufo21")], [4, 8, 8])

    def test_context_limits(self):
        with self.assertRaises(P.Invalid) as e:
            P.validate_settings(settings(context=65536))
        self.assertIn("RoPE", e.exception.errors["context"])
        with self.assertRaises(P.Invalid) as e:
            P.validate_settings(settings(context=30000))
        self.assertIn("28,672 or 30,720", e.exception.errors["context"])
        self.assertEqual(P.MAX_CONTEXT, 63488)
        P.validate_settings(settings(context=63488))
        with self.assertRaises(P.Invalid):
            P.validate_settings(settings(context=2048, max_tokens=512))

    def test_extent_rule(self):
        # U43's cases: 32 + 16,352 in a 16,384 pool is refused by the engine; so is 16,352 + 16
        self.assertFalse(P.extent_ok(32, 16352, 16384))
        self.assertFalse(P.extent_ok(16352, 16, 16384))
        self.assertTrue(P.extent_ok(16000, 366, 16384))
        self.assertFalse(P.extent_ok(16000, 367, 16384))
        self.assertEqual(P.max_prompt(63488, 4096), 59374)
        self.assertTrue(P.extent_ok(59374, 4096, 63488))
        self.assertFalse(P.extent_ok(59375, 4096, 63488))
        with self.assertRaises(P.Invalid) as e:
            P.validate_settings(settings(context=16384, max_tokens=16000))
        self.assertIn("prompt tokens", e.exception.errors["max_tokens"])

    def test_types_and_ranges(self):
        bad = {"max_tokens": 0, "prompt_cache_gib": 9, "idle_min": 3, "drafts": "maybe", "context": 1.5}
        for k, v in bad.items():
            with self.assertRaises(P.Invalid, msg=k) as e:
                P.validate_settings(settings(**{k: v}))
            self.assertIn(k, e.exception.errors)
        with self.assertRaises(P.Invalid) as e:
            P.validate_settings(dict(settings(), power="performance"))
        self.assertIn("power", e.exception.errors)  # power is display-only
        self.assertEqual(P.validate_settings(settings(idle_min=0))["idle_min"], 0)

    def test_deploy_and_memory(self):
        with self.assertRaises(P.Invalid) as e:
            P.validate_settings(settings(deploy="nope"), {"current"})
        self.assertIn("not deployed", e.exception.errors["deploy"])
        with self.assertRaises(P.Invalid) as e:
            P.validate_settings(settings(), {"current"}, {"box1": 120, "box2": 104})
        self.assertIn("box2 has 104 GiB", e.exception.errors["memory"])
        P.validate_settings(settings(), {"current"}, {"box1": 120, "box2": 111})


class Conf(unittest.TestCase):
    def test_resolve_overrides(self):
        p = P.BUILTIN[0]
        text, v = P.resolve(p, p["settings"], SHIPPED, "a11df7c77f76")
        self.assertEqual(v["CONTEXT"], "63488")
        self.assertEqual(v["DRAFTS"], "1")
        self.assertEqual(v["THINKING"], "0")
        self.assertEqual(v["MAX_TOKENS"], "4096")
        self.assertEqual(v["PROMPT_CACHE_GIB"], "2")
        self.assertEqual(v["DEPLOY_DIR"], "$HOME/tfp/deploy/a11df7c77f76")
        self.assertEqual(v["RUN_ROOT"], "$HOME/halo-control/runs")
        self.assertEqual(v["BENCH"], "0")
        self.assertEqual(v["PEAK_GIB"], "102")  # 101 at 63,488 + the prefill split's arena
        self.assertEqual(v["PREFILL_SPLIT_LARGE"], "1")
        # everything else is the shipped conf's
        ship = P.parse_conf(SHIPPED)
        for k in ("TP_STRIPE", "COLL_KIND", "GRAPHS", "PREFILL_SPLIT", "MEMORY_LIMIT_GB", "BUNDLE_SHA256", "AUX", "MASTER_IP"):
            self.assertEqual(v[k], ship[k], k)
        # each key assigned once, comments kept, a bash-sourceable file
        keys = [l.split("=", 1)[0] for l in text.splitlines() if l[:1].isupper() and "=" in l]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("# ---- the server (rank 0)", text)

    def test_current_profiles_inherit_the_qualified_costs(self):
        costs = "36.06,43.76,50.38,56.29,59.52,61.52,66.27,71.02,75.77,80.52,85.27,90.02,94.77,99.52,104.27,109.02"
        current = P.apply_conf(SHIPPED, {"ROUND_MS": costs}, tag="qualified current fixture")
        for profile in P.BUILTIN:
            if profile["model_id"] == "dsv41" and profile["settings"]["deploy"] == "current":
                _, values = P.resolve(profile, profile["settings"], current, "f5e4162cf606")
                self.assertEqual(values["ROUND_MS"], costs, profile["id"])


    def test_bash_reads_it(self):
        import subprocess
        p = next(b for b in P.BUILTIN if b["id"] == "quick-chat-16k")
        text, v = P.resolve(p, p["settings"], SHIPPED, "a11df7c77f76")
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(text)
        out = subprocess.run(["bash", "-c", f"source {f.name}; echo $CONTEXT $DRAFTS $MAX_TOKENS $PREFILL_SPLIT $PROMPT_CACHE_GIB"],
                             capture_output=True, text=True).stdout.split()
        os.unlink(f.name)
        self.assertEqual(out, ["16384", "1", "2048", "256", "0"])

    def test_split_large_in_63488_builtins(self):
        """PREFILL_SPLIT_LARGE=1 in exactly the built-in profiles at 63,488, with 1 GiB more PEAK_GIB; bash reads it."""
        import subprocess
        on = {b["id"] for b in P.BUILTIN if b.get("advanced", {}).get("PREFILL_SPLIT_LARGE") == "1"}
        self.assertEqual(on, {"agent-coding", "agent-coding-thinking", "long-documents", "unc-agent", "quick-chat", "unc-chat", "agents-2x", "unc-swarm"}
                         | {b["id"] for b in P.BUILTIN if b.get("chat_size") and not b.get("fixed")})
        self.assertEqual(on, {b["id"] for b in P.BUILTIN if b["settings"]["context"] >= 63488 and not b.get("fixed")})
        for b in P.BUILTIN:
            if b.get("fixed"):
                continue
            _, v = P.resolve(b, b["settings"], SHIPPED, "a11df7c77f76")
            extra = P.model(b["model_id"]).get("peak_extra_gib", 0)
            self.assertEqual(v.get("PREFILL_SPLIT_LARGE"), "1" if b["id"] in on else None, b["id"])
            # the table's peak, or the team's measured one when a profile carries it (higher: the chat profiles)
            self.assertEqual(int(v["PEAK_GIB"]), max(101 + extra + (1 if b["id"] in on else 0), b.get("peak_gib") or 0), b["id"])
            self.assertEqual(P.describe_limits(b["settings"], b.get("advanced"), b["model_id"], b.get("peak_gib"))["peak_gib"],
                             int(v["PEAK_GIB"]), b["id"])
        p = next(b for b in P.BUILTIN if b["id"] == "unc-agent")
        text, _ = P.resolve(p, p["settings"], SHIPPED, "8d4f195d5cfa")
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(text)
        out = subprocess.run(["bash", "-c", f"source {f.name}; echo $CONTEXT $PREFILL_SPLIT $PREFILL_SPLIT_LARGE $PEAK_GIB"],
                             capture_output=True, text=True).stdout.split()
        os.unlink(f.name)
        self.assertEqual(out, ["63488", "256", "1", "103"])
        # at 16,384 the split runs anyway: no extra memory there
        self.assertEqual(P.peak_gib(16384, 0, 1, 0, True), 101)
        # a build whose serve.conf ships PREFILL_SPLIT_LARGE=1 (766ef21): profiles without the override count it too
        shipped_on = SHIPPED.replace("PREFILL_SPLIT_ZIP=", "PREFILL_SPLIT_LARGE=1\nPREFILL_SPLIT_ZIP=", 1)
        plain = dict(P.BUILTIN[0], advanced={})
        for ctx, peak in ((32768, "102"), (16384, "101")):
            _, v = P.resolve(plain, dict(plain["settings"], context=ctx, max_tokens=2048), shipped_on, "a11df7c77f76")
            self.assertEqual((v["PREFILL_SPLIT_LARGE"], v["PEAK_GIB"]), ("1", peak), ctx)
        _, v = P.resolve(dict(plain, advanced={"PREFILL_SPLIT_LARGE": "0"}), plain["settings"], shipped_on, "a11df7c77f76")
        self.assertEqual((v["PREFILL_SPLIT_LARGE"], v["PEAK_GIB"]), ("0", "101"))

    def test_decode_boost_switch(self):
        """L2_PREFETCH: a named switch in the Advanced section, off in every built-in, 1 when a profile turns it on."""
        import subprocess
        for base in (SHIPPED, UNC):
            k = {x["key"]: x for x in P.conf_schema(base)}["L2_PREFETCH"]
            self.assertEqual((k["level"], k["group"], k["base"], k["editable"]), ("safe", "experimental", "0", True))
            self.assertEqual(k["label"], "Experimental: decode boost (L2 prefetch)")
            for words in ("About +4% decode with drafts off", "+0.3 to +3% with drafts on across two test runs",
                          "identical output in tests", "DEBUG_HIP_GRAPH_SEGMENT_SCHEDULING", "stays off until you choose"):
                self.assertIn(words, k["tested"])
        for b in P.BUILTIN:
            if not b.get("fixed"):
                _, v = P.resolve(b, b["settings"], UNC if b["model_id"] == "dsv41-unc" else SHIPPED, "8d4f195d5cfa")
                self.assertEqual(v["L2_PREFETCH"], "0", b["id"])
        keys, vals = set(P.parse_conf(SHIPPED)), P.parse_conf(SHIPPED)
        self.assertEqual(P.validate_advanced({"L2_PREFETCH": "1"}, keys, False, vals), {"L2_PREFETCH": "1"})
        with self.assertRaises(P.Invalid):
            P.validate_advanced({"L2_PREFETCH": "2"}, keys, False, vals)
        p = dict(P.BUILTIN[0], advanced={"PREFILL_SPLIT_LARGE": "1", "L2_PREFETCH": "1"})
        text, v = P.resolve(p, p["settings"], SHIPPED, "8d4f195d5cfa")
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(text)
        out = subprocess.run(["bash", "-c", f"source {f.name}; echo $L2_PREFETCH $PEAK_GIB"], capture_output=True, text=True).stdout.split()
        os.unlink(f.name)
        self.assertEqual(out, ["1", "102"])

    def test_fused_hc_switch(self):
        """TF_HC_FUSE: a switch beside the decode boost, off by default (unset or 0 everywhere), on = both ranks start
        inside /usr/bin/env TF_HC_FUSE=1 (WRAP0 / WRAP1; serve_lib starts each rank with env -i)."""
        import subprocess
        for base in (SHIPPED, UNC):
            k = {x["key"]: x for x in P.conf_schema(base)}["TF_HC_FUSE"]
            self.assertEqual((k["level"], k["group"], k["base"], k["editable"]), ("safe", "experimental", "0", True))
            self.assertEqual(k["label"], "Experimental: fused hc chain (TF_HC_FUSE)")
            self.assertEqual(k["tested"], "About +2.3% decode with drafts off; no change with drafts on; identical output "
                                          "in tests (layer gate, Spark prompts, ~6,800 served rounds).")
        for b in P.BUILTIN:  # the default: TF_HC_FUSE unset or 0, and nothing in the WRAPs
            if not b.get("fixed"):
                _, v = P.resolve(b, b["settings"], UNC if b["model_id"] == "dsv41-unc" else SHIPPED, "8d4f195d5cfa")
                self.assertIn(v.get("TF_HC_FUSE"), (None, "", "0"), b["id"])
                self.assertEqual((v["WRAP0"], v["WRAP1"]), ("/usr/local/bin/halo-inference-run",) * 2, b["id"])
        keys, vals = set(P.parse_conf(SHIPPED)), P.parse_conf(SHIPPED)
        self.assertEqual(P.validate_advanced({"TF_HC_FUSE": "1", "L2_PREFETCH": "1"}, keys, False, vals),
                         {"TF_HC_FUSE": "1", "L2_PREFETCH": "1"})
        with self.assertRaises(P.Invalid):
            P.validate_advanced({"TF_HC_FUSE": "1"}, keys, False, dict(vals, WRAP0="/opt/rocm/bin/rocprofv3 --"))
        with self.assertRaises(P.Invalid):
            P.validate_advanced({"WRAP0": "/usr/bin/env TF_HC_FUSE=1"}, keys, True, vals)  # never typed in
        p = dict(P.BUILTIN[0], advanced={"PREFILL_SPLIT_LARGE": "1", "TF_HC_FUSE": "1"})
        text, v = P.resolve(p, p["settings"], SHIPPED, "8d4f195d5cfa")
        self.assertNotIn("TF_HC_FUSE", v)
        self.assertEqual((v["WRAP0"], v["WRAP1"]), ("/usr/local/bin/halo-inference-run /usr/bin/env TF_HC_FUSE=1",) * 2)
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(text)
        out = subprocess.run(["bash", "-c", f"source {f.name}; for wv in WRAP0 WRAP1; do read -ra w <<< \"${{!wv}}\"; "
                              "env -i \"${w[@]:1}\" /usr/bin/printenv TF_HC_FUSE; done"], capture_output=True, text=True).stdout.split()
        os.unlink(f.name)
        self.assertEqual(out, ["1", "1"])

    def test_parse_value_forms(self):
        self.assertEqual(P.conf_value("'$HOME/x'   # c"), "$HOME/x")
        self.assertEqual(P.conf_value("0-5,7-21   # c"), "0-5,7-21")
        self.assertEqual(P.conf_value(""), "")


class Store(unittest.TestCase):
    def test_custom_crud(self):
        d = tempfile.mkdtemp()
        s = P.Store(os.path.join(d, "p.json"))
        c = s.create("My agent", "agent-coding", {"context": 32768, "idle_min": 90}, {"current"})
        self.assertEqual(c["settings"]["context"], 32768)
        self.assertEqual(c["advanced"], {"PREFILL_SPLIT_LARGE": "1"})  # a copy keeps the built-in's prefill split
        self.assertEqual(c["settings"]["prompt_cache_gib"], 2)
        self.assertFalse(c["builtin"])
        with self.assertRaises(P.Invalid):
            s.create("my agent", "agent-coding", {}, {"current"})  # same name
        with self.assertRaises(P.Invalid):
            s.create("Qwen", "qwen38-flash-next", {}, {"current"})  # an external engine's own settings
        with self.assertRaises(P.Invalid):
            s.update("agent-coding", settings={"context": 16384})  # built-in
        with self.assertRaises(P.Invalid):
            s.update(c["id"], settings={"context": 1000}, deploys={"current"})
        old, u = s.update(c["id"], settings={"thinking": True}, deploys={"current"})
        self.assertTrue(u["settings"]["thinking"])
        self.assertEqual(P.diff(old, u), "thinking False -> True")
        s2 = P.Store(os.path.join(d, "p.json"))
        self.assertTrue(s2.get(c["id"])["settings"]["thinking"])
        with self.assertRaises(P.Invalid):
            s.delete("quick-chat")
        s.delete(c["id"])
        self.assertIsNone(P.Store(os.path.join(d, "p.json")).get(c["id"]))

    def test_engine_adapter_field(self):
        s = P.Store(os.path.join(tempfile.mkdtemp(), "p.json"))
        for p in s.all():
            self.assertIn(p["engine"], P.ENGINES)
            self.assertIn(p["model_id"], P.MODEL_IDS)
        q = s.get("qwen38-flash-next")
        self.assertEqual(q["engine"], "gufo")
        self.assertTrue(P.model("qwen38")["external"]["up"].endswith("halo-qwen up one box1"))
        with self.assertRaises(P.Invalid):
            s.create("Qwen copy", "qwen38-flash-next", {}, {"current"})  # its settings are its engine's
        self.assertTrue(s.get("unc-agent")["installed"])  # halod's install check decides on the boxes

    def test_old_custom_profiles_load(self):
        d = tempfile.mkdtemp()
        import json
        json.dump({"profiles": [{"id": "custom-x", "name": "X", "engine": "tensorfold-dsv41", "model": P.DSV, "quant": "",
                                 "settings": {"context": 63488, "max_tokens": 4096, "drafts": True, "thinking": False,
                                              "prompt_cache_gib": 2.0, "idle_min": 5, "deploy": "current"}, "base": "agent-coding"}]},
                  open(os.path.join(d, "p.json"), "w"))
        store = P.Store(os.path.join(d, "p.json"))
        x = store.get("custom-x")
        self.assertEqual(x["model_id"], "dsv41")
        self.assertEqual(x["settings"]["streams"], 1)
        self.assertEqual(x["advanced"], {})
        P.validate_settings(x["settings"], {"current"})
        _, x = store.update("custom-x", settings={"streams": 2}, deploys={"current"})
        self.assertEqual(x["settings"]["streams"], 2)

    def test_export_import(self):
        d = tempfile.mkdtemp()
        s = P.Store(os.path.join(d, "a.json"))
        s.create("Short agent", "agent-coding", {"context": 32768}, {"current"}, {"ROUND_ROWS": "8"}, False,
                 set(P.parse_conf(SHIPPED)), P.parse_conf(SHIPPED))
        s.create("Unc short", "unc-chat", {"max_tokens": 1024}, {P.UNC_DEPLOY})
        data = s.export()
        t = P.Store(os.path.join(d, "b.json"))
        made, errors = t.import_(data, None, lambda base: (set(P.parse_conf(SHIPPED)), P.parse_conf(SHIPPED)))
        self.assertEqual(errors, {})
        self.assertEqual([m["name"] for m in made], ["Short agent", "Unc short"])
        self.assertEqual(made[0]["advanced"], {"ROUND_ROWS": "8"})
        self.assertEqual(made[1]["model_id"], "dsv41-unc")
        made2, _ = t.import_(data)  # again: names taken
        self.assertEqual(made2[0]["name"], "Short agent (imported)")
        bad = dict(data, profiles=[{"name": "Evil", "model_id": "dsv41", "base": "agent-coding", "settings": {},
                                    "advanced": {"WRAP0": "/bin/sh -c id"}},
                                   {"name": "Evil2", "model_id": "dsv41", "base": "agent-coding", "settings": {},
                                    "advanced": {"PROBE_PROMPT": "$(id)"}}])
        made3, errors = t.import_(bad)
        self.assertEqual(made3, [])
        self.assertIn("WRAP0", errors["Evil"])
        self.assertIn("PROBE_PROMPT", errors["Evil2"])
        with self.assertRaises(P.Invalid):
            t.import_({"profiles": []})


class Advanced(unittest.TestCase):
    keys, vals = set(P.parse_conf(SHIPPED)), P.parse_conf(SHIPPED)

    def test_conf_doc_help(self):
        doc = {d["key"]: d for d in P.conf_doc(SHIPPED)}
        self.assertIn("SMT sibling 22", doc["ENGINE_CPUS"]["help"])  # a continued comment
        self.assertEqual(doc["COLL_KIND"]["section"], "the ranks (rank 0 = box1, the coordinator; rank 1 = box2)")
        self.assertEqual(doc["PROBE_PROMPT"]["value"], "Answer in one sentence: what is the capital of France?")

    def test_levels(self):
        sch = {k["key"]: k for k in P.conf_schema(SHIPPED)}
        self.assertEqual(sch["ROUND_ROWS"]["level"], "safe")
        self.assertTrue(sch["ROUND_ROWS"]["editable"])
        self.assertFalse(sch["COLL_KIND"]["editable"])
        self.assertTrue({k["key"]: k for k in P.conf_schema(SHIPPED, expert=True)}["COLL_KIND"]["editable"])
        for k in ("SERVE_HOST", "SERVE_PORT", "WRAP0", "WRAP1", "TEMPERATURE", "VERIFY_REPORT", "DEPLOY_DIR", "CONTEXT",
                  "BUNDLE_SHA256", "MODEL_DIR"):
            self.assertFalse(sch[k]["editable"], k)
            self.assertFalse(P.conf_schema(SHIPPED, expert=True)[[x["key"] for x in P.conf_schema(SHIPPED)].index(k)]["editable"], k)
        self.assertNotEqual(sch["WORLD"]["level"], "unknown")
        self.assertIn("PREFILL_SPLIT_LARGE", sch)
        self.assertIn("in three rounds", sch["PREFILL_SPLIT_LARGE"]["tested"])
        self.assertIsNone(sch["ROUND_ROWS"]["tested"])

    def test_validation(self):
        ok = P.validate_advanced({"ROUND_ROWS": "8", "PROBE_EXPECT": "Paris", "TRACE_ROUNDS": "", "AUX": "attn,hc"},
                                 self.keys, False, self.vals)
        self.assertEqual(ok["ROUND_ROWS"], "8")
        cases = {"ROUND_ROWS": "17", "GRAPHS": "2", "AUX": "attn,bogus", "PROBE_PROMPT": "a'b", "ARENA_GIB": "-1",
                 "ROUND_MS": "1,x", "FIX_K": "1.5", "TP_PORT": "80", "SERVE_HOST": "0.0.0.0", "WRAP0": "/x",
                 "NOPE_KEY": "1", "TEMPERATURE": "0.7", "PROBE_EXPECT": "$(id)", "ENGINE_CPUS": "0-40"}
        for k, v in cases.items():
            with self.assertRaises(P.Invalid, msg=k) as e:
                P.validate_advanced({k: v}, self.keys, True, self.vals)
            self.assertIn(k, e.exception.errors)
        with self.assertRaises(P.Invalid) as e:  # transport needs Expert
            P.validate_advanced({"COLL_RTO_MIN_US": "9000"}, self.keys, False, self.vals)
        self.assertIn("Expert", e.exception.errors["COLL_RTO_MIN_US"])
        self.assertEqual(P.validate_advanced({"COLL_RTO_MIN_US": "9000"}, self.keys, True, self.vals), {"COLL_RTO_MIN_US": "9000"})
        with self.assertRaises(P.Invalid) as e:  # cross-key: graphs over tcp_host
            P.validate_advanced({"COLL_KIND": "host"}, self.keys, True, self.vals)
        self.assertIn("COLL_KIND=proxy", e.exception.errors["GRAPHS"])
        P.validate_advanced({"COLL_KIND": "host", "GRAPHS": "0"}, self.keys, True, self.vals)
        with self.assertRaises(P.Invalid) as e:
            P.validate_advanced({"ENGINE_CPUS": "0-31"}, self.keys, True, self.vals)
        self.assertIn("COLL_PROXY_CPU", e.exception.errors["ENGINE_CPUS"])

    def test_bash_safe(self):
        import subprocess
        p = dict(P.BUILTIN[0], advanced={"PROBE_PROMPT": "Say hi (once), please: now!", "ROUND_ROWS": "8", "TRACE_ROUNDS": ""})
        text, v = P.resolve(p, p["settings"], SHIPPED, "x")
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(text)
        out = subprocess.run(["bash", "-c", f"source {f.name}; echo \"$PROBE_PROMPT|$ROUND_ROWS|$TRACE_ROUNDS|$STREAMS|$PARALLEL\""],
                             capture_output=True, text=True).stdout.strip()
        os.unlink(f.name)
        self.assertEqual(out, "Say hi (once), please: now!|8||1|1")

    def test_uncensored_resolve(self):
        p = next(b for b in P.BUILTIN if b["id"] == "unc-agent")
        text, v = P.resolve(p, p["settings"], UNC, P.UNC_DEPLOY, deploy_root="$HOME/tfp/deploy")
        self.assertEqual(v["DEPLOY_DIR"], f"$HOME/tfp/deploy/{P.UNC_DEPLOY}")
        self.assertEqual(v["BUNDLE"], "$HOME/models/tf-port/inputs-tp2-c178a012")  # the official pack, edited
        self.assertEqual(v["BUNDLE_SHA256"], "243b984d1069b190e762b3f9eaf12c90b2a23dd1e8f33880019f60d61264e8e9")
        self.assertEqual(v["EDIT"], "$HOME/models/tf-port/unc-edit-89af9bf1.safetensors")
        self.assertEqual(v["MODEL_DIR"], "$BUNDLE/model")  # follows BUNDLE
        self.assertEqual((v["CONTEXT"], v["DRAFTS"], v["TOP_K_MAX"]), ("63488", "1", "0"))  # drafts on since U52

    def test_streams(self):
        p = dict(next(b for b in P.BUILTIN if b["id"] == "quick-chat-16k"))
        st = dict(p["settings"], streams=4)
        P.validate_settings(st)
        text, v = P.resolve(p, st, SHIPPED, "x")
        self.assertEqual((v["STREAMS"], v["PARALLEL"]), ("4", "4"))
        with self.assertRaises(P.Invalid):
            P.validate_settings(dict(st, streams=P.STREAMS_MAX + 1))
        self.assertEqual(P.describe_limits(st)["per_stream_room"], 4096)

    def test_eight_streams_require_qualified_configuration(self):
        st = settings(context=16384, prompt_cache_gib=0, streams=8)
        P.validate_settings(st, model_id="dsv41")
        for changed in (dict(context=63488), dict(context=8192), dict(prompt_cache_gib=2),
                        dict(deploy="conf"), dict(deploy="a11df7c77f76")):
            with self.assertRaises(P.Invalid) as e:
                P.validate_settings(dict(st, **changed), model_id="dsv41")
            self.assertIn("streams", e.exception.errors)
        with self.assertRaises(P.Invalid):
            P.validate_settings(st, model_id="dsv41-unc")
        store = P.Store(os.path.join(tempfile.mkdtemp(), "p.json"))
        with self.assertRaises(P.Invalid):
            store.create("Unqualified unc", "unc-chat", {"streams": 8}, {P.UNC_DEPLOY})
        unc = store.create("Unc copy", "unc-chat", {}, {P.UNC_DEPLOY})
        with self.assertRaises(P.Invalid):
            store.update(unc["id"], settings={"streams": 8}, deploys={P.UNC_DEPLOY})


class UncDeployAndDisk(unittest.TestCase):
    """The uncensored built-ins serve one deploy (0a6eb9c2abf4, StrixFold #30's edit of the official pack), the
    swarm is a 1M pool, and the pair's disk budget reaches the run conf."""

    def test_every_uncensored_builtin_serves_the_edit_deploy(self):
        m = P.model("dsv41-unc")
        self.assertEqual(m["deploy_default"], P.UNC_DEPLOY)
        self.assertEqual(m["deploys"], [P.UNC_DEPLOY])  # an older deploy ignores EDIT: it would serve the official weights
        self.assertNotIn("conf_rel", m)  # the deploy's own serve.conf
        self.assertEqual(m["conf"]["BUNDLE"], "'$HOME/models/tf-port/inputs-tp2-c178a012'")  # the official pack's bundle
        self.assertEqual((m["conf"]["EDIT"], len(m["conf"]["EDIT_SHA256"])), (f"'{P.UNC_EDIT}'", 64))
        unc = [p for p in P.BUILTIN if p["model_id"] == "dsv41-unc"]
        self.assertGreater(len(unc), 6)
        for p in unc:
            self.assertEqual(p["settings"]["deploy"], P.UNC_DEPLOY, p["id"])

    def test_the_swarm_is_a_1m_pool_of_four(self):
        p = {q["id"]: q for q in P.BUILTIN}["unc-swarm"]
        s = P.validate_settings(p["settings"], {P.UNC_DEPLOY}, mem_available_gib={"box5": 122, "box4": 122},
                                model_id="dsv41-unc", max_rows=1114112 - P.KIT_PAD)
        self.assertEqual((s["context"], s["streams"]), (1048576, 4))
        v = P.conf_values(p, s, P.UNC_DEPLOY)
        self.assertEqual((v["CONTEXT"], v["STREAMS"], v["PEAK_GIB"]), ("1048576", "4", "107"))
        with self.assertRaises(P.Invalid):  # a deploy without the long RoPE kit cannot serve it
            P.validate_settings(p["settings"], {P.UNC_DEPLOY}, model_id="dsv41-unc")

    def test_prompt_disk_keys_only_with_a_budget_and_the_prompt_cache(self):
        on = "PROMPT_CACHE_GIB=2   # x\nSERVE_PORT=18080\n"
        v = P.parse_conf(P.prompt_disk(on, 20))
        self.assertEqual((v["PROMPT_DISK_GIB"], v["PROMPT_DISK_DIR"]), ("20", "$HOME/strixfold-cache/prefix"))
        self.assertEqual(P.parse_conf(P.prompt_disk(on, 128.5, "/srv/cache"))["PROMPT_DISK_GIB"], "128.5")
        self.assertEqual(P.prompt_disk(on, 0), on)
        self.assertEqual(P.prompt_disk(on, None), on)
        off = "PROMPT_CACHE_GIB=0\n"
        self.assertEqual(P.prompt_disk(off, 20), off)
        for gib, path in ((-1, "$HOME/x"), (P.PROMPT_DISK_MAX_GIB + 1, "$HOME/x"), (20, "rel/dir"), (20, "$HOME"),
                          (20, "$HOME/a'b"), (20, "/tmp/../etc"), (20, "$HOME/x y"), (20, "")):
            with self.assertRaises(ValueError, msg=(gib, path)):
                P.prompt_disk(on, gib, path)


if __name__ == "__main__":
    unittest.main()
