# DeepSeek inference network boundary

Install it on both boxes. This applies to the TensorFold DeepSeek ranks launched by StrixFold UI. It does not isolate the Mac dashboard, its tool runners, other model engines, or the entire box.

## Enforcement

`profiles.conf_values` always sets WRAP0 and WRAP1 to `/usr/local/bin/halo-inference-run` (preserving experimental environment switches after that wrapper). The wrapper enters the root-owned `halo-inference` cgroup before exec, preserves the PID used by the model supervisor, sets `no_new_privs`, and installs an inherited seccomp rule denying creation of Unix-domain sockets.

The root systemd service installs OUTPUT rules for sockets created in that cgroup:

- IPv4: allow established replies, plus TCP to the other box's two private interconnect addresses on ports 29620, 29636, 29637, 29644, 29645.
- Reject and rate-limit-log all other IPv4 output, including loopback and DNS.
- Reject and rate-limit-log all IPv6 output.

Unix-domain sockets are denied to prevent local service-manager/proxy connections bypassing IP filtering. The rank processes have no effective capabilities. Their children inherit the restrictions. The entering helper only accepts an existing PID owned by the invoking model account; it never executes a supplied command with elevated privileges. Missing readiness state causes launch to fail.

`install.sh` installs root-owned files and enables the policy service at boot. Policy-service restarts preserve the active chains rather than flushing them. Root administrators can still change or remove the controls; this is not a claim of immunity to host compromise or arbitrary local filesystem attacks.

## Verification

Run on each host:

```
/usr/local/bin/halo-inference-run /usr/bin/python3 ~/halo-control/box/inference_net/probe.py
sudo iptables -nvxL HALO_INFERENCE
sudo ip6tables -nvxL HALO_INFERENCE
sudo journalctl -k --grep HALO-INFER-DENY
```

Verify the actual rank PID is in `/sys/fs/cgroup/halo-inference/cgroup.procs` and `/proc/PID/status` shows `NoNewPrivs: 1`, `Seccomp: 2`, and zero `CapEff`. Probe failures are meaningful alongside firewall counters/logs and an unrestricted positive control to the same destination. IPv6 internet attempts currently fail for lack of a route; IPv6 loopback probes demonstrate the firewall's IPv6 rejection separately.

The rules apply to newly created sockets. Moving an already-running rank into the cgroup is not sufficient: launch/restart it using the wrapper. Verify inference still answers after the restart.

## Maintenance

If TensorFold changes interconnect ports, review and update the explicit allowlist on both hosts before launching that build. The installer does not silently replace an existing active chain. Never flush a host's entire firewall to troubleshoot this policy. Preserve the restriction while diagnosing, and stop both model ranks before deliberately removing it.
