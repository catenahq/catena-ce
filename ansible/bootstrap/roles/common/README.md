# common

Baseline host setup that an operator runs, over SSH, from outside. First role
in `converge.yml`'s converge order.

## Responsibilities

- Set the hostname and keep `/etc/hosts` mapping it to `127.0.1.1`, with
  cloud-init told not to regenerate the file.
- apt: the optional HTTP proxy (`01proxy`), the dpkg lock timeout, the cache
  refresh with a proxy-bypass fallback, and the one-time full upgrade a fresh
  image needs. They run before the other bootstrap roles install anything.
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
