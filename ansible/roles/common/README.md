# common

Baseline host setup. First role in `site.yml`'s converge order.

## Responsibilities

- Capture the controller's git state (commit SHA, dirty flag,
  branch) and ship it to the host as `/etc/catena/git-version`.
- Refuse to converge against a dirty controller tree unless
  `allow_dirty_converge=true` is explicitly set. This prevents
  "the host has bits from a tree the operator can't reproduce".
- Install minimal package baseline (curl, jq, sudo, tzdata,
  ca-certificates) before any later role depends on them.
- Provide the `ufw_lockdown.yml` task file used as the final lock
  step in `site.yml` (Tailscale-only ingress).

## Inputs

- `allow_dirty_converge` -- bool, default false.
- `git_version_path` -- defaults to `/etc/catena/git-version`.

## Side effects

- apt cache update + small install.
- Writes `/etc/catena/git-version`.
- `ufw_lockdown.yml` (when invoked) reconfigures the firewall.

## Idempotency

- All resources converge; re-running with the same inputs is a
  no-op.

## Related

- Caller: `playbooks/site.yml`.
