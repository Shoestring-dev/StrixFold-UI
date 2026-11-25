#!/usr/bin/env python3
"""fake_env.py ROOT [--load-s S] [--speed X] [--port P] [--endpoint-port P]: build a fake two-box world under ROOT and
print the halod config (JSON) that drives it. Each fake box is a HOME (ROOT/box1, ROOT/box2) with the real deploy
layout (tfp/deploy/<commit>/tools/dsv41-hip/{serve.conf, serve-rank0.sh, serve-rank1.sh}, 'current' pointing at it)
whose rank scripts are fake_serve.py, and its own lock file. Used by the unit tests and by halo-control --fake.
"""
import json
import os
import shutil
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOX = os.path.dirname(HERE)
COMMIT = "fa4e00000001"
UNC_COMMIT = "0a6eb9c2abf4"  # the uncensored model's deploy_default (4f2979d61444 earlier, 0d050c5b9b74 with the dense pack before that)
UNC_SHA = "243b984d1069b190e762b3f9eaf12c90b2a23dd1e8f33880019f60d61264e8e9"  # the official bundle's, which the edit runs on
HOST = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()


def edit_conf(endpoint_port=18080):
    """The uncensored deploy's serve.conf: the shipped one's keys with TOP_K_MAX and an empty EDIT (halod writes it)."""
    text = open(os.path.join(HERE, "serve-unc.conf")).read().replace("SERVE_PORT=18080", f"SERVE_PORT={endpoint_port}")
    lines = [l for l in text.splitlines() if not l.startswith(("BUNDLE=", "BUNDLE_SHA256="))]
    return "\n".join(lines + ["BUNDLE='$HOME/models/tf-port/inputs-tp2-c178a012'", f"BUNDLE_SHA256={UNC_SHA}", "EDIT=",
                              "EDIT_SHA256="]) + "\n"


def build(root, load_s=6.0, speed=1.0, port=8710, endpoint_port=18080, tick_s=1.0, qwen_port=None, **more):
    root = os.path.abspath(root)
    shared = os.path.join(root, "shared")
    os.makedirs(shared, exist_ok=True)
    boxes = []
    for rank, name in ((0, "box1"), (1, "box2")):
        home = os.path.join(root, name)
        tools = os.path.join(home, "tfp", "deploy", COMMIT, "tools", "dsv41-hip")
        os.makedirs(tools, exist_ok=True)
        os.makedirs(os.path.join(home, "tfp", "qual", "conf"), exist_ok=True)
        with open(os.path.join(home, "tfp", "deploy", COMMIT, "DEPLOY.json"), "w") as f:
            json.dump({"commit": COMMIT + "0" * 28, "fake": True}, f)
        conf = open(os.path.join(HERE, "serve.conf")).read().replace("SERVE_PORT=18080", f"SERVE_PORT={endpoint_port}")
        open(os.path.join(tools, "serve.conf"), "w").write(conf)
        q = open(os.path.join(HERE, "q-a11-on.conf")).read().replace("a11df7c77f76", COMMIT).replace("SERVE_PORT=18080", f"SERVE_PORT={endpoint_port}")
        open(os.path.join(home, "tfp", "qual", "conf", "q-a11-on.conf"), "w").write(q)
        for r in (0, 1):
            p = os.path.join(tools, f"serve-rank{r}.sh")
            with open(p, "w") as f:
                f.write(f"#!/bin/bash\nexec {shlex_q(sys.executable)} {shlex_q(os.path.join(HERE, 'fake_serve.py'))} {r} \"$@\"\n")
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        cur = os.path.join(home, "tfp", "deploy", "current")
        if os.path.islink(cur):
            os.unlink(cur)
        os.symlink(COMMIT, cur)
        # the uncensored model: a newer deploy beside the official one (its serve.conf knows EDIT and TOP_K_MAX) on the
        # official bundle, with this box's verify report passed
        utools = os.path.join(home, "tfp", "deploy", UNC_COMMIT, "tools", "dsv41-hip")
        os.makedirs(utools, exist_ok=True)
        with open(os.path.join(home, "tfp", "deploy", UNC_COMMIT, "DEPLOY.json"), "w") as f:
            json.dump({"commit": UNC_COMMIT + "0" * 28, "fake": True}, f)
        open(os.path.join(utools, "serve.conf"), "w").write(edit_conf(endpoint_port))
        for r in (0, 1):
            shutil.copy(os.path.join(tools, f"serve-rank{r}.sh"), os.path.join(utools, f"serve-rank{r}.sh"))
        bundle = os.path.join(home, "models", "tf-port", "inputs-tp2-c178a012")
        os.makedirs(bundle, exist_ok=True)
        with open(f"{bundle}.verify-rank{rank}-{HOST}.json", "w") as f:
            json.dump({"status": "pass", "rank": rank, "manifest_sha256": UNC_SHA}, f)
        lock = os.path.join(root, f"{name}.lock")
        open(lock, "a").close()
        env = ["env", f"HOME={home}", f"HALO_FAKE_LOCK={lock}", f"HALO_FAKE_SHARED={shared}",
               f"HALO_FAKE_LOAD_S={load_s}", f"HALO_FAKE_SPEED={speed}"]
        boxes.append({"name": name, "rank": rank, "exec": [env], "lock": lock})
    cfg = {"listen_port": port, "home": os.path.join(root, "halod"), "fake": "fake",
           "probe": os.path.join(BOX, "probe.py"), "boxes": [{k: b[k] for k in ("name", "rank", "exec", "lock")} for b in boxes],
           "quiet_dir": os.path.join(root, "quiet"), "endpoint": f"http://127.0.0.1:{endpoint_port}",
           "tick_s": tick_s, "shared": shared}
    os.makedirs(cfg["quiet_dir"], exist_ok=True)
    # Qwen3.8 through a stand-in for halo-qwen (fake_qwen.py): its own start/stop commands, its own port
    q = os.path.join(root, "qwen")
    os.makedirs(q, exist_ok=True)
    for f in ("fail-up", "alive", "state"):
        if os.path.exists(os.path.join(q, f)):
            os.unlink(os.path.join(q, f))
    open(os.path.join(q, "port"), "w").write(str(qwen_port or endpoint_port + 10))
    fq = f"{shlex_q(sys.executable)} {shlex_q(os.path.join(HERE, 'fake_qwen.py'))} {shlex_q(root)}"
    cfg["models_ready"] = ["qwen38", "qwen38-kyojin"]  # the stand-ins count as verified
    k = os.path.join(root, "kyojin")  # Qwen3.8 on Kyojin through a stand-in for halo-qwen-kyojin, one server per box
    os.makedirs(k, exist_ok=True)
    for f in ("fail-up", "alive", "state", "idle-stop"):
        if os.path.exists(os.path.join(k, f)):
            os.unlink(os.path.join(k, f))
    open(os.path.join(k, "port"), "w").write(str((qwen_port or endpoint_port + 10) + 3))
    for name in ("box1", "box2"):  # the per-box run dirs of the Kyojin stand-in
        d = os.path.join(root, name, "kyojin")
        os.makedirs(d, exist_ok=True)
        for f in ("alive", "state", "hold", "idle-stop", "peers-dropped"):
            if os.path.exists(os.path.join(d, f)):
                os.unlink(os.path.join(d, f))
    cfg["power_path"] = "$HOME/platform_profile"  # each fake box's own file in place of /sys/firmware/acpi
    cfg["power_write"] = "echo {value} > {path}"
    for b in boxes:
        home = [e for e in b["exec"][0] if e.startswith("HOME=")][0][5:]
        with open(os.path.join(home, "platform_profile"), "w") as f:
            f.write("balanced\n")
    cfg["external"] = {"qwen38": {"up": f"{fq} up", "down": f"{fq} down", "box": "box1", "port": qwen_port or endpoint_port + 10,
                                  "state_file": os.path.join(q, "state"), "unit": "fake-qwen", "log": os.path.join(q, "rank.log"),
                                  "alive": f"test -f {shlex_q(os.path.join(q, 'alive'))} && echo active",
                                  "check": f"test -f {shlex_q(os.path.join(HERE, 'fake_qwen.py'))}"},
                       "qwen38-kyojin": {"up": f"env KQ_CONTEXT={{pool}} KQ_SESSIONS={{streams}} KQ_IDLE_MIN={{idle_min}} {fq} up kyojin",
                                         "down": f"{fq} down kyojin", "box": "box1",
                                         # each box's node keeps its own run dir ($HOME: the fake box's), so one box can fail alone
                                         "boxes": ["box1", "box2"], "ready_path": "/health", "hold_file": "$HOME/kyojin/hold",
                                         "idle_marker": "$HOME/kyojin/idle-stop", "dropped_file": "$HOME/kyojin/peers-dropped",
                                         "port": (qwen_port or endpoint_port + 10) + 3, "state_file": "$HOME/kyojin/state",
                                         "unit": "fake-kyojin", "log": os.path.join(k, "rank.log"),
                                         "alive": "test -f $HOME/kyojin/alive && echo active",
                                         "check": f"test -f {shlex_q(os.path.join(HERE, 'fake_qwen.py'))}"}}
    return cfg


def shlex_q(s):
    import shlex
    return shlex.quote(s)


if __name__ == "__main__":
    a = sys.argv[1:]
    opts = {"--load-s": "6", "--speed": "1", "--port": "8710", "--endpoint-port": "18080"}
    it = iter(a[1:])
    for k in it:
        opts[k] = next(it)
    print(json.dumps(build(a[0], float(opts["--load-s"]), float(opts["--speed"]), int(opts["--port"]), int(opts["--endpoint-port"])), indent=1))
