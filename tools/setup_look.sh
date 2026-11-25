#!/bin/bash
# setup_look.sh: the Mac's photo look (mac/look.py), so the text-only chat models can read a description of a photo
# and ask about it. Two downloads, here and now, both pinned:
#  - the venv: mlx-vlm 0.7.6 and its dependencies, each file by sha256 (tools/look-requirements.txt), in
#    ~/Library/Application Support/StrixFoldUI/look-venv;
#  - the model: one Hugging Face repo at one revision, each file by sha256 (tools/look-model.json), in ~/models.
# The app never installs or downloads anything itself; its worker runs in a Seatbelt profile with no network.
# Undo: rm -rf the two folders this prints.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HOME/Library/Application Support/StrixFoldUI/look-venv"
DEST="$HOME/models/$(/usr/bin/python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m["repo"].split("/")[1] + "@" + m["revision"][:7])' "$HERE/look-model.json")"
NEED_GB=$(( $(/usr/bin/python3 -c 'import json,sys; print(sum(f["bytes"] for f in json.load(open(sys.argv[1]))["files"].values()) >> 30)' "$HERE/look-model.json") + 2 ))
FREE_GB=$(df -g "$HOME" | awk 'NR==2 {print $4}')
if [ "$FREE_GB" -lt $(( NEED_GB + 100 )) ]; then
  echo "Not enough room: ${FREE_GB} GB free; this needs ${NEED_GB} GB and keeps 100 GB free." >&2
  exit 1
fi
command -v uv >/dev/null || { echo "uv is missing (it made the whisper venv too): https://docs.astral.sh/uv/" >&2; exit 1; }
[ -x "$VENV/bin/python" ] || uv venv --python 3.12 "$VENV"
uv pip install --python "$VENV/bin/python" --require-hashes --no-deps -r "$HERE/look-requirements.txt"
/usr/bin/python3 -I "$HERE/look_fetch.py" "$HERE/look-model.json" "$DEST"
chmod -R go-rwx "$VENV" "$DEST"
"$VENV/bin/python" -c 'import mlx_vlm; print("photo look ready: mlx-vlm", mlx_vlm.__version__)'
echo "venv:  $VENV"
echo "model: $DEST"
