#!/bin/bash
# setup_office_reader.sh: the venv StrixFold UI's chat reads old Excel files (.xls) with: xlrd 2.0.1, pure Python, from PyPI,
# pinned by its sha256 (the hash PyPI publishes for xlrd-2.0.1-py2.py3-none-any.whl). One download, here and now; the
# app never installs anything itself and reads .xls only when this venv exists (else the file comes as its details).
set -euo pipefail
VENV="$HOME/Library/Application Support/StrixFoldUI/office-venv"
REQ=$(mktemp)
echo "xlrd==2.0.1 --hash=sha256:6a33ee89877bd9abc1158129f6e94be74e2679636b8a205b43b85206c3f0bbdd" > "$REQ"
/usr/bin/python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --no-deps --only-binary=:all: --require-hashes -r "$REQ"
rm -f "$REQ"
chmod -R go-rwx "$VENV"
"$VENV/bin/python" -c 'import xlrd; print("office reader ready: xlrd", xlrd.__version__)'
