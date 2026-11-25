#!/usr/bin/python3 -I
"""Root-only helper: move one process owned by the invoking model user into the restricted cgroup."""
import os,sys
MODEL_UID=1001  # the model account; install.sh writes the installing account's own
if os.geteuid()!=0 or len(sys.argv)!=2 or not sys.argv[1].isdigit():
 raise SystemExit('Expected sudo and a numeric PID')
uid=int(os.environ.get('SUDO_UID','-1')); pid=int(sys.argv[1])
if uid!=MODEL_UID or pid<2 or os.stat('/proc/'+str(pid)).st_uid!=uid:
 raise SystemExit('Only the model account can enter its own process')
if not os.path.exists('/run/halo-inference-net.ready'):
 raise SystemExit('Inference network policy is not ready; refusing launch')
with open('/sys/fs/cgroup/halo-inference/cgroup.procs','w') as f:f.write(str(pid))
