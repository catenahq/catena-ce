# bootstrap/roles/catena_admin_host

The host-side half of the admin panel: the trust path its dispatch arrives over.

- the runner account and its home
- the sudoers drop-in and the sshd `AcceptEnv` allow-list
- the forced command and the runner's `authorized_keys`
- the SSH client config the panel's container uses
- the bind-mount sources the panel's service needs before it can start

The host lanes' configuration (`daily.env`, `stack-update.env`,
`managed-services.json`) and the panel's port declarations are
`reconcile/roles/catena-admin/tasks/lanes.yml`, so a host that converges itself
keeps them current.

## Why it is a role rather than a file in reconcile/roles/catena-admin

A reconcile that could rewrite the forced command could rewrite what a
reconcile is. `playbooks/reconcile.yml` runs `reconcile/roles/catena-admin`, so
the trust path lives in a role that playbook does not name, and the boundary is
a fact about the tree rather than a conditional import.

## Where its variables live

Here: everything read only by `tasks/main.yml` or one of its templates.

In `playbooks/group_vars/all/main.yml`: the panel's hostname, subdomain, UI
port, state dir, stats dir and default theme. Both halves read those, and
`reconcile/roles/oauth2_proxy` resolves `catena_admin_hostname` by NAME at a point where
neither role's defaults are reliably in scope. A role default is only dependable
once that role has run, so a value two roles either side of the boundary share
belongs to the product.

## Ordering

Immediately before `reconcile/roles/catena-admin` in `converge.yml`. It is
absent from `playbooks/reconcile.yml`.
