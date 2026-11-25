"""The photo look's model, downloaded once (tools/setup_look.sh): every file of one Hugging Face repo at one revision,
as tools/look-model.json lists them, each checked against its sha256 before it is kept. A file already there with the
right hash is not downloaded again; a wrong one is refused and removed.

python3 -I look_fetch.py MANIFEST.json DEST_DIR   (DEST_DIR: the model's own new folder, e.g. ~/models/<name>@<rev7>)
"""
import hashlib
import json
import os
import subprocess
import sys


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for ch in iter(lambda: f.read(1 << 22), b""):
            h.update(ch)
    return h.hexdigest()


def main(manifest, dest):
    man = json.load(open(manifest))
    os.makedirs(dest, mode=0o700, exist_ok=True)
    for name, f in sorted(man["files"].items()):
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            sys.exit(f"refused: {name} is not a plain file name")
        p = os.path.join(dest, name)
        if os.path.exists(p) and os.path.getsize(p) == f["bytes"] and sha256(p) == f["sha256"]:
            print("have", name, flush=True)
            continue
        url = f"https://huggingface.co/{man['repo']}/resolve/{man['revision']}/{name}"
        subprocess.run(["curl", "-fsSL", "--proto", "=https", "--retry", "5", "--retry-delay", "5", "-o", p + ".part", url], check=True)
        got = sha256(p + ".part")
        if got != f["sha256"]:
            os.unlink(p + ".part")
            sys.exit(f"refused: {name} has sha256 {got}, the manifest says {f['sha256']}")
        os.replace(p + ".part", p)
        os.chmod(p, 0o600)
        print("ok", name, f["bytes"], flush=True)
    extra = sorted(set(os.listdir(dest)) - set(man["files"]) - {"MANIFEST.json"})
    if extra:
        sys.exit(f"refused: {dest} holds files the manifest does not list: {', '.join(extra)}")
    with open(os.path.join(dest, "MANIFEST.json"), "w") as out:
        json.dump(man, out, indent=1, sort_keys=True)
    print("model ready:", man["repo"], man["revision"])


if __name__ == "__main__":
    main(*sys.argv[1:])
