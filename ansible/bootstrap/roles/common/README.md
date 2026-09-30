# common

Baseline host setup. First role in `converge.yml`'s converge order.

## Responsibilities

- Capture the controller's git state (commit SHA, dirty flag,
  branch) and ship it to the host as `/etc/catena/git-version`. The
  `-dirty` suffix is recorded, not enforced: a client installs from a
  release checkout and never edits it, and the one caller that did edit
  it -- the test bench -- passed the override on every run.
- Install minimal package baseline (curl, jq, sudo, tzdata,
  ca-certificates) before any later role depends on them.
- Create the `ops` sudo account and the `panel` account, which has no shell
  and whose keys may only forward to the panel (9010) and Portainer (9000) on
  the host's loopback.
- Provide the `ufw_lockdown.yml` task file `playbooks/lockdown.yml` runs: it
  allows SSH over the tailnet, and closes public 22 only when the panel's
  Lockdown asks. On a host with no tailnet it opens public 22.

## Inputs

- `git_version_path` -- defaults to `/etc/catena/git-version`.

## Side effects

- apt cache update + small install.
- Writes `/etc/catena/git-version`.
- `ufw_lockdown.yml` (when invoked) reconfigures the firewall.

## Idempotency

- All resources converge; re-running with the same inputs is a
  no-op.

## Related

- Callers: `playbooks/bootstrap.yml` and `playbooks/converge.yml`;
  `playbooks/lockdown.yml` for `ufw_lockdown.yml`.
