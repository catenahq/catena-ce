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

## Why no upstream collection

Earlier revisions wrapped `devsec.hardening.os_hardening`. Removed because:

- 13 of 16 devsec subroles collide with `roles/common` (PAM, sudoers, ufw,
  unattended-upgrades, ops user) and had to be disabled.
- Disable toggles passed via the calling role's `defaults/` are silently
  ignored by `include_role` -- the called role's own defaults win unless
  every override is re-passed via `vars:`. This bug class repeatedly
  caused devsec to silently install auditd, run PAM hardening, etc.
- devsec ships `net.ipv4.ip_forward=0` (router-paranoid). When the override
  fails to propagate, every container's egress through any docker bridge
  / swarm overlay black-holes (cloudflared "failed to dial to edge with
  quic"). Observed and patched in commit history.
- `net.ipv4.conf.{all,default}.log_martians` flips on every converge
  because docker swarm operations (network/container creation) interact
  with the kernel's `all` aggregator. Tripped the bench's idempotency
  assertion and could not be cleanly suppressed via devsec's own
  `sysctl_unsupported_entries` allow-list.

The cost (≈80 lines of namespaced shadow-vars + `vars:`-on-include-role
boilerplate, plus three commits of fallout) was greater than just owning
the value list. The replacement is two templates and one task list.

## Knobs flipped (anti-escape)

Each entry below is in [defaults/main.yml](defaults/main.yml) under
`host_hardening_sysctl:`. The "why" column is the escape vector closed.

| Knob | Value | Why for escape |
|---|---|---|
| `kernel.yama.ptrace_scope` | 2 | Block `PTRACE_ATTACH` outside parent/child. Cuts the "shared pid ns -> ptrace a host process" pivot. Debian default = 1. |
| `kernel.unprivileged_bpf_disabled` | 1 | Closes unprivileged-eBPF exploit family (DirtyCred, BPF JIT bugs). Docker daemon = root, so dockerd-driven networking unaffected. |
| `net.core.bpf_jit_harden` | 2 | Constant-blinds JIT-ed BPF code that remains. |
| `kernel.kptr_restrict` | 2 | `/proc/kallsyms` returns zeros to non-CAP_SYSLOG. Most KASLR-bypass chains start here. |
| `kernel.dmesg_restrict` | 1 | Kernel pointers no longer leak via dmesg. |
| `fs.protected_symlinks` | 1 | Bans cross-UID TOCTOU via symlink. Closes runc/CRI-O bind-mount class. |
| `fs.protected_hardlinks` | 1 | Same family for hardlinks. |
| `fs.protected_fifos` | 2 | Ban FIFO follow in sticky world-writable dirs. |
| `fs.protected_regular` | 2 | Same for regular files. |
| `fs.suid_dumpable` | 0 | No core dumps from suid binaries. |
| `kernel.kexec_load_disabled` | 1 | A `--privileged` container with `CAP_SYS_BOOT` could otherwise kexec into a hostile kernel. |
| `kernel.sysrq` | 0 | SysRq triggers process-killing primitives; no operational use on a remote VPS. |
| `kernel.randomize_va_space` | 2 | ASLR strict. |
| `vm.mmap_min_addr` | 65536 | Block low-mmap (NULL-deref escalations). |
| `kernel.perf_event_paranoid` | 3 | Closes perf_events as side-channel oracle for non-root. |
| `kernel.io_uring_disabled` | 1 | io_uring has been a CVE-rich path. We do NOT set 2 -- would break Postgres + Node containers that opt into io_uring. |
| Module blacklist | (see below) | `cramfs freevxfs jffs2 hfs hfsplus udf dccp rds sctp tipc` -- none used; loading any has been an exploit primitive in past CVEs. `vfat` is NOT blacklisted (EFI requirement). |

## Knobs deliberately NOT flipped

[tasks/validate.yml](tasks/validate.yml) catches a future commit that
silently flips one of these:

| Setting | Value | Why we hold it |
|---|---|---|
| `kernel.modules_disabled` | 0 | Docker auto-loads `br_netfilter`, `overlay`, `ip_vs*` at runtime; pinning to 1 breaks any swarm change requiring a new module. |
| `kernel.unprivileged_userns_clone` | 1 (Debian default) | Several Dokploy-managed images use unshare/userns. The escape vector is partially mitigated by the `protected_*` family above. |
| `net.ipv4.ip_forward` | 1 (force) | Required by Docker bridge. Drop to 0 -> all container egress black-holes. |
| `net.ipv6.conf.all.forwarding` | 1 (force) | Same for IPv6. |
| `net.bridge.bridge-nf-call-{iptables,ip6tables}` | 1 (force) | Required by Docker swarm overlay so iptables FORWARD sees bridged traffic. |
| `net.ipv4.conf.{all,default}.log_martians` | not set | docker swarm interface churn flips these between converges; per-iface enforcement would require chasing docker. Trade-off accepted: martian-source logging is debug noise, not security control. |

## Ordering

Run **after `roles/common`** (which installs `python3-apt`) and **before
`roles/docker`** so the sysctl values are in place before dockerd's first
start. Slot is between `common` and `tailscale` in
[../../playbooks/site.yml](../../playbooks/site.yml).

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
3. Run the bench: `./catena test bench-run-all`.

## Validation

[tasks/validate.yml](tasks/validate.yml) is invoked by the project's
top-level [../../playbooks/validate.yml](../../playbooks/validate.yml)
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
[../../../test_bench/scenarios/security_scan.py](../../../test_bench/scenarios/security_scan.py)
and exercise the policy from inside a real container.
