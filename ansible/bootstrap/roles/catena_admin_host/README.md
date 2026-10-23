# bootstrap/roles/catena_admin_host

The host-side half of the admin panel that only the install writes: who the
panel's dispatch signs in as, and with what.

- the runner account and its home, with an empty `~/.ssh`
- the panel's SSH key pair in `/etc/catena/admin-ssh/`, minted on the host
  once and never replaced, the host key it trusts (`known_hosts`, scanned over
  loopback) and the SSH client config the panel's container uses
- the bind-mount sources the panel's service needs before it can start
- the migration account (`catena_migrate_user`) and its sshd drop-in,
  `/etc/ssh/sshd_config.d/31-catena-migrate.conf`: a server taking this one
  over signs in with the key of the open migration window, read from the one
  file the migration lane writes while that window is open, and runs nothing
  but the pipe to the lane's socket

Once signed in, a dispatch crosses what
[`reconcile/roles/dispatch_path`](../../../reconcile/roles/dispatch_path/README.md)
keeps: sshd's `AcceptEnv` for the runner, the forced command on the panel
key's `authorized_keys` line, the sudo rule and the dispatcher. An update moves
those by a proven switch.

The host lanes' configuration (`daily.env`, `stack-update.env`,
`managed-services.json`) and the panel's UI port declaration are
`reconcile/roles/catena-admin/tasks/lanes.yml`, so a host that converges itself
keeps them current.

## Why it is a bootstrap role

A reconcile that could mint, move or trust another key could rewrite what a
reconcile is. The accounts, the panel's key and the host key it trusts are
what every dispatch, and so every reconcile the panel starts, authenticates
with, and nothing on the host can prove a replacement before it is in use.
The migration account's rules are here for the same reason: nothing can prove
them from the host without an open migration window.

## Where its variables live

Here: everything read only by `tasks/main.yml` or one of its templates.

In `playbooks/group_vars/all/main.yml`: the panel's hostname, subdomain, UI
port, state dir, stats dir and default theme, the runner account and its home,
and the panel's SSH dir. `reconcile/roles/dispatch_path` and
`reconcile/roles/catena-admin` read those too, and `reconcile/roles/oauth2_proxy`
resolves `catena_admin_hostname` by NAME at a point where no role's defaults
are reliably in scope. The migration account's name, grant file and forced
command are there too, because `bootstrap/roles/common` puts the account in
`AllowUsers` and validates it. A role default is only dependable once that role
has run, so a value two roles either side of the boundary share belongs to the
product.

## Ordering

Right after `docker` in `converge.yml`, immediately before
`reconcile/roles/dispatch_path`, which needs the runner account, the panel's
key and `known_hosts`. It is absent from `playbooks/reconcile.yml`.
