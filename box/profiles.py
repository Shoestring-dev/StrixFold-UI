"""Models, profiles and their serve.conf: the built-in models and profiles, custom profiles, validation, the Advanced
knobs and conf resolution.

A model names an engine adapter, where its deploys live and what it pins in the conf (the uncensored bundle). A
profile names a model, a deploy and a few settings, plus any Advanced conf values. For the TensorFold adapter the run's
conf is the deploy's shipped tools/dsv41-hip/serve.conf with the model's, the profile's and halod's values written over
the matching KEY= lines; the result is written next to the run (RUN_DIR/serve.conf) and copied byte for byte to the
other box, because the ranks' handshake compares the conf's SHA-256. serve.conf is sourced by bash on the boxes, so
every value written into it is validated against a strict pattern here: no free text reaches it. Python 3.9 standard
library only (the Mac's python3 runs the fake backend).
"""
import copy
import json
import os
import re
import time

GIB = 1 << 30
ROPE_ROWS = 65487          # CONTEXT <= the bundle kit's RoPE rows (65,536) - 49: a deploy without its own ds-kit
KIT_PAD = 49               # serve_lib.sh: a deploy's limit is its ds-kit's rows - 49 (the engine reads pool + 49 rows)
# serve_lib.sh's rule for a deploy's rows (the team): the smallest of its four RoPE tables in ds-kit, by size
# (32 fp32 a row: 128 bytes); a deploy without them is refused by serve_lib, and halod falls back to the bundle kit's
KIT_TABLES = ("rope-plain-cos.f32", "rope-plain-sin.f32", "rope-compressed-cos.f32", "rope-compressed-sin.f32")
KIT_ROW_BYTES = 128


def kit_rows_cmd(kit_dir):
    """A command printing a ds-kit's rows by serve_lib's rule (the smallest table's size / 128), or "none"."""
    files = " ".join(f"{kit_dir}/{f}" for f in KIT_TABLES)
    return (f"python3 -c \"import os,sys; print(min(os.stat(p).st_size for p in sys.argv[1:]) // {KIT_ROW_BYTES})\" "
            f"{files} 2>/dev/null || echo none")
ALIGN = 2048               # the pool's chunk rows; the engine places a request's extent in these
EXTENT_PAD = 18            # the engine's extent rule: align(prompt + max_tokens + 18, 2048) <= pool
MAX_CONTEXT = (ROPE_ROWS // ALIGN) * ALIGN   # 63,488
MIN_CONTEXT = 4096
MEM_FLOOR_GIB = 8          # a rank starts only with MemAvailable >= PEAK_GIB + 8 (serve_lib.sh serve_idle)
CACHE_MAX_GIB = 4          # the device budget left after the model at CONTEXT 63,488 is ~6 GiB (rank-0 log)
# parallel requests (STREAMS = PARALLEL): the pool's CONTEXT positions are shared by every stream (each request takes
# an extent of prompt + max_tokens + a round's rows); see STREAMS_NOTE for what is measured
STREAMS_MAX = 8
STREAMS_NOTE = ("Up to 8 requests at once with the current official build, a 16,384-token window and prompt cache off. "
                "Other configurations support up to 4. The window is shared: a request that does not fit beside "
                "the others waits its turn. More simultaneous requests share the available throughput, so each "
                "may run more slowly.")

# A rank's peak (GTT + RSS) by CONTEXT, GiB, as serve.conf's PEAK_GIB: 101 at 8,192 is the ledger. At 63,488
# the prompt-cache test saw GTT 102.8 GiB after load and MemAvailable at least 14.2 GiB on box2, so
# the same 101 (+8 GiB floor) holds there.
PEAK_GIB = {8192: 101, 16384: 101, 63488: 101, 131072: 101, 262144: 102, 524288: 102, 1048576: 103}  # longer: the team's table
STREAM_GIB = 0             # per stream past the first: GTT 102.8-102.95 GiB at 1, 2 and 4 streams (ms n1)
# PREFILL_SPLIT_LARGE=1 keeps the prefill split above CONTEXT 16,384 (serve_lib clears it there otherwise); the team
# measured its arena at 0.62-0.68 GiB a rank at 63,488 over three rounds (fd65f28), so PEAK_GIB gets
# 1 GiB more. strix-port 766ef21 (live from 41ef468) ships it as serve.conf's default: a base conf's 1 counts too.
SPLIT_LARGE_GIB = 1
# TF_HC_FUSE=1 (strix-port d4f0a53f, the team's fused hc chain): off in every profile; the lead's wording
HC_FUSE_NOTE = ("About +2.3% decode with drafts off; no change with drafts on; identical output in tests (layer gate, "
                "Spark prompts, ~6,800 served rounds).")
# L2_PREFETCH=1 (strix-port 7dbc76b, the team's L2/MALL prefetch kernel): off in every profile until the user chooses
# (the lead's wording: the team's FROZEN_TIMING A/B gave drafts-on code +2.2% and prose +3.0%,
# the gate run +0.3% and +1.5%; drafts off 32.5 to 33.6 tok/s)
L2_PREFETCH_NOTE = ("About +4% decode with drafts off; +0.3 to +3% with drafts on across two test runs; identical "
                    "output in tests; turns on a HIP debug setting (DEBUG_HIP_GRAPH_SEGMENT_SCHEDULING), so it stays "
                    "off until you choose.")
SPLIT_LARGE_NOTE = ("Tested in three rounds (build fd65f28, drafts on) at CONTEXT 63,488 with "
                    "PREFILL_SPLIT=256: prompt reading +34% at 11,594 and 34,287 tokens (about 1,100 to 1,480 tok/s), "
                    "the same logits SHAs every round, 0.62-0.68 GiB more a rank with no growth "
                    "(MemAvailable floor 16.4 GiB). StrixFold UI adds 1 GiB to PEAK_GIB while it is 1 above 16,384. "
                    "On in the built-in 63,488-token profiles; builds from 41ef468 ship it on.")

TF = "tensorfold-dsv41"
DSV = "DeepSeek-V4.1-Flash-1017"

ENGINES = {
    TF: {"name": "TensorFold DSv4.1 (TP2 over USB4 TCP)", "installed": True},
    "gufo": {"name": "Gufo", "installed": True},
    "kyojin": {"name": "Kyojin (Yamz-Labs, ExLlamaV3 for gfx1151)", "installed": True},
    "gufo21": {"name": "Gufo v0.21.0 (gufo-org release)", "installed": True},
    "halogen": {"name": "Halogen 0.18.22 (peonist-ai, a pinned container)", "installed": True},
}

UNC_BUNDLE = "$HOME/models/tf-port/inputs-tp2-c178a012"  # the official pack: the uncensored model is its edit (EDIT below)
UNC_EDIT = "$HOME/models/tf-port/unc-edit-89af9bf1.safetensors"  # StrixFold make_edit.py of dealignai's 89af9bf1
UNC_DEPLOY = "0a6eb9c2abf4"  # the uncensored built-ins' deploy (StrixFold main: #30's TF_DS_EDIT, #31's sliced prompt reads)
MODELS = [
    {"id": "dsv41", "name": "DeepSeek V4.1 Flash", "variant": "Official weights", "engine": TF, "model": DSV,
     "quant": "EXL3 2.9 bpw", "deploy_root": "$HOME/tfp/deploy", "deploy_default": "current",
     "about": "DeepSeek's own DeepSeek-V4.1-Flash-1017 at EXL3 2.9 bpw, split over both boxes. 30-scenario "
              "tool-use eval: 57/60.",
     "facts": ["Tool eval: asked before deleting files and before moving money", "Engine: the current deploy"]},
    {"id": "dsv41-unc", "name": "DeepSeek V4.1 Flash, uncensored", "variant": "Uncensored (dealignai)", "engine": TF,
     "model": DSV + " uncensored", "quant": "EXL3 2.9 bpw", "deploy_root": "$HOME/tfp/deploy",
     # strix-port 8d4f195d: production's build at first (the stripe and the drafting
     # switch). The smoke check gave identical code/prose token and text hashes to d4f0a53f
     # (docs/results/unc-8d4f195/). d4f0a53f = 6bdf46c plus the team's fused hc chain behind TF_HC_FUSE=1 (off;
     # its default kernels are 6bdf46c's by disassembly); identical hashes to 6bdf46c (docs/results/unc-d4f0a53/).
     # 6bdf46c = 7dbc76b plus the team's hc_mix_pf rewrite (prompt path only; identical hashes to 7dbc76b,
     # docs/results/unc-6bdf46c/). 7dbc76b = cb8f97b plus the team's L2/MALL prefetch kernel (off unless
     # L2_PREFETCH=1). cb8f97b = 766ef21 (dense support, U49's 400, the U52 fix, so drafts on gives the same prompt
     # logits as off, PREFILL_SPLIT_LARGE=1 by default) plus the router tiling, the 32 MiB staging ring and the Engram
     # rows exchange. Its base conf is the deploy's own tools/dsv41-hip/unc/serve-unc.conf (the pack's BUNDLE,
     # BUNDLE_SHA256, CONTEXT 16384, PEAK_GIB 102). Builds before 1f93ac2 lack the dense weights and 1f93ac2 has U52;
     # one current deploy is offered, add later builds here. Then 500fb6efc137 was production (deploy/current on both
     # boxes; the team): the same native code and outputs (the official gates and the unc smoke hashes match) with the
     # 1,114,112-row RoPE kit in ds-kit (windows up to 1,114,063), and the uncensored conf is serve.conf plus the
     # bundle (the speed settings). Then c0ed414133a9 (strix-port 500fb6e + gpu-wait: the TP-pair crash fix, a split
     # chunk's Engram gathers in rank 0's order on both ranks; kept states give their room least recently used first;
     # TF_COLL_GPU_WAIT_S and the collectives' diagnostic lines).
     # Then 52d3c2b52963 = c0ed414133a9 plus StrixFold #5/#18 (tcp_proxy: the idle proxy threads sleep instead of
     # spinning; A/B on the pair: idle rank CPU 200% -> 3%, about 45 W less at idle, decode and outputs unchanged; same
     # kit, ds-kit and tools, only the four binaries differ).
     # Then 0d050c5b9b74 = 52d3c2b52963 plus StrixFold #15 (sf-disk: kept prompt states on each box's disk, off
     # unless PROMPT_DISK_GIB > 0, which halod writes from halod.json prompt_disk_gib; same kit and ds-kit, the four
     # binaries and serve.conf / serve_lib.sh differ).
     # Then 4f2979d61444 = StrixFold main (db516da) plus #30: dealignai's pack is the official EXL3 pack with 54
     # weights edited, so the model serves the official bundle with that edit applied at run time (TF_DS_EDIT), 22%
     # faster than the dense pack (box-c: 63.2-63.7 vs 51.8-52.0 tok/s; the official 65.4-65.8). Older deploys ignore
     # EDIT and would serve the official weights under this name, so only this one is offered; the dense pack's own
     # bundle (inputs-tp2-unc-89af9bf1) stays on the boxes. Rollback: revert this commit (0d050c5b9b74 with that
     # bundle). Then 0a6eb9c2abf4 = 4f2979d61444 plus StrixFold #31: beside answering streams a cold prompt reads in
     # 1 s slices (FILL_SLICE_MS=1000), so a 100K read leaves them waiting at most ~2 s instead of ~71 s; the same bits.
     "deploy_default": UNC_DEPLOY, "deploys": [UNC_DEPLOY],
     "bundle": UNC_BUNDLE, "peak_extra_gib": 1,
     "conf": {"BUNDLE": f"'{UNC_BUNDLE}'",
              "BUNDLE_SHA256": "243b984d1069b190e762b3f9eaf12c90b2a23dd1e8f33880019f60d61264e8e9",
              "EDIT": f"'{UNC_EDIT}'",
              "EDIT_SHA256": "17bcc5bea5af425fff4bf589219e62e997040111b4bb6aa81a86749ad33fded2"},
     "about": "dealignai's uncensored version of the same model, 2.9 bpw: refusals removed. It skips safety "
              "confirmations by design. In the tool eval it deleted a whole directory and sent money to an unknown "
              "account without asking, and made up a missing recipient and destination instead of asking. "
              "30-scenario tool-use eval: 50/60 (official 57/60).",
     "warning": "Skips safety confirmations by design. Give it read-only tools, or check every action it takes.",
     "facts": ["Tool eval: deleted and paid without asking (0/4 on safety)",
               "Engine 0a6eb9c (StrixFold #30 and #31): the official pack's weights with dealignai's edit applied as it runs, "
               "22% faster than the dense uncensored pack (one request: 63 tok/s against 52; official 65); a long new "
               "prompt no longer stops the other chats while it is read"]},
    {"id": "qwen38", "name": "Qwen3.8 Flash-Next", "variant": "Gufo engine, one box", "engine": "gufo",
     "model": "Qwen3.8 Flash-Next", "quant": "UD-Q4_K_XL", "max_context": 73728,  # halo-qwen's qwen.conf SOLO_CONTEXT
     # the team's ~/bin/halo-qwen: `up one box1` serves on box1 only, under box1's GPU lock (its node takes it);
     # box2 and the USB4 cables are untouched. `down` stops it, restores the links if needed and releases the lock.
     "external": {"up": "$HOME/bin/halo-qwen up one box1", "down": "$HOME/bin/halo-qwen down", "box": "box1",
                  "port": 18090, "state_file": "$HOME/tfp/qwen/run/state", "unit": "halo-qwen-node",
                  "log": "$HOME/tfp/qwen/run/rank.log",
                  "check": "test -x $HOME/bin/halo-qwen && . $HOME/tfp/qwen/qwen.conf && test -x \"$GUFO\" -a -r \"$MODEL\" -a -r \"$MTP\""},
     "missing": "Needs halo-qwen, the Gufo build and the model on both boxes.",
     # until the team confirms a verified `up` on these boxes; then list it in halod.json "models_ready": ["qwen38"]
     "pending": "Not verified on your boxes yet: once it has served there, list it in halod.json's models_ready.",
     # Gufo's Prometheus counters show activity: halod's idle stop works for it
     "metrics": {"path": "/metrics", "busy": "llamacpp:requests_processing", "count": "llamacpp:tokens_predicted_total"},
     "about": "A second model family, on Gufo, started and stopped through halo-qwen. One box: it "
              "serves from box1 under box1's GPU lock and leaves box2 free.",
     "facts": ["Measured on box1 (Q4 + MTP, greedy): about 43 tok/s on code and 35 on prose alone; two "
               "at once about 31 + 27, 59-60 tok/s for the box. Loads in about 13 s. Prompt reading not measured yet",
               "Endpoint 127.0.0.1:18090 on box1 (tunnelled to the same port on the Mac)",
               "Idle stop from Gufo's request counters (/metrics)"]},
    # The two community engines, one independent server per box through the team's ~/bin/halo-qwen-ENGINE (`up each`:
    # box1's lock, then box2's; both on 127.0.0.1:18090 of their box; the Mac tunnels :18090 -> box1 and :18095 ->
    # box2). The profile's window, sessions and idle minutes reach the engine through {context}, {streams} and
    # {idle_min} in `up`. The node stops itself after idle_min without a request on both boxes (state "released (idle
    # ...)" and idle_marker, which halod reads as an idle stop); halod's keep-running hold writes hold_file on each box.
    # max_context: the longest window the engine serves (no RoPE kit).
    {"id": "qwen38-kyojin", "name": "Qwen3.8 Flash-Next", "variant": "Kyojin engine, one per box", "engine": "kyojin",
     "model": "Qwen3.8 Flash-Next", "quant": "EXL3 (yamz-labs pack)", "max_context": 524288, "streams_max": 8,
     "pool_max": 524288,  # its -c is one pool for all sessions (context x streams); Kyojin's longest measured -c
     # Kyojin v1.6.0 (25eeefe1) with yamz-labs/Qwen3.8-Flash-Next-EXL3-Yamz @ 247ef39b, installed as published:
     # ~/qwen-kyojin/README.md on each box. It answers /health (200 once loaded), not /ready. Its -c is one pool for all
     # sessions; above 262,144 its 8-bit cache is used (engine.conf).
     # --no-uncensor (the plain model; the pack's opt-in refusal-ablation preset never applies): in every start command
     # and in the box's engine.conf server() too; tests/test_profiles.py fails if a Kyojin profile could start without it
     "external": {"up": "env KQ_CONTEXT={pool} KQ_SESSIONS={streams} KQ_IDLE_MIN={idle_min} KQ_EXTRA_ARGS=--no-uncensor "
                        "$HOME/bin/halo-qwen-kyojin up each",
                  "down": "$HOME/bin/halo-qwen-kyojin down", "box": "box1", "boxes": ["box1", "box2"], "port": 18090,
                  "ready_path": "/health", "state_file": "$HOME/qwen-kyojin/run/state", "unit": "halo-qwen-kyojin",
                  "log": "$HOME/qwen-kyojin/run/serve.log", "hold_file": "$HOME/qwen-kyojin/run/hold", "idle_marker": "$HOME/qwen-kyojin/run/idle-stop",
                  "dropped_file": "$HOME/qwen-kyojin/run/peers-dropped",
                  "check": "test -x $HOME/bin/halo-qwen-kyojin && . $HOME/qwen-kyojin/engine.conf && test -r \"$PACK/config.json\" "
                           "&& ls $HOME/qwen-kyojin/kyojin/exllamav3_ext*.so >/dev/null"},
     "missing": "Needs halo-qwen-kyojin, the Kyojin install and the pack on both boxes.",
     # until the lead has reviewed the team's measured run; then list it in halod.json "models_ready"
     "pending": "Not verified on your boxes yet: once it has served there, list it in halod.json's models_ready.",
     "about": "Qwen3.8 on Kyojin, the community's fastest open engine for one or two requests at a time on this chip, with "
              "Yamz Labs' EXL3 pack. One independent copy per box: both GPUs, two endpoints.",
     "facts": ["Measured (qbench, one request, greedy): about 57-61 tok/s on code and 47-50 on prose; "
               "prompt reading about 1,350-1,460 tok/s at 2K-120K; 28/30 on a fixed exact-match set",
               "Several at once add little: 62 / 58 / 64 / 98 tok/s summed for 1 / 2 / 4 / 8 at once on one box; one each "
               "on both boxes 123 in all",
               "Memory: 69-75 GiB of GTT loaded at 64K-128K pools, 43-48 GiB left",
               "Endpoint 127.0.0.1:18090 on each box (the Mac: :18090 box1, :18095 box2)"]},
    {"id": "qwen38-gufo21", "name": "Qwen3.8 Flash-Next", "variant": "Gufo v0.21, one per box", "engine": "gufo21",
     "model": "Qwen3.8 Flash-Next", "quant": "UD-Q4_K_XL", "max_context": 262144, "streams_max": 8,
     # gufo-org/gufo v0.21.0 (7110c795) built beside the team's ff6c44e5 (~/qwen-gufo21/README.md), with the same unsloth
     # 38bb39ee UD-Q4_K_XL GGUF + shared-Q8 MTP head. --context is per session and reserved at start (about 27.5 KiB a
     # token with MTP), so its sessions trade against the window.
     "external": {"up": "env KQ_CONTEXT={context} KQ_SESSIONS={streams} KQ_IDLE_MIN={idle_min} $HOME/bin/halo-qwen-gufo21 up each",
                  "down": "$HOME/bin/halo-qwen-gufo21 down", "box": "box1", "boxes": ["box1", "box2"], "port": 18090,
                  "ready_path": "/ready", "state_file": "$HOME/qwen-gufo21/run/state", "unit": "halo-qwen-gufo21",
                  "log": "$HOME/qwen-gufo21/run/serve.log", "hold_file": "$HOME/qwen-gufo21/run/hold", "idle_marker": "$HOME/qwen-gufo21/run/idle-stop",
                  "dropped_file": "$HOME/qwen-gufo21/run/peers-dropped",
                  "check": "test -x $HOME/bin/halo-qwen-gufo21 && . $HOME/qwen-gufo21/engine.conf && test -x \"$GUFO\" -a -r \"$MODEL\" -a -r \"$MTP\""},
     "missing": "Needs halo-qwen-gufo21, the Gufo v0.21.0 build and the GGUF on both boxes.",
     "pending": "Not verified on your boxes yet: once it has served there, list it in halod.json's models_ready.",
     "about": "Qwen3.8 on Gufo v0.21.0, the community engine that gains most from several requests at once. One "
              "independent copy per box: both GPUs, two endpoints.",
     "facts": ["Measured (qbench, one request, greedy): about 47 tok/s on code and 37 on prose; prompt "
               "reading about 1,410-1,690 tok/s at 2K-120K; 28/30 on a fixed exact-match set, its code and prose text the "
               "same as the team's ff6c44e5 build",
               "Scales with requests at once: 47 / 61 / 80 / 109 tok/s summed for 1 / 2 / 4 / 8 on one box; 8 on each box "
               "209 in all",
               "Memory (GiB of GTT used / left): 83 / 35 at 64K x 1 after load, 90 / 29 at 32K x 8, 95 / 23 at "
               "128K x 4 after load and 96 / 18 after a 120K read",
               "Endpoint 127.0.0.1:18090 on each box (the Mac: :18090 box1, :18095 box2)"]},
    # Halogen: Qwen 3.8's one engine from now on, a copy on each box through the same launcher (~/bin/halo-qwen-halogen,
    # ~/qwen-halogen/engine.conf).
    # A closed engine in a pinned container (localhost/halogen:0.18.22 = ghcr.io/peonist-ai/halogen-flash-server@sha256:
    # 273714cc...), its own v2 checkpoint (HF peonist-ai/halogen-qwen3.8-flash-next @ 19569737, sha256-checked); no
    # outbound connection, its API on the box's 127.0.0.1 only. {context} is per request (HALOGEN_CTX), {streams} its
    # slots; engine.conf sizes the shared KV pool to context x streams (at least 2 x context).
    {"id": "qwen38-halogen", "name": "Qwen3.8 Flash-Next", "variant": "Halogen, one per box", "engine": "halogen",
     "model": "Qwen3.8 Flash-Next", "quant": "Halogen v2 (4.16 bpw)", "max_context": 262144, "streams_max": 8,
     "external": {"up": "env KQ_CONTEXT={context} KQ_SESSIONS={streams} KQ_IDLE_MIN={idle_min} $HOME/bin/halo-qwen-halogen up each",
                  "down": "$HOME/bin/halo-qwen-halogen down", "box": "box1", "boxes": ["box1", "box2"], "port": 18090,
                  "ready_path": "/health", "state_file": "$HOME/qwen-halogen/run/state", "unit": "halo-qwen-halogen",
                  "log": "$HOME/qwen-halogen/run/serve.log", "hold_file": "$HOME/qwen-halogen/run/hold", "idle_marker": "$HOME/qwen-halogen/run/idle-stop",
                  "dropped_file": "$HOME/qwen-halogen/run/peers-dropped",
                  "check": "test -x $HOME/bin/halo-qwen-halogen && . $HOME/qwen-halogen/engine.conf && test -r \"$MODELS/qwen38-flash-next-v2.hgn\" "
                           "-a -r \"$MODELS/qwen38-flash-next-ngram.hgn\" && podman image exists \"$IMG\""},
     "missing": "Needs halo-qwen-halogen, the Halogen image and its checkpoint on both boxes.",
     "pending": "Pending: installed on both boxes; startable once listed in halod.json models_ready.",
     "about": "Qwen3.8 on Halogen, an engine written for this one chip and model: the fastest measured here for one "
              "request and for two to four at once, and level with Gufo at eight. It also reads photos. One independent "
              "copy per box: both GPUs, two endpoints.",
     "facts": ["Measured (the same bench on box1, greedy, one request): about 74 tok/s on code and 57 "
               "on prose; prompt reading about 1,620 at 2K and 1,835 at 16K; needles at 60K and 120K found (1,682 and "
               "1,598 tok/s); 28/30 on the fixed exact-match set",
               "Several at once: 74 / 88 / 96 / 108.5 tok/s summed for 1 / 2 / 4 / 8 on one box",
               "Agent turns 33K-60K tokens deep: first token in about 1-2.4 s (its prompt cache keeps the conversation)",
               "Its own quantized weights, not the official files (Agents treats it as unofficial until the user decides)",
               "Endpoint 127.0.0.1:18090 on each box (the Mac: :18090 box1, :18095 box2)"]},
]
MODEL_IDS = {m["id"] for m in MODELS}


def model(mid):
    for m in MODELS:
        if m["id"] == mid:
            return copy.deepcopy(m)
    return None


# settings a profile may carry, with their conf key and kind; only these are editable on custom profiles
FIELDS = {
    "context": ("CONTEXT", int),
    "max_tokens": ("MAX_TOKENS", int),
    "drafts": ("DRAFTS", bool),
    "thinking": ("THINKING", bool),
    "prompt_cache_gib": ("PROMPT_CACHE_GIB", float),
    "streams": ("STREAMS", int),
    "idle_min": (None, int),
    "deploy": (None, str),
    "power": (None, str),
}
EDITABLE = tuple(FIELDS)
DEFAULTS = {"streams": 1, "power": "default"}
# Power: "default" leaves the boxes' platform profile alone (balanced); "max" writes performance on both boxes while
# the profile serves (halod: after both locks and the ranks are up; balanced again on every exit path)
POWER = ("default", "max")
POWER_NOTE = ("Max speed: the boxes' performance platform profile while this profile serves. Measured on b010ee4"
              ": prompt reading +9.3%, decode +6%, at about 129 W mean (153 W peak) and 88-93 C, against "
              "about 100 W and 71-78 C balanced. Balanced again whenever it stops.")

AGENT_TRADE = ("The full 63,488-token window. The prompt cache keeps the shared start of each turn, so a later turn "
               "starts in 1-4 s instead of 15-25 s. Prefill split on (PREFILL_SPLIT_LARGE): prompts read at about "
               "1,500 tok/s. Replies default to 4,096 tokens when a request names none (send max_tokens).")
SPLIT_LARGE = {"PREFILL_SPLIT_LARGE": "1"}  # the built-in 63,488 profiles (SPLIT_LARGE_NOTE)

BUILTIN = [
    {"id": "agent-coding", "name": "Agent coding", "model_id": "dsv41",
     "use_for": "Coding agents and tool use with long sessions: omp, Cline, any OpenAI-compatible harness.",
     "tradeoffs": AGENT_TRADE + " Thinking off: answers start sooner.",
     "settings": {"context": 63488, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 2,
                  "streams": 1, "idle_min": 45, "deploy": "current"}, "advanced": SPLIT_LARGE},
    {"id": "agent-coding-thinking", "name": "Agent coding + thinking", "model_id": "dsv41",
     "use_for": "Harder agent tasks where a reasoning pass before each answer is worth the wait.",
     "tradeoffs": "As Agent coding, with thinking on: every reply spends tokens reasoning first, so replies take "
                  "longer and use more of the window.",
     "settings": {"context": 63488, "max_tokens": 4096, "drafts": True, "thinking": True, "prompt_cache_gib": 2,
                  "streams": 1, "idle_min": 45, "deploy": "current"}, "advanced": SPLIT_LARGE},
    {"id": "quick-chat", "name": "Quick chat", "model_id": "dsv41",
     "use_for": "Everyday chat, and files or long conversations in it.",
     # The user hit the 16K limit in StrixFold UI's chat, hence this: the long-documents window and cache, proven by NIAH 32/32 to
     # 59.8K tokens with memory checked; StrixFold UI's chat clamps each reply to the room left and compacts long chats
     "tradeoffs": "A 63,488-token window with the prompt cache, so a long chat does not re-read its history each turn. "
                  "Prefill split on: prompts read at about 1,500 tok/s. Replies capped at 2,048 tokens unless a "
                  "request asks for more (and at the room the window has left).",
     "settings": {"context": 63488, "max_tokens": 2048, "drafts": True, "thinking": False, "prompt_cache_gib": 2,
                  "streams": 1, "idle_min": 45, "deploy": "current"}, "advanced": SPLIT_LARGE,
     "peak_gib": 104},  # the team's measured GTT peak at 63,488 and above, rounded up
    {"id": "agents-2x", "name": "Agents, 2 at once", "model_id": "dsv41",
     "use_for": "Agent harnesses that send helpers in parallel (DeepSeek Harness on Toolbox): two requests answered at once.",
     # to a 2-at-once profile after the dsh trial, where 3 subagents queued up to 45 s on quick-chat's one stream.
     # quick-chat's window, cache and peak; STREAM_GIB 0 (GTT the same at 1, 2 and 4 streams)
     "tradeoffs": "Quick chat's 63,488-token window, shared by two requests at once (each takes room for its prompt and "
                  "reply; one that doesn't fit beside the other waits its turn), with the prompt cache. Two at once "
                  "share the throughput, so each may run a little slower than alone. Replies up to 4,096 tokens.",
     "settings": {"context": 63488, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 2,
                  "streams": 2, "idle_min": 45, "deploy": "current"}, "advanced": SPLIT_LARGE,
     "peak_gib": 104},
    {"id": "quick-chat-16k", "name": "Quick chat, 16K", "model_id": "dsv41",
     "use_for": "Short questions: the fastest start.",
     "tradeoffs": "A 16,384-token window. Prefill split on (prompts read at about 1,500 tok/s: 1,543 at 11,594 tokens). "
                  "Replies capped at 2,048 tokens unless a request asks for more. No prompt cache: it is untested "
                  "together with the prefill split.",
     "settings": {"context": 16384, "max_tokens": 2048, "drafts": True, "thinking": False, "prompt_cache_gib": 0,
                  "streams": 1, "idle_min": 45, "deploy": "current"}},
    {"id": "long-documents", "name": "Long documents", "model_id": "dsv41",
     "use_for": "Reading long files or transcripts and asking several questions about them.",
     "tradeoffs": "The full 63,488-token window with the prompt cache, so follow-up questions on the same document "
                  "skip re-reading it. Prefill split on: prompts read at about 1,500 tok/s, so a 34K-token "
                  "document takes about 23 s the first time.",
     "settings": {"context": 63488, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 2,
                  "streams": 1, "idle_min": 45, "deploy": "current"}, "advanced": SPLIT_LARGE},
    {"id": "unc-agent", "name": "Uncensored agent coding", "model_id": "dsv41-unc",
     "use_for": "Agent work where refusals get in the way. Read-only tools, or review each action.",
     "tradeoffs": "The full 63,488-token window with the prompt cache. Prefill split on: prompts read at about "
                  "1,500 tok/s. Drafts on (the same output as off since U52's fix): decode about 55 tok/s on code, "
                  "32 on prose. It does not ask before deleting files or sending anything.",
     "settings": {"context": 63488, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 2,
                  "streams": 1, "idle_min": 45, "deploy": UNC_DEPLOY}, "advanced": SPLIT_LARGE},
    {"id": "unc-chat", "name": "Uncensored chat", "model_id": "dsv41-unc",
     "use_for": "Questions the official model refuses.",
     # the team at CONTEXT 63,488 on 8d4f195d5cfa: NIAH 24/24 (17K/34K/60K), GTT peak 104.6 GiB, lowest
     # MemAvailable 15.3 GiB (floor 8), load 21 s, 60K prefill 45.8 s, a cached re-ask 0.92 s, decode at 60K 53.7
     # code / 29.7 prose; PEAK_GIB 103 (as unc-agent's)
     "tradeoffs": "A 63,488-token window with the prompt cache (checked at 60K: every needle found, a cached re-ask in "
                  "about 1 s). Drafts on (decode about 55 tok/s on code, 32 on prose; about 54 and 30 near 60K). "
                  "Replies capped at 2,048 tokens unless a request asks for more.",
     "settings": {"context": 63488, "max_tokens": 2048, "drafts": True, "thinking": False, "prompt_cache_gib": 2,
                  "streams": 1, "idle_min": 45, "deploy": UNC_DEPLOY}, "advanced": SPLIT_LARGE,
     "peak_gib": 105},  # the team: 104.6 GiB GTT peak at 60K, rounded up
    {"id": "unc-swarm", "name": "Uncensored swarm, 4 at once", "model_id": "dsv41-unc",
     "use_for": "A swarm of agents on one pair (and Chat beside them): four requests answered at once.",
     # a 1,048,576-position pool, so several long conversations keep their prompt states (two of 110K and
     # 170K taking turns at 262,144 each read 112K and 172K tokens again, 96 and 149 s; at 1M both resumed in about 4
     # s). Measured on box-b at 1M x 4: MemAvailable 15.2-15.5 GiB, decode and prompt speed as at 256K, four
     # conversations resumed their own states; PEAK_GIB 107 as unc-chat-1m's (the team's 106.0 GiB plus halod's adds);
     # STREAM_GIB 0
     "tradeoffs": "A 1,048,576-token pool shared by four requests at once, with the prompt cache: several long "
                  "conversations keep their states, so a later turn of each starts in seconds. Chats and agents keep "
                  "their own windows inside it. Four at once share the throughput, so each runs slower than alone. "
                  "Replies up to 4,096 tokens.",
     "settings": {"context": 1048576, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 2,
                  "streams": 4, "idle_min": 45, "deploy": UNC_DEPLOY}, "advanced": SPLIT_LARGE,
     "peak_gib": 107},
    {"id": "unc-chat-16k", "name": "Uncensored chat, 16K", "model_id": "dsv41-unc",
     "use_for": "Short questions the official model refuses: the fastest start.",
     "tradeoffs": "The window it was first evaluated in: 16,384 tokens. Drafts on (decode about 55 tok/s on code, "
                  "32 on prose), replies capped at 2,048 tokens unless a request asks for more.",
     "settings": {"context": 16384, "max_tokens": 2048, "drafts": True, "thinking": False, "prompt_cache_gib": 0,
                  "streams": 1, "idle_min": 45, "deploy": UNC_DEPLOY}},
    {"id": "qwen38-flash-next", "name": "Qwen3.8 on box1", "model_id": "qwen38", "fixed": True,
     "use_for": "Trying the second model family while box2 stays free.",
     "tradeoffs": "halo-qwen's one-box settings (qwen.conf: 73,728-token window, 2 requests at once, MTP drafts). "
                  "Its settings are the launcher's, not editable here. Idle stop after 45 minutes without requests.",
     "chips": ["73,728 ctx", "2 parallel", "MTP drafts on", "box1 only", "idle stop 45 min"],
     # streams: qwen.conf's SOLO_SESSIONS (2); the agent runner runs this many at once
     "settings": {"context": 63488, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 0,
                  "streams": 2, "idle_min": 45, "deploy": "current"}},
]

# Chat's longer windows. A size is offered only when the team has passed it for that model ("proven") and the deploy's
# own RoPE kit serves it (halod.kit_limit: its rows
# - 49); turning one on is its proven flag here. peak_gib: the team's measured GTT peak at that window, rounded up
# (None until measured: such a size is not offered).
LONG_SIZES = [  # (label, context, {model id: (proven, peak_gib)})
    # the team's final table, prompt cache 2 GiB. GTT peaks: 128K
    # official 103.6 / unc 104.6; 256K official 104.1 / unc 104.9 (NIAH 12/12); 512K (interpolated, proven by 1M: memory
    # only grows with the window, the user agreed to skip its run) official 104.6 / unc 105.5; 1M official 105.1 (loads
    # with 15.05 GiB MemAvailable) / unc 106.0 (3/3 needles in one 998K prompt, lowest MemAvailable 13.2 GiB).
    # peak_gib: the higher of the table plus halod's adds and the measured peak, rounded up.
    ("128K", 131072, {"dsv41": (True, 104), "dsv41-unc": (True, 105)}),
    ("256K", 262144, {"dsv41": (True, 105), "dsv41-unc": (True, 105)}),
    # 512K and 1M: the engine reports a prefill rate only when a prefill completes (strix-port
    # zig/src/core/lane_host.zig:385), so a first read this long is given time by halod's stall rule (its run's window,
    # Halod.stall_allowance) and the chat's socket wait (halo_chat.read_timeout), not the plain 240 s. Uncensored at 107
    # for both (1M's measured 106.0 plus halod's adds rounds up past 106).
    ("512K", 524288, {"dsv41": (True, 105), "dsv41-unc": (True, 107)}),
    ("1M", 1048576, {"dsv41": (True, 106), "dsv41-unc": (True, 107)}),
]
for _label, _ctx, _per in LONG_SIZES:
    for _base, _mid, _dep, _name in (("quick-chat", "dsv41", "current", "Quick chat"),
                                     ("unc-chat", "dsv41-unc", UNC_DEPLOY, "Uncensored chat")):
        _proven, _peak = _per[_mid]
        BUILTIN.append({
            "id": f"{_base}-{_label.lower()}", "name": f"{_name}, {_label}", "model_id": _mid, "chat_size": _label,
            "proven": _proven, "peak_gib": _peak,
            "use_for": "Very long files and conversations in StrixFold UI's chat.",
            "tradeoffs": (f"A {_ctx:,}-token window with the prompt cache. It needs a deploy whose RoPE kit has at least "
                          f"{_ctx + 49:,} rows."),
            "settings": {"context": _ctx, "max_tokens": 2048, "drafts": True, "thinking": False, "prompt_cache_gib": 2,
                         "streams": 1, "idle_min": 45, "deploy": _dep}, "advanced": SPLIT_LARGE})


# Qwen3.8 Flash-Next's windows on the two community engines (up to the model's native 262,144, plus
# 512K on Kyojin, experimental, through its 8-bit cache). A size is offered in the chat once the team's measured run has
# proven it: qbench (both boxes) read a needle at 60K and 120K on both engines and measured memory at each
# window x sessions below; 256K and 512K were not run. streams: requests at once on EACH box (the agent runner runs
# twice that across the two). The window is per request on both engines: Gufo's --context is per session (reserved at
# start, about 27.5 KiB a token), Kyojin's -c is one pool shared by its sessions, so Kyojin gets window x sessions
# ({pool}; qbench: two 50K prompts on a 64K pool ran one after the other, on a 128K pool together, for about 2 GiB
# more). Halogen: its trial ran one 524,288-position pool with 8 slots and read needles at 60K and 120K, so each
# window whose pool (window x streams) fits that pool is proven; 256K was not read.
QWEN_SIZES = [  # (label, context, {model id: (proven, streams)})
    ("16K", 16384, {"qwen38-kyojin": (True, 2), "qwen38-gufo21": (True, 8), "qwen38-halogen": (True, 8)}),
    ("64K", 65536, {"qwen38-kyojin": (True, 2), "qwen38-gufo21": (True, 4), "qwen38-halogen": (True, 8)}),
    ("128K", 131072, {"qwen38-kyojin": (True, 1), "qwen38-gufo21": (True, 4), "qwen38-halogen": (True, 4)}),  # Kyojin: pool 131,072, as measured
    ("256K", 262144, {"qwen38-kyojin": (False, 1), "qwen38-gufo21": (False, 2), "qwen38-halogen": (False, 2)}),  # one Kyojin session: its pool stays fp16
    ("512K", 524288, {"qwen38-kyojin": (False, 1)}),  # experimental: Kyojin's 8-bit cache past 262,144
]
# The recommended Qwen profile (the lead's pick from qbench): Gufo v0.21, a copy on each box, 8 requests of up to 32K
# each per box.
# Listed first among the community Qwen profiles. Every number in its text is qbench's.
BUILTIN.append({
    "id": "qwen38-gufo21-agents", "name": "Qwen 3.8 · agents (16 at once)", "model_id": "qwen38-gufo21",
    "fixed": True, "proven": True,  # qbench: 32K x 8 on both boxes at once, 90 GiB GTT a box; recommended earlier
    "use_for": "Recommended for agents: 16 at once, 8 on each box (Qwen3.8 on Gufo v0.21).",
    "tradeoffs": "Measured with 8 requests at once on each box: about 107 and 103 tok/s summed (209 across "
                 "both), about 13 tok/s for each request; one request alone about 47 tok/s on code and 37 on prose; 28 of "
                 "30 on a fixed exact-match set. A 32,768-token window per request; 90 GiB of GPU memory a box. Idle stop "
                 "after 45 minutes without requests on both boxes.",
    "chips": ["32K window", "8 per box", "both boxes", "idle stop 45 min"],
    "settings": {"context": 32768, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 0,
                 "streams": 8, "idle_min": 45, "deploy": "current"}})
# Longer agent windows (research requests reach about 18K; coding and long-document
# agents need more): 64K x 8 a box, the same 512K tokens a box as 128K x 4. Measured in the landing smoke test
# (the lead, through halod, both copies): ready in 20 s; 16 at once (8 a copy, about 1K-token prompts, 300
# out) 16/16, 155 tok/s in all; a 46,040-token needle read on each box found in about 30 s; GTT / MemAvailable 97 / 21
# (box1) and 97 / 20 (box2) after load, 98 / 16 and 98 / 14 after the reads. box2's 14 GiB is under the 15 GiB rule
# for the default, so it is proven and offered, and 32K x 8 stays the recommended default.
BUILTIN.append({
    "id": "qwen38-gufo21-agents-64k", "name": "Qwen 3.8 · agents, 64K (16 at once)", "model_id": "qwen38-gufo21",
    "fixed": True, "proven": True,
    "use_for": "Coding and long-document agents: 16 at once, 8 on each box, up to 64K each (Qwen3.8 on Gufo v0.21).",
    "tradeoffs": "A 65,536-token window per request, 8 at once on each box (two copies). Measured: 16 at once "
                 "ran about 155 tok/s in all; a 46K-token read took about 30 s on each box; 97-98 GiB of GPU memory a box, "
                 "with 14-16 GiB left after a 46K read (the 32K profile leaves more). Idle stop after 45 minutes without "
                 "requests on both boxes.",
    "chips": ["64K window", "8 per box", "both boxes", "idle stop 45 min"],
    "settings": {"context": 65536, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 0,
                 "streams": 8, "idle_min": 45, "deploy": "current"}})
# Halogen's agents profile, now the recommended Qwen one:
# Gufo's shape (8 requests of up to 32K on each box, both boxes), on the engine that matched Gufo at eight at once in
# the trial and beat it at one to four. Listed first among the Qwen profiles.
BUILTIN.insert(next(i for i, p in enumerate(BUILTIN) if p["id"] == "qwen38-gufo21-agents"), {
    "id": "qwen38-halogen-agents", "name": "Qwen 3.8 · agents (16 at once)", "model_id": "qwen38-halogen",
    "fixed": True, "proven": True, "recommended": True,
    "use_for": "Recommended for agents and chat: 16 at once, 8 on each box (Qwen3.8 on Halogen).",
    "tradeoffs": "Measured on one box: 108.5 tok/s summed with 8 at once (about 13.5 each), 96 with 4, 88 "
                 "with 2; one request alone about 74 tok/s on code and 57 on prose; 28 of 30 on a fixed exact-match set. "
                 "A 32,768-token window per request. Idle stop after 45 minutes without requests on both boxes.",
    "chips": ["32K window", "8 per box", "both boxes", "idle stop 45 min"],
    "settings": {"context": 32768, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 0,
                 "streams": 8, "idle_min": 45, "deploy": "current"}})
QWEN_ENGINES = (("qwen38-kyojin", "Qwen3.8, Kyojin"), ("qwen38-gufo21", "Qwen3.8, Gufo v0.21"), ("qwen38-halogen", "Qwen3.8, Halogen"))
for _label, _ctx, _per in QWEN_SIZES:
    for _mid, _name in QWEN_ENGINES:
        if _mid not in _per:
            continue
        _proven, _streams = _per[_mid]
        _exp = _ctx > 262144
        BUILTIN.append({
            "id": f"{_mid}-{_label.lower()}", "name": f"{_name}, {_label}" + (" (experimental)" if _exp else ""),
            "model_id": _mid, "fixed": True, "proven": _proven, "experimental": _exp,
            # 64K shows on the Models page; the other sizes are chosen in Chat's composer, as DeepSeek's long ones
            **({} if _label == "64K" else {"chat_size": _label}),
            "use_for": ("Very long files and conversations; experimental past the model's native window." if _exp else
                        "StrixFold UI's chat and agents on Qwen, a copy on each box."),
            "tradeoffs": (f"A {_ctx:,}-token window, {_streams} request{'s' if _streams > 1 else ''} at once on each box "
                          f"(two copies). Settings are the engine's, set by halo-qwen-{_mid.split('-')[-1]}. Idle stop "
                          "after 45 minutes without requests on both boxes."),
            "chips": [f"{_label} ctx", f"{_streams} per box", "both boxes", "idle stop 45 min"],
            "settings": {"context": _ctx, "max_tokens": 4096, "drafts": True, "thinking": False, "prompt_cache_gib": 0,
                         "streams": _streams, "idle_min": 45, "deploy": "current"}})


def fill_up(up, settings):
    """An external engine's `up` with a profile's window ({context}), Kyojin's pool ({pool} = window x sessions),
    sessions a box ({streams}) and idle minutes ({idle_min})."""
    st = settings or {}
    ctx, n = int(st.get("context") or 0), int(st.get("streams") or 1)
    return (up.replace("{context}", str(ctx)).replace("{pool}", str(ctx * n)).replace("{streams}", str(n))
            .replace("{idle_min}", str(int(st.get("idle_min") or 0))))


def external_up(p):
    """The `up` command halod runs for a profile of an external engine (its model's spec, filled by fill_up)."""
    return fill_up(((model(p["model_id"]) or {}).get("external") or {}).get("up", ""), p.get("settings"))


def model_window(mid):
    """The longest window a community Qwen engine serves (its model's max_context), or None for DeepSeek, whose limit
    is its deploy's RoPE kit (halod.kit_limit)."""
    m = model(mid) or {}
    return int(m["max_context"]) if m.get("max_context") else None


class Invalid(ValueError):
    """A profile or setting refused, with a plain message per field."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


def extent_ok(prompt, max_tokens, pool):
    """The engine's rule for one request: its extent, rounded up to the chunk rows, fits the pool."""
    need = prompt + max_tokens + EXTENT_PAD
    return ((need + ALIGN - 1) // ALIGN) * ALIGN <= pool


def max_prompt(context, max_tokens):
    """The longest prompt a request with this max_tokens can send at this pool (extent rule)."""
    return context - max_tokens - EXTENT_PAD


def peak_gib(context, cache_gib, streams=1, arena_gib=0, split_large=False):
    """PEAK_GIB for a run: the nearest measured context at or above this one (else the largest), plus any cache
    budget past 2 GiB (the 2 GiB test held 6 states in 38 MiB; GTT after load was the same with and without it), the
    streams past the first, a fixed arena and the prefill split's arena above 16,384."""
    known = sorted(PEAK_GIB)
    at = next((c for c in known if c >= context), known[-1])
    extra = max(0.0, (cache_gib or 0) - 2) + STREAM_GIB * max(0, (streams or 1) - 1) + (arena_gib or 0)
    if split_large and context > 16384:
        extra += SPLIT_LARGE_GIB
    return int(PEAK_GIB[at] + extra + 0.999)


def _coerce(name, value):
    kind = FIELDS[name][1]
    if kind is bool:
        if isinstance(value, bool):
            return value
        if value in (0, 1, "0", "1", "true", "false"):
            return value in (1, "1", "true")
        raise ValueError("must be on or off")
    if kind is int:
        if isinstance(value, bool):
            raise ValueError("must be a whole number")
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a whole number")
        if f != int(f):
            raise ValueError("must be a whole number")
        return int(f)
    if kind is float:
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError("must be a number")
        return f
    if name == "power":
        if value not in POWER:
            raise ValueError("Default or Max speed")
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise ValueError("must be a deploy name")
    return value


def stream_limit(settings, model_id="dsv41"):
    """Five to eight streams use the current official 16K, cache-off configuration; a model with its own streams_max
    (the community Qwen engines: eight at any window) is held to that."""
    own = (model(model_id) or {}).get("streams_max")
    if own:
        return int(own)
    if (model_id == "dsv41" and settings["deploy"] == "current" and
            settings["context"] == 16384 and settings["prompt_cache_gib"] == 0):
        return STREAMS_MAX
    return 4


def validate_settings(settings, deploys=None, mem_available_gib=None, model_id="dsv41", max_rows=None):
    """Checked settings (coerced), or Invalid with one plain message per bad field. deploys: the names present on
    both boxes (None: not checked). mem_available_gib: {box: GiB} now (None: not checked). max_rows: the longest
    window the deploy's RoPE kit serves (halod.kit_limit: its rows - 49; None: the bundle kit's 65,487); a model with
    its own max_context (the community Qwen engines) is held to that instead."""
    out, errors = {}, {}
    settings = dict(DEFAULTS, **(settings or {}))
    for name in FIELDS:
        if name not in settings:
            errors[name] = "missing"
            continue
        try:
            out[name] = _coerce(name, settings[name])
        except ValueError as e:
            errors[name] = str(e)
    for name in settings:
        if name not in FIELDS:
            errors[name] = "not a setting a profile can change"
    if errors:
        raise Invalid(errors)
    c, m = out["context"], out["max_tokens"]
    own = model_window(model_id)  # a Qwen engine's own longest window; DeepSeek's is its deploy's RoPE kit
    limit = own or int(max_rows or ROPE_ROWS)
    top = limit // ALIGN * ALIGN
    pool = (model(model_id) or {}).get("pool_max")
    if pool and "streams" in out and c * out["streams"] > pool:  # Kyojin: one cache for all sessions, sized window x streams
        errors["streams"] = (f"window x requests at once at most {pool:,} tokens (the engine's one cache for all "
                             f"of them): at most {max(1, pool // c)} at {c:,}")
    if c > limit:
        errors["context"] = (f"at most {limit:,} (the engine's longest window)" if own else
                             f"at most {top:,} (this deploy's RoPE kit serves {limit:,} positions)")
    elif c % ALIGN:
        lo, hi = c // ALIGN * ALIGN, (c // ALIGN + 1) * ALIGN
        errors["context"] = f"must be a multiple of {ALIGN:,} (the pool's chunk rows): {lo:,} or {min(hi, top):,}"
    elif c < MIN_CONTEXT:
        errors["context"] = f"at least {MIN_CONTEXT:,}"
    if m < 1:
        errors["max_tokens"] = "at least 1"
    elif "context" not in errors and max_prompt(c, m) < 1024:
        errors["max_tokens"] = (f"leaves {max(0, max_prompt(c, m)):,} prompt tokens in a {c:,} window (the engine needs "
                                f"prompt + max_tokens + {EXTENT_PAD} to fit); at most {c - EXTENT_PAD - 1024:,}")
    g = out["prompt_cache_gib"]
    if g < 0 or g > CACHE_MAX_GIB:
        errors["prompt_cache_gib"] = f"0 to {CACHE_MAX_GIB} GiB (about 6 GiB of device memory is left after the model)"
    n = out["streams"]
    limit = stream_limit(out, model_id)
    if not 1 <= n <= limit:
        errors["streams"] = (f"1 to {limit}" if limit == STREAMS_MAX else
                             "1 to 4; 5 to 8 require the current official build, a 16,384-token window and prompt cache off")
    i = out["idle_min"]
    if i != 0 and not 5 <= i <= 1440:
        errors["idle_min"] = "0 (never stop) or 5 to 1,440 minutes"
    if deploys is not None and out["deploy"] not in deploys:
        errors["deploy"] = f"not deployed on both boxes ({', '.join(sorted(deploys)) or 'none found'})"
    if mem_available_gib and "context" not in errors and "prompt_cache_gib" not in errors:
        need = peak_gib(c, g, n) + MEM_FLOOR_GIB
        short = {b: a for b, a in mem_available_gib.items() if a is not None and a < need}
        if short:
            errors["memory"] = "needs {} GiB free on each box; {}".format(
                need, ", ".join(f"{b} has {a:.0f} GiB" for b, a in sorted(short.items())))
    if errors:
        raise Invalid(errors)
    return out


def describe_limits(settings, advanced=None, model_id=None, measured_peak=None):
    c, m = settings["context"], settings["max_tokens"]
    arena = _int_or0((advanced or {}).get("ARENA_GIB"))
    split = (advanced or {}).get("PREFILL_SPLIT_LARGE") == "1"
    peak = max(peak_gib(c, settings.get("prompt_cache_gib", 0), settings.get("streams", 1), arena, split) +
               (model(model_id) or {}).get("peak_extra_gib", 0), int(measured_peak or 0))
    n = settings.get("streams", 1)
    return {"max_prompt_default": max_prompt(c, m), "peak_gib": peak, "mem_needed_gib": peak + MEM_FLOOR_GIB,
            "streams": n, "per_stream_room": c // n if n > 1 else c}


def _int_or0(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- the conf file

_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_SECTION = re.compile(r"^# ----\s*(.+?)\s*$")


def conf_value(raw):
    """A conf line's value: up to an unquoted #, quotes removed (as bash reads a simple assignment)."""
    raw = raw.strip()
    if raw.startswith("'"):
        end = raw.find("'", 1)
        return raw[1:end] if end > 0 else raw[1:]
    if raw.startswith('"'):
        end = raw.find('"', 1)
        return raw[1:end] if end > 0 else raw[1:]
    return raw.split("#", 1)[0].strip()


def _comment(raw):
    """The trailing comment of a conf line's value part (after an unquoted #), or ''."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        rest = raw[end + 1:] if end > 0 else ""
    else:
        rest = raw
    i = rest.find("#")
    return rest[i + 1:].strip() if i >= 0 else ""


def parse_conf(text):
    out = {}
    for line in text.splitlines():
        mt = _LINE.match(line)
        if mt:
            out[mt.group(1)] = conf_value(mt.group(2))
    return out


def conf_doc(text):
    """[{key, value, help, section}] in file order: each KEY=VALUE line, its comment and the indented comment lines
    that continue it, under the latest '# ---- section' heading."""
    out, section, last = [], "", None
    for line in text.splitlines():
        sm = _SECTION.match(line)
        if sm:
            section, last = sm.group(1), None
            continue
        mt = _LINE.match(line)
        if mt:
            last = {"key": mt.group(1), "value": conf_value(mt.group(2)), "help": _comment(mt.group(2)), "section": section}
            out.append(last)
            continue
        s = line.strip()
        if last is not None and line[:1].isspace() and s.startswith("#"):
            last["help"] = (last["help"] + " " + s.lstrip("#").strip()).strip()
        else:
            last = None
    return out


def apply_conf(text, values, tag="halo-control"):
    """text with each KEY's assignment replaced (the old comment dropped, a short tag kept); keys not in text are
    appended. Values are written as given (callers quote paths)."""
    seen, lines = set(), []
    for line in text.splitlines():
        mt = _LINE.match(line)
        if mt and mt.group(1) in values:
            k = mt.group(1)
            seen.add(k)
            line = f"{k}={values[k]}   # {tag}"
        lines.append(line)
    extra = [f"{k}={v}   # {tag}" for k, v in values.items() if k not in seen]
    if extra:
        lines += ["", f"# ---- added by {tag}"] + extra
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- the Advanced knobs
# Every KEY of serve.conf has a level. "safe": editable on a custom profile; "expert": editable only with the profile's
# expert switch on (transport and lock); "profile": set by the profile's main fields; "model": set by the model;
# "halod": written by halod for every run; "fixed": shown, never changed (the endpoint's address, commands run on the
# boxes, the inputs' checks). A key StrixFold UI does not know is shown and never changed.

_INT = r"-?[0-9]+"
_IP = r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
AUX_PARTS = ("attn", "index", "hc", "gate", "idxw", "rope")


def _rule_int(lo, hi, empty=False):
    return ("int", lo, hi, empty)


def _rule_enum(*vals):
    return ("enum", vals)


KNOBS = {
    # transport (expert)
    "MASTER_IP": ("expert", ("ip", False), "transport"),
    "TP_PORT": ("expert", _rule_int(1024, 65535), "transport"),
    "TRANSPORT": ("fixed", None, "transport"),
    "TP_DATA_PORT": ("expert", ("int", 1024, 65535, True), "transport"),
    "TP_STRIPE": ("expert", ("ip", True), "transport"),
    "COLL_STRIPE_MIB": ("expert", _rule_int(1, 64), "transport"),
    "COLL_RTO_MIN_US": ("expert", _rule_int(1000, 200000), "transport"),
    "COLL_PROGRESS_MS": ("expert", _rule_int(500, 60000), "transport"),
    "COLL_STAGING": ("expert", _rule_enum("blit", "dma"), "transport"),
    "COLL_KIND": ("expert", _rule_enum("proxy", "host"), "transport"),
    "COLL_PROXY_CPU": ("expert", _rule_int(0, 31), "transport"),
    "ENGINE_CPUS": ("expert", ("cpus",), "transport"),
    "COLL_SLOT_KIB": ("expert", _rule_int(1024, 65536), "transport"),
    "MEMORY_LIMIT_GB": ("expert", _rule_int(100, 120), "transport"),
    # lock (expert): halod's own lock protocol reads these two
    "LOCK_WAIT_S": ("expert", _rule_int(60, 3 * 3600), "lock"),
    "LOCK_PAIR_S": ("expert", _rule_int(10, 600), "lock"),
    # the engine (safe)
    "ROUND_ROWS": ("safe", _rule_int(1, 16), "engine"),
    "GRAPHS": ("safe", _rule_enum("0", "1"), "engine"),
    "GRAPHS_EAGER": ("safe", ("int", 1, 100000, True), "engine"),
    "ROUND_MS": ("safe", ("floats", 16), "engine"),
    "DRAFT_MS": ("safe", ("float", 0, 100, True), "engine"),
    "FIX_K": ("safe", ("int", 1, 15, True), "engine"),
    "FROZEN_TIMING": ("safe", ("enum", ("", "0", "1")), "engine"),
    "TRACE_ROUNDS": ("safe", ("enum", ("", "0", "1", "2")), "engine"),
    "ARENA_GIB": ("safe", _rule_int(0, 8), "engine"),
    "PREFILL_SPLIT": ("safe", ("int", 0, 2048, True), "engine"),
    "PREFILL_SPLIT_ZIP": ("safe", ("enum", ("", "0", "1")), "engine"),
    "PREFILL_SPLIT_LARGE": ("safe", ("enum", ("", "0", "1")), "engine"),
    "LOGITS_SHA": ("safe", ("enum", ("", "0", "1")), "engine"),
    "L2_PREFETCH": ("safe", _rule_enum("0", "1"), "experimental"),
    # not a serve.conf key: an environment variable both ranks start with, through WRAP0 / WRAP1 (WRAP_ENV)
    "TF_HC_FUSE": ("safe", _rule_enum("0", "1"), "experimental"),
    "GPU_STRICT": ("safe", _rule_enum("0", "1"), "engine"),
    "AUX": ("safe", ("aux",), "engine"),
    "REPLAY_FLOOR": ("safe", _rule_enum("0", "1"), "engine"),
    "HC_SIDE": ("safe", _rule_enum("0", "1"), "engine"),
    "ENGRAM_AIO": ("safe", _rule_enum("0", "1"), "engine"),
    "TOP_K_MAX": ("safe", _rule_int(0, 56), "server"),
    "SEED_RANDOM": ("safe", _rule_enum("0", "1"), "server"),
    "PROBE_PROMPT": ("safe", ("text", 200), "server"),
    "PROBE_EXPECT": ("safe", ("text", 40), "server"),
    "PROBE_MAX_TOKENS": ("safe", _rule_int(4, 256), "server"),
    "STOP_TIMEOUT_S": ("safe", _rule_int(10, 240), "server"),
    # set elsewhere
    "CONTEXT": ("profile", None, "engine"), "MAX_TOKENS": ("profile", None, "server"),
    "DRAFTS": ("profile", None, "engine"), "THINKING": ("profile", None, "server"),
    "PROMPT_CACHE_GIB": ("profile", None, "server"), "STREAMS": ("profile", None, "engine"),
    "PARALLEL": ("profile", None, "engine"),
    "BUNDLE": ("model", None, "model"), "BUNDLE_SHA256": ("model", None, "model"),
    "EDIT": ("model", None, "model"), "EDIT_SHA256": ("model", None, "model"),
    "DEPLOY_DIR": ("halod", None, "build"), "PEAK_GIB": ("halod", None, "engine"), "RUN_ROOT": ("halod", None, "run"),
    "BENCH": ("halod", None, "run"),
}
LEVEL_NOTE = {
    "profile": "Set by the profile's main settings above.",
    "model": "Set by the model.",
    "halod": "Written by StrixFold UI for every run.",
    "fixed": "Shown, never changed by StrixFold UI.",
    "unknown": "Not known to StrixFold UI: shown, never changed.",
}
KNOB_TESTED = {"PREFILL_SPLIT_LARGE": SPLIT_LARGE_NOTE, "L2_PREFETCH": L2_PREFETCH_NOTE,  # team measurements, beside
               "TF_HC_FUSE": HC_FUSE_NOTE}                                                   # the conf's help
# a plain name and an on/off switch in the Advanced section, for the settings the user may want to try
KNOB_LABEL = {"L2_PREFETCH": "Experimental: decode boost (L2 prefetch)",
              "TF_HC_FUSE": "Experimental: fused hc chain (TF_HC_FUSE)"}
# switches that are environment variables of the engine, not serve.conf keys: serve_lib starts each rank with env -i
# and its own list, so the one way in is WRAP0 / WRAP1 (each rank's command runs inside them). On, both become
# `/usr/bin/env KEY=1` (the team's kd_session.sh does the same); off, they stay as the base conf has them (empty).
WRAP_ENV = {"TF_HC_FUSE": "1 starts both ranks with TF_HC_FUSE=1 (WRAP0 and WRAP1 = /usr/bin/env TF_HC_FUSE=1): "
                          "The team's fused hc chain, in strix-port d4f0a53f and later (earlier builds ignore it). Empty "
                          "or 0: the default kernels, identical to 6bdf46c's."}
FIXED_WHY = {
    "SERVE_HOST": "The model listens on 127.0.0.1 only; the Mac reaches it through an SSH tunnel.",
    "SERVE_PORT": "The tunnel and every client setting expect 18080.",
    "WRAP0": "A command run on the box: never typed in the dashboard; the fused hc switch sets /usr/bin/env TF_HC_FUSE=1.",
    "WRAP1": "A command run on the box: never typed in the dashboard; the fused hc switch sets /usr/bin/env TF_HC_FUSE=1.",
    "TEMPERATURE": "Kept at 0 (greedy). On this build a sampled request without top_k fails with HTTP 500.",
    "VERIFY_REPORT": "Kept on: a rank starts only from inputs that passed verify_inputs.sh.",
    "LOCK_TRIES": "StrixFold UI waits up to 3 hours in all instead of counting tries.",
    "WORLD": "Two ranks, one per box.", "RANK0_HOST": "box1 is rank 0 (the server).", "RANK1_HOST": "box2 is rank 1.",
    "KIT_SUBDIR": "Part of the deploy.", "MODEL_DIR": "Part of the model's bundle.", "RANK_CACHE": "Part of the model's bundle.",
    "DS_KIT": "Part of the model's bundle.", "ENGRAM_DIR": "Part of the model's inputs.", "TOKEN_MAP": "Part of the model's bundle.",
    "CHUNK_ROWS": "Fixed in the engine.", "MODALITIES": "This port serves text only.", "HIP_DEVICE": "Each box has one GPU.",
    "SERVE_NAME": "Clients name the model dsv41.",
    "READY_TIMEOUT_S": "first_light.sh's; StrixFold UI has its own: loading 15 min, linking 5 min, first request 4 min.",
    "PROBE_TIMEOUT_S": "first_light.sh's; StrixFold UI gives the first request 4 minutes.",
    "BENCH_CASES": "first_light.sh's timed run; StrixFold UI runs none.", "BENCH_REPEAT": "first_light.sh's timed run.",
    "BENCH_MAX_TOKENS": "first_light.sh's timed run.",
}


def knob_level(key):
    return (KNOBS.get(key) or ("unknown" if key not in FIXED_WHY else "fixed", None, None))[0]


def describe_knob(key):
    lvl, rule, _ = KNOBS.get(key) or ("unknown", None, None)
    if key in FIXED_WHY:
        lvl = "fixed"
    if rule is None:
        return lvl, FIXED_WHY.get(key) or LEVEL_NOTE.get(lvl, ""), None
    kind = rule[0]
    if kind == "int":
        hint = f"whole number {rule[1]:,} to {rule[2]:,}" + (", or empty" if rule[3] else "")
    elif kind == "float":
        hint = f"number {rule[1]} to {rule[2]}" + (", or empty" if rule[3] else "")
    elif kind == "enum":
        hint = "one of " + ", ".join(repr(v) if v else "empty" for v in rule[1])
    elif kind == "ip":
        hint = "an IPv4 address" + (", or empty" if rule[1] else "")
    elif kind == "cpus":
        hint = "CPU list like 0-5,7-21 (0-31), or empty"
    elif kind == "floats":
        hint = f"comma-separated ms values (1 to {rule[1]} of them), or empty"
    elif kind == "aux":
        hint = "hip, triton, or a list of " + ",".join(AUX_PARTS)
    else:
        hint = f"letters, digits, spaces and . , ? ! : ; ( ) - only, up to {rule[1]} characters"
    return lvl, hint, kind


def check_knob(key, value):
    """The value normalized (as stored on a profile; conf_literal quotes it for the conf), or ValueError with a plain
    message."""
    lvl, rule, _ = KNOBS.get(key) or ("unknown", None, None)
    if rule is None or key in FIXED_WHY:
        raise ValueError(FIXED_WHY.get(key) or LEVEL_NOTE.get(lvl, "not editable"))
    v = "" if value is None else str(value).strip()
    kind = rule[0]
    if kind == "int":
        if v == "" and rule[3]:
            return ""
        if not re.fullmatch(_INT, v) or not rule[1] <= int(v) <= rule[2]:
            raise ValueError(f"a whole number from {rule[1]:,} to {rule[2]:,}" + (" (or empty)" if rule[3] else ""))
        return str(int(v))
    if kind == "float":
        if v == "" and rule[3]:
            return ""
        try:
            f = float(v)
        except ValueError:
            f = None
        if f is None or not re.fullmatch(r"[0-9]+(\.[0-9]+)?", v) or not rule[1] <= f <= rule[2]:
            raise ValueError(f"a number from {rule[1]} to {rule[2]}" + (" (or empty)" if rule[3] else ""))
        return v
    if kind == "enum":
        if v not in rule[1]:
            raise ValueError("one of " + ", ".join(repr(x) if x else "empty" for x in rule[1]))
        return v
    if kind == "ip":
        if v == "" and rule[1]:
            return ""
        if not re.fullmatch(_IP, v) or any(int(p) > 255 for p in v.split(".")):
            raise ValueError("an IPv4 address like 10.40.0.1")
        return v
    if kind == "cpus":
        if v == "":
            return ""
        if not re.fullmatch(r"[0-9]{1,2}(-[0-9]{1,2})?(,[0-9]{1,2}(-[0-9]{1,2})?)*", v):
            raise ValueError("a CPU list like 0-5,7-21")
        for part in v.split(","):
            a, _, b = part.partition("-")
            if int(a) > 31 or (b and (int(b) > 31 or int(b) < int(a))):
                raise ValueError("CPUs 0 to 31, ranges low-high")
        return v
    if kind == "floats":
        if v == "":
            return ""
        parts = v.split(",")
        if len(parts) > rule[1] or not all(re.fullmatch(r"[0-9]+(\.[0-9]+)?", p) and 0 < float(p) < 10000 for p in parts):
            raise ValueError(f"1 to {rule[1]} comma-separated ms values, each above 0")
        return v
    if kind == "aux":
        if v in ("hip", "triton"):
            return v
        parts = v.split(",")
        if not v or not all(p in AUX_PARTS for p in parts) or len(set(parts)) != len(parts):
            raise ValueError("hip, triton, or a list of " + ",".join(AUX_PARTS))
        return v
    if kind == "text":
        if not re.fullmatch(r"[A-Za-z0-9 .,?!:;()-]{1,%d}" % rule[1], v):
            raise ValueError(f"letters, digits, spaces and . , ? ! : ; ( ) - only, 1 to {rule[1]} characters")
        return v
    raise ValueError("not editable")


def conf_literal(key, value):
    """A checked value as written into serve.conf: text in single quotes (its characters exclude quotes and $)."""
    rule = (KNOBS.get(key) or (None, None, None))[1]
    return f"'{value}'" if rule and rule[0] == "text" else value


def validate_advanced(adv, base_keys=None, expert=False, merged=None):
    """{KEY: conf value} checked, or Invalid {KEY: message}. base_keys: the keys the base conf has (None: not
    checked; PREFILL_SPLIT_LARGE may be added to any). merged: the base conf's values, for the cross-key checks."""
    out, errors = {}, {}
    if not isinstance(adv, dict):
        raise Invalid({"advanced": "must be an object of KEY: value"})
    for k, v in adv.items():
        if not isinstance(k, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,40}", k):
            errors[str(k)[:40]] = "not a serve.conf key"
            continue
        lvl = knob_level(k)
        if base_keys is not None and k not in base_keys and k != "PREFILL_SPLIT_LARGE" and k not in WRAP_ENV:
            errors[k] = "this model's serve.conf has no such key"
            continue
        if lvl == "expert" and not expert:
            errors[k] = "a transport or lock setting: turn on Expert to change it"
            continue
        try:
            out[k] = check_knob(k, v)
        except ValueError as e:
            errors[k] = str(e)
    if merged is not None and not errors:
        m = dict(merged)
        m.update(out)
        graphs, kind = m.get("GRAPHS", "0"), m.get("COLL_KIND", "proxy")
        if graphs not in ("", "0") and m.get("TRANSPORT", "tcp") == "tcp" and kind != "proxy":
            errors["GRAPHS"] = "GRAPHS=1 over tcp needs COLL_KIND=proxy"
        try:
            rows, slot = int(m.get("ROUND_ROWS") or 16), int(m.get("COLL_SLOT_KIB") or 4352)
        except ValueError:
            rows, slot = 16, 4352
        if graphs not in ("", "0") and kind == "proxy" and rows * 64640 * 4 > slot * 1024:
            errors["ROUND_ROWS"] = (f"with GRAPHS=1 the head gather ({rows * 64640 * 4:,} B) must fit COLL_SLOT_KIB "
                                    f"({slot * 1024:,} B)")
        for k in WRAP_ENV:
            if m.get(k) == "1" and (m.get("WRAP0") or m.get("WRAP1")):
                errors[k] = "WRAP0 or WRAP1 is set in the base conf; this switch needs both empty"
        try:
            if m.get("COLL_PROXY_CPU") and m.get("ENGINE_CPUS") and _in_cpus(int(m["COLL_PROXY_CPU"]), m["ENGINE_CPUS"]):
                errors["ENGINE_CPUS"] = "must leave out COLL_PROXY_CPU's core (and its SMT sibling)"
        except ValueError:
            pass
    if errors:
        raise Invalid(errors)
    return out


def _in_cpus(cpu, spec):
    for part in spec.split(","):
        a, _, b = part.partition("-")
        if int(a) <= cpu <= int(b or a):
            return True
    return False


def conf_schema(base_text, advanced=None, expert=False):
    """The Advanced section for one profile: every key of its base conf with value, override, level, help, hint."""
    advanced = advanced or {}
    out = []
    doc = conf_doc(base_text)
    keys = {d["key"] for d in doc}
    if "WRAP0" in keys and "WRAP1" in keys:
        for k, why in WRAP_ENV.items():
            if k not in keys:
                doc.append({"key": k, "value": "0", "section": "the engine", "help": why})
    if "PREFILL_SPLIT" in keys and "PREFILL_SPLIT_LARGE" not in keys:
        doc.append({"key": "PREFILL_SPLIT_LARGE", "value": "", "section": "the engine",
                    "help": "1 keeps PREFILL_SPLIT above CONTEXT 16,384 (serve_lib clears it there otherwise)"})
    for d in doc:
        lvl, hint, kind = describe_knob(d["key"])
        group = (KNOBS.get(d["key"]) or (None, None, None))[2] or "other"
        ov = advanced.get(d["key"])
        out.append({"key": d["key"], "base": d["value"], "value": ov if ov is not None else d["value"],
                    "override": ov is not None, "level": lvl, "editable": lvl == "safe" or (lvl == "expert" and expert),
                    "group": group, "section": d["section"], "help": d["help"], "tested": KNOB_TESTED.get(d["key"]),
                    "label": KNOB_LABEL.get(d["key"]),
                    "hint": hint, "kind": kind,
                    "note": FIXED_WHY.get(d["key"]) or LEVEL_NOTE.get(lvl)})
    return out


def conf_values(profile, settings, deploy_name, run_root="$HOME/halo-control/runs", deploy_root="$HOME/tfp/deploy",
                base_split=""):
    """The conf keys a run of this profile writes over the shipped serve.conf: the model's, the profile's Advanced
    values, then the main settings and halod's own."""
    m = model(profile.get("model_id") or "dsv41") or {}
    v = dict(m.get("conf", {}))
    v.update(profile.get("conf", {}))
    adv = profile.get("advanced", {})
    v.update({k: conf_literal(k, x) for k, x in adv.items() if k not in WRAP_ENV})
    on = [k for k in WRAP_ENV if adv.get(k) == "1"]
    # Fail closed: the root-managed wrapper enters the network-restricted cgroup before either rank starts.
    wrap = "/usr/local/bin/halo-inference-run"
    if on:
        wrap += " /usr/bin/env " + " ".join(f"{k}=1" for k in on)
    v["WRAP0"] = v["WRAP1"] = "'" + wrap + "'"
    n = settings.get("streams", 1)
    arena = _int_or0(profile.get("advanced", {}).get("ARENA_GIB"))
    split = profile.get("advanced", {}).get("PREFILL_SPLIT_LARGE", base_split) == "1"
    v.update({
        "DEPLOY_DIR": f"'{deploy_root}/{deploy_name}'",
        "CONTEXT": str(settings["context"]),
        "MAX_TOKENS": str(settings["max_tokens"]),
        "DRAFTS": "1" if settings["drafts"] else "0",
        "THINKING": "1" if settings["thinking"] else "0",
        "PROMPT_CACHE_GIB": _num(settings["prompt_cache_gib"]),
        "STREAMS": str(n),
        "PARALLEL": str(n),
        "PEAK_GIB": str(max(peak_gib(settings["context"], settings["prompt_cache_gib"], n, arena, split) + m.get("peak_extra_gib", 0),
                            int(profile.get("peak_gib") or 0))),  # the team's measured peak when higher
        "RUN_ROOT": f"'{run_root}'",
        "BENCH": "0",
    })
    return v


def _num(x):
    return str(int(x)) if float(x) == int(x) else str(x)


def resolve(profile, settings, base_text, deploy_name, run_root="$HOME/halo-control/runs", deploy_root="$HOME/tfp/deploy"):
    """(conf text, parsed values) for a run: the base conf with the profile's values written over it."""
    base_split = parse_conf(base_text).get("PREFILL_SPLIT_LARGE", "")
    text = apply_conf(base_text, conf_values(profile, settings, deploy_name, run_root, deploy_root, base_split),
                      tag=f"halo-control {profile['id']}")
    return text, parse_conf(text)


# sf-disk (StrixFold #15, deploy 0d050c5b9b74 on): kept prompt states also on each box's own disk, so a conversation the
# pool's placement pushed out, or one from before a restart, comes back in seconds instead of being read again. The
# budget is the pair's (halod.json prompt_disk_gib: the main boxes 20 GiB, box2 has ~40 GB free; the extra pairs 128), not a
# profile's. A deploy without the tier ignores the keys.
PROMPT_DISK_MAX_GIB = 1024
_DISK_DIR = re.compile(r"(\$HOME|)(/[A-Za-z0-9_.-]+)+")


def prompt_disk(text, gib, path="$HOME/strixfold-cache/prefix"):
    """A run's conf text with sf-disk's PROMPT_DISK_GIB and PROMPT_DISK_DIR when the pair has a disk budget (gib > 0)
    and the run keeps prompt states (PROMPT_CACHE_GIB > 0: the disk tier writes what the memory tier keeps); else as is."""
    g = float(gib or 0)
    if not 0 <= g <= PROMPT_DISK_MAX_GIB:
        raise ValueError(f"prompt_disk_gib: 0 to {PROMPT_DISK_MAX_GIB}, not {gib}")
    if not _DISK_DIR.fullmatch(path or "") or "/../" in path + "/":
        raise ValueError(f"prompt_disk_dir: an absolute path or one under $HOME, not {path!r}")
    if g == 0 or float(parse_conf(text).get("PROMPT_CACHE_GIB") or 0) <= 0:
        return text
    return apply_conf(text, {"PROMPT_DISK_GIB": _num(g), "PROMPT_DISK_DIR": f"'{path}'"}, tag="halo-control prompt-disk")


def diff(old, new):
    """A plain one-line description of what changed between two profile dicts (for the audit log)."""
    parts = []
    if old.get("name") != new.get("name"):
        parts.append(f"name {old.get('name')!r} -> {new.get('name')!r}")
    for k in sorted(set(old.get("settings", {})) | set(new.get("settings", {}))):
        a, b = old.get("settings", {}).get(k), new.get("settings", {}).get(k)
        if a != b:
            parts.append(f"{k} {a} -> {b}")
    oa, na = old.get("advanced", {}), new.get("advanced", {})
    for k in sorted(set(oa) | set(na)):
        if oa.get(k) != na.get(k):
            parts.append(f"{k} {oa.get(k, '(conf default)')} -> {na.get(k, '(conf default)')}")
    if bool(old.get("expert")) != bool(new.get("expert")):
        parts.append(f"expert {'on' if new.get('expert') else 'off'}")
    return "; ".join(parts) or "no change"


# ---------------------------------------------------------------- the profile store

def _with_conf(deploys, profile):
    """The deploy choices for a profile: 'conf' (the deploy its base_conf names) only when it has one."""
    if deploys is None:
        return None
    return set(deploys) | ({"conf"} if profile.get("base_conf") else set())


class Store:
    """Built-in profiles plus custom ones in a JSON file (custom only may be created, changed or deleted)."""

    def __init__(self, path):
        self.path = path
        self.custom = []
        if os.path.exists(path):
            with open(path) as f:
                self.custom = json.load(f).get("profiles", [])

    def _view(self, p, builtin):
        q = copy.deepcopy(p)
        q["builtin"] = builtin
        q.setdefault("model_id", "dsv41")
        q["settings"] = dict(DEFAULTS, **q.get("settings", {}))
        q.setdefault("advanced", {})
        q.setdefault("expert", False)
        m = model(q["model_id"]) or {}
        q["engine"] = m.get("engine", TF)
        q["model"] = m.get("model", q.get("model", ""))
        q["quant"] = m.get("quant", "")
        if not m.get("installed", True) or not ENGINES.get(q["engine"], {}).get("installed", False):
            q["installed"] = False
            q["missing"] = m.get("missing") or "Not installed."
        else:
            q.setdefault("installed", True)
        return q

    def all(self):
        return [self._view(p, True) for p in BUILTIN] + [self._view(p, False) for p in self.custom]

    def get(self, pid):
        for p in self.all():
            if p["id"] == pid:
                return p
        return None

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"profiles": self.custom}, f, indent=1)
        os.replace(tmp, self.path)

    def _check_name(self, name, pid=None):
        name = (name or "").strip() if isinstance(name, str) else ""
        if not name or len(name) > 60 or any(ord(c) < 32 for c in name):
            raise Invalid({"name": "1 to 60 characters"})
        if any(p["name"].lower() == name.lower() and p["id"] != pid for p in self.all()):
            raise Invalid({"name": "a profile has this name already"})
        return name

    def create(self, name, base_id, settings, deploys=None, advanced=None, expert=None, base_keys=None, base_values=None,
               max_rows=None):
        base = self.get(base_id)
        if base is None:
            raise Invalid({"base": f"no profile {base_id}"})
        if not base.get("installed", True):
            raise Invalid({"base": f"{base['name']} is not installed yet"})
        if base.get("fixed"):
            raise Invalid({"base": f"{base['name']} runs on its engine's own settings; it cannot be duplicated"})
        name = self._check_name(name)
        merged = dict(base["settings"])
        merged.update(settings or {})
        checked = validate_settings(merged, _with_conf(deploys, base), model_id=base["model_id"], max_rows=max_rows)
        exp = bool(base.get("expert") if expert is None else expert)
        adv = dict(base.get("advanced", {}))
        if advanced is not None:
            adv = advanced
        adv = validate_advanced(adv, base_keys, exp, base_values)
        pid = "custom-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        n, base_pid = 2, pid
        while self.get(pid):
            pid, n = f"{base_pid}-{n}", n + 1
        p = {"id": pid, "name": name, "model_id": base["model_id"],
             "use_for": f"Custom, from {base['name']}.", "tradeoffs": "", "settings": checked, "base": base["id"],
             "advanced": adv, "expert": exp, "created": time.time()}
        for k in ("conf", "base_conf"):
            if base.get(k):
                p[k] = base[k]
        self.custom.append(p)
        self._save()
        return self.get(pid)

    def update(self, pid, name=None, settings=None, deploys=None, advanced=None, expert=None, base_keys=None, base_values=None,
               max_rows=None):
        """(old view, new view) after changing a custom profile. max_rows: as validate_settings' (the window its deploy's
        RoPE kit serves; None: the bundle kit's)."""
        for i, p in enumerate(self.custom):
            if p["id"] == pid:
                old = self.get(pid)
                q = copy.deepcopy(p)
                if name is not None:
                    q["name"] = self._check_name(name, pid)
                if settings is not None:
                    merged = dict(DEFAULTS, **q["settings"])
                    merged.update(settings)
                    q["settings"] = validate_settings(merged, _with_conf(deploys, q), model_id=q.get("model_id", "dsv41"),
                                                     max_rows=max_rows)
                if expert is not None:
                    q["expert"] = bool(expert)
                if advanced is not None or expert is not None:
                    adv = q.get("advanced", {}) if advanced is None else advanced
                    q["advanced"] = validate_advanced(adv, base_keys, q.get("expert", False), base_values)
                self.custom[i] = q
                self._save()
                return old, self.get(pid)
        if self.get(pid):
            raise Invalid({"profile": "built-in profiles cannot be changed; duplicate it instead"})
        raise KeyError(pid)

    def delete(self, pid):
        for i, p in enumerate(self.custom):
            if p["id"] == pid:
                del self.custom[i]
                self._save()
                return
        if self.get(pid):
            raise Invalid({"profile": "built-in profiles cannot be deleted"})
        raise KeyError(pid)

    def export(self):
        keep = ("name", "model_id", "base", "settings", "advanced", "expert", "conf", "base_conf", "use_for")
        return {"halo_control_profiles": 1, "exported": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "profiles": [{k: p[k] for k in keep if k in p} for p in self.custom]}

    def import_(self, data, deploys=None, base_keys_for=None, kit_limit=None):
        """Add the profiles of an export. Each is validated as if created by hand; a name already taken gets
        ' (imported)'. kit_limit(model, deploy): the window that deploy serves (halod.kit_limit). Returns (created
        views, {name: error})."""
        if not isinstance(data, dict) or data.get("halo_control_profiles") != 1 or not isinstance(data.get("profiles"), list):
            raise Invalid({"file": "not a StrixFold UI profiles export"})
        made, errors = [], {}
        for raw in data["profiles"][:100]:
            nm = str((raw or {}).get("name") or "?")[:60]
            try:
                mid = raw.get("model_id", "dsv41")
                if mid not in MODEL_IDS:
                    raise Invalid({"model": f"unknown model {mid!r}"})
                base_id = raw.get("base") if self.get(raw.get("base") or "") else next(
                    (b["id"] for b in BUILTIN if b["model_id"] == mid), None)
                base = self.get(base_id) if base_id else None
                if base is None or base["model_id"] != mid:
                    raise Invalid({"model": f"no built-in profile for model {mid}"})
                if raw.get("base_conf") and raw.get("base_conf") != base.get("base_conf"):
                    raise Invalid({"base_conf": "differs from its built-in profile's"})
                name = nm
                if any(p["name"].lower() == name.lower() for p in self.all()):
                    name = (name[:48] + " (imported)")
                keys, vals = base_keys_for(base) if base_keys_for else (None, None)
                dep = (raw.get("settings") or {}).get("deploy") or base["settings"].get("deploy")
                made.append(self.create(name, base["id"], raw.get("settings") or {}, deploys, raw.get("advanced") or {},
                                        bool(raw.get("expert")), keys, vals, kit_limit(mid, dep) if kit_limit else None))
            except Invalid as e:
                errors[nm] = str(e)
            except (AttributeError, TypeError) as e:
                errors[nm] = f"malformed: {e}"
        return made, errors
