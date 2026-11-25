#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo 'Run with sudo' >&2; exit 1; }
install -d -m 755 /usr/local/libexec
model_uid=${SUDO_UID:?run it with sudo from the model account}
sed "s/^MODEL_UID=1001 /MODEL_UID=$model_uid /" enter.py >/usr/local/libexec/halo-inference-enter
chmod 755 /usr/local/libexec/halo-inference-enter
grep -q "^MODEL_UID=$model_uid " /usr/local/libexec/halo-inference-enter
install -m 755 run.py /usr/local/bin/halo-inference-run
install -m 755 firewall.sh /usr/local/libexec/halo-inference-firewall
install -m 644 halo-inference-net.service /etc/systemd/system/halo-inference-net.service
printf '%%sudo ALL=(root) NOPASSWD: /usr/local/libexec/halo-inference-enter\n' >/etc/sudoers.d/halo-inference-enter
chmod 440 /etc/sudoers.d/halo-inference-enter
visudo -cf /etc/sudoers.d/halo-inference-enter
systemctl daemon-reload
systemctl enable --now halo-inference-net.service
