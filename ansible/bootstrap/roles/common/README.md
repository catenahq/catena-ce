# common

Baseline host setup that an operator runs, over SSH, from outside. First role
in `converge.yml`'s converge order.

## Responsibilities

- Set the hostname, then apply `playbooks/tasks/host_name_resolution.yml`
  (the name on `127.0.1.1`, cloud-init kept off `/etc/hosts`) and
  `playbooks/tasks/apt_settings.yml` (the optional HTTP proxy, the dpkg lock
  timeout). Both are shared with `reconcile/roles/host_maintenance`, which
  applies them on every converge; here they run first because sudo and every
  apt operation after them depend on them.
- apt: the cache refresh with a proxy-bypass fallback, and the full upgrade a
  fresh image needs, before the other bootstrap roles install anything.
- Install `catena_baseline_packages` (shared with
  `reconcile/roles/host_maintenance`, which keeps them installed on every
  converge).
- Create the `ops` sudo account and the `panel` account, which has no shell
  and whose keys may only forward to the panel (9010) and Portainer (9000) on
  the host's loopback; set the console break-glass password.
- Prove the operator key works as `ops`, then apply the sshd hardening.
- Mint the journal's sealing key once and stage its verification half for
  `show-keyset.yml`.
- The ufw baseline, public SSH, and the `ufw_lockdown.yml` task file
  `playbooks/lockdown.yml` runs: it allows SSH over the tailnet, and closes
  public 22 only when the panel's Lockdown asks.
- Create `/etc/catena` and `/var/lib/catena`.

## Idempotency

- All resources converge; re-running with the same inputs is a no-op.

## Related

- Callers: `playbooks/bootstrap.yml` and `playbooks/converge.yml`;
  `playbooks/lockdown.yml` for `ufw_lockdown.yml`.
- The version stamp is `playbooks/tasks/stamp_version.yml`, shared by both
  converge paths.
