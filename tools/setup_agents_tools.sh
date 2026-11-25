#!/bin/bash
# Agents tools on this Mac: SearXNG (a pinned git commit) and the web_read page parser (trafilatura + lxml),
# each in its own venv, every Python package installed with pip --require-hashes --only-binary from the lock files
# in mac/agents_web/locks. Idempotent. Nothing runs as a service: StrixFold UI starts SearXNG on demand, under
# its Seatbelt profile, and stops it. Python packages only; no Homebrew, no system changes.
#   tools/setup_agents_tools.sh          install or repair
#   tools/setup_agents_tools.sh lock     re-compile the lock files from the .in files (uv; review the diff)
set -euo pipefail
umask 077
REPO=$(cd "$(dirname "$0")/.." && pwd)
LOCKS="$REPO/mac/agents_web/locks"
BASE="${HALO_AGENTS_HOME:-$HOME/Library/Application Support/HaloAgents}"
SEARXNG_REPO=https://github.com/searxng/searxng
SEARXNG_COMMIT=81371861f67226091d1c4df2e3908a11d3b54eb9   # master, 2026-10-19
PYVER=3.12.13
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

if [ "${1:-}" = lock ]; then
  for n in parser searxng; do
    "$UV" pip compile "$LOCKS/$n.in" --python-version 3.12 --python-platform aarch64-apple-darwin --generate-hashes \
      --no-header -o "$LOCKS/$n.txt"
  done
  exit 0
fi

PY="${HALO_AGENTS_PYTHON:-$("$UV" python find "$PYVER" 2>/dev/null || true)}"
[ -n "$PY" ] && [ -x "$PY" ] || { echo "Python $PYVER was not found (uv python find $PYVER)."; exit 1; }
mkdir -p "$BASE"
chmod 700 "$BASE"

pipi() {  # venv, lock file
  "$1/bin/python" -m pip install --disable-pip-version-check --no-input --quiet --require-hashes --only-binary :all: \
    --no-deps -r "$2"
}

newvenv() {  # path: a fresh venv unless it already runs this Python
  if [ -x "$1/bin/python" ] && [ "$("$1/bin/python" -c 'import platform; print(platform.python_version())')" = "$PYVER" ]; then
    return
  fi
  rm -rf "$1"
  "$PY" -m venv "$1"
}

# ---------------------------------------------------------------- the page parser
newvenv "$BASE/parser-venv"
pipi "$BASE/parser-venv" "$LOCKS/parser.txt"
"$BASE/parser-venv/bin/python" -I -c 'import trafilatura, lxml; print("parser: trafilatura", trafilatura.__version__)'

# ---------------------------------------------------------------- SearXNG
SX="$BASE/searxng"
mkdir -p "$SX/cache"
if [ ! -d "$SX/src/.git" ] || [ "$(git -C "$SX/src" rev-parse HEAD 2>/dev/null)" != "$SEARXNG_COMMIT" ]; then
  rm -rf "$SX/src"
  git init -q "$SX/src"
  git -C "$SX/src" fetch -q --depth 1 "$SEARXNG_REPO" "$SEARXNG_COMMIT"
  git -C "$SX/src" checkout -q --detach FETCH_HEAD
fi
[ "$(git -C "$SX/src" rev-parse HEAD)" = "$SEARXNG_COMMIT" ] || { echo "SearXNG is not at $SEARXNG_COMMIT"; exit 1; }
cmp -s "$SX/src/requirements.txt" "$LOCKS/searxng.in" || { echo "SearXNG's requirements.txt differs from locks/searxng.in: re-lock"; exit 1; }
cat > "$SX/src/searx/version_frozen.py" <<EOF
# written by halo-control tools/setup_agents_tools.sh (no git calls at run time)
VERSION_STRING = "${SEARXNG_COMMIT:0:12}"
VERSION_TAG = "${SEARXNG_COMMIT:0:12}"
DOCKER_TAG = "${SEARXNG_COMMIT:0:12}"
GIT_URL = "$SEARXNG_REPO"
GIT_BRANCH = "master"
EOF
newvenv "$SX/venv"
pipi "$SX/venv" "$LOCKS/searxng.txt"
if [ ! -f "$SX/secret" ]; then
  "$PY" -c 'import secrets; print(secrets.token_hex(32))' > "$SX/secret"
fi
chmod 600 "$SX/secret"
cp "$REPO/mac/agents_web/searxng-settings.yml" "$SX/settings.yml"
chmod 600 "$SX/settings.yml"
(cd "$SX/src" && PYTHONPATH="$SX/src" SEARXNG_SETTINGS_PATH="$SX/settings.yml" SEARXNG_SECRET=x \
  "$SX/venv/bin/python" -I -c 'import sys; sys.path.insert(0, "."); import searx; print("searxng: ok")')
echo "Agents tools installed in $BASE"
