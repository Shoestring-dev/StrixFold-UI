#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
rm -f /run/halo-inference-net.ready
mkdir -p /sys/fs/cgroup/halo-inference
case $(hostname -s) in
 box1) peers=(10.40.0.2 10.40.1.2);;  # rank 0
 box2) peers=(10.40.0.1 10.40.1.1);;
 *) echo 'Unknown inference host' >&2;exit 1;;
esac
# Keep the current rules during a service restart. Never flush an active policy.
for ipt in iptables ip6tables;do
 if ! "$ipt" -w -S HALO_INFERENCE >/dev/null 2>&1;then
  "$ipt" -w -N HALO_INFERENCE
  if [ "$ipt" = iptables ];then
   "$ipt" -w -A HALO_INFERENCE -m conntrack --ctstate ESTABLISHED --ctdir REPLY -j ACCEPT
   for peer in "${peers[@]}";do
    "$ipt" -w -A HALO_INFERENCE -d "$peer/32" -p tcp -m multiport --dports 29620,29636,29637,29644,29645 -j ACCEPT
   done
  fi
  "$ipt" -w -A HALO_INFERENCE -m limit --limit 6/min --limit-burst 10 -j LOG --log-prefix 'HALO-INFER-DENY '
  "$ipt" -w -A HALO_INFERENCE -j REJECT
 fi
 "$ipt" -w -C OUTPUT -m cgroup --path halo-inference -j HALO_INFERENCE 2>/dev/null || \
  "$ipt" -w -I OUTPUT 1 -m cgroup --path halo-inference -j HALO_INFERENCE
 "$ipt" -w -C HALO_INFERENCE -j REJECT
 done
install -m 600 /dev/null /run/halo-inference-net.ready
