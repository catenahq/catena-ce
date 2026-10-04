# host_hardening

Self-contained kernel-state hardening scoped to **Docker container escape**.
Drops two config files and applies them; no upstream collection dependency.

## What it touches

| Path | Purpose |
|---|---|
| `/etc/sysctl.d/99-catena-hardening.conf` | sysctl values listed in [defaults/main.yml](defaults/main.yml) under `host_hardening_sysctl` |
| `/etc/modprobe.d/99-catena-hardening.conf` | `install <module> /bin/true` lines for each entry in `host_hardening_modprobe_blacklist` |

The role is the **sole writer** to those paths in catena. Any future task
that wants to flip a kernel knob extends [defaults/main.yml](defaults/main.yml)
instead of dropping a new file.

## Knobs set

[defaults/main.yml](defaults/main.yml) holds the values and, beside each,
the escape vector it closes or the Docker requirement it meets
(`host_hardening_sysctl`, `host_hardening_modprobe_blacklist`), and why the
role owns the list rather than wrapping a hardening collection.

## Knobs that stay permissive

[tasks/validate.yml](tasks/validate.yml) catches a future commit that
silently flips one of these:

| Setting | Value | Why we hold it |
|---|---|---|
| `kernel.modules_disabled` | 0 | Docker auto-loads `br_netfilter`, `overlay`, `ip_vs*` at runtime; pinning to 1 breaks any swarm change requiring a new module. |
| `kernel.unprivileged_userns_clone` | 1 (Debian default) | Several managed images use unshare/userns. The escape vector is partially mitigated by the `protected_*` family above. |
| `net.ipv4.ip_forward` | 1 (force) | Required by Docker bridge. Drop to 0 -> all container egress black-holes. |
| `net.ipv6.conf.all.forwarding` | 1 (force) | Same for IPv6. |
| `net.bridge.bridge-nf-call-{iptables,ip6tables}` | 1 (force) | Required by Docker swarm overlay so iptables FORWARD sees bridged traffic. |
| `net.ipv4.conf.{all,default}.log_martians` | not set | docker swarm interface churn flips these between converges; per-iface enforcement would require chasing docker. Trade-off accepted: martian-source logging is debug noise, not security control. |

## Ordering

Run **after `bootstrap/roles/common`** (which installs `python3-apt`) and **before
`bootstrap/roles/docker`** so the sysctl values are in place before dockerd's first
start. Slot is right after `public_ports`, before `storage` and `docker`, in
[../../../playbooks/converge.yml](../../../playbooks/converge.yml).

If `dockerd` starts first, its runtime writes to `/proc/sys` win over
`/etc/sysctl.d/` until next boot, leaving Day-1 state divergent from
reboot-recovery state.

## Reboot

The role does NOT reboot the host. The modprobe blacklist file takes
effect on **next module load**, not via reboot. On a freshly-installed
VPS the blacklisted modules are never loaded in the first place; the
validate task checks `lsmod` separately on long-running hosts.

If a future operator wants to enforce immediate unload, they should run
`modprobe -r <module>` for each blacklisted module after the converge --
not via this role.

## Adding a knob

1. Append the key/value to `host_hardening_sysctl` (or
   `host_hardening_modprobe_blacklist`) in
   [defaults/main.yml](defaults/main.yml) with a one-line "why" comment.
2. Add an assertion to [tasks/validate.yml](tasks/validate.yml) so a
   future delete is caught.
3. Run the bench (`./bench run` in the ops repo).

## Validation

[tasks/validate.yml](tasks/validate.yml) is invoked by the project's
top-level [../../../playbooks/validate.yml](../../../playbooks/validate.yml)
and asserts:

- Every positive-side sysctl matches its expected value.
- Every Docker-requirement sysctl is held at 1.
- `kernel.modules_disabled` remains at 0.
- `/etc/modprobe.d/99-catena-hardening.conf` exists and contains every
  expected blacklist line.
- `vfat` is NOT blacklisted (EFI requirement).
- `auditd` is NOT installed (canary against a hardening collection
  re-introduction).

Container-side escape probes live in
`ops/automation/test_bench/scenarios/security_scan.py`
and exercise the policy from inside a real container.
