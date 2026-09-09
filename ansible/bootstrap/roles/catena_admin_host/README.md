# bootstrap/roles/catena_admin_host

The host-side half of the admin panel: the trust path its dispatch arrives over,
and the env files the host lanes read.

- the runner account and its home
- the sudoers drop-in and the sshd `AcceptEnv` allow-list
- the forced command and the runner's `authorized_keys`
- the SSH client config the panel's container uses
- `daily.env`, `auto-update.env`, `stack-update.env`, `managed-services.json`

## Why it is a role rather than a file in reconcile/roles/catena-admin

Because the boundary it sits on has to be enforceable.

`boundary.yml` declared `host.yml` bootstrap-side from phase 1a, and that
declaration could not act on anything: `reconcile/roles/catena-admin/tasks/main.yml`
imported the file unconditionally, and `playbooks/reconcile.yml` runs that role.
So for as long as the two halves shared a role, a reconcile could rewrite the
forced command -- the one thing the split exists to prevent -- while every
assertion about the FILES passed.

Holding the line without splitting meant a conditional import plus a play that
remembered to disable it. That works and it is one edit away from not working.
Two roles is the shape where the property is a fact about the tree.

## Where its variables live

Here: everything read only by `tasks/main.yml` or one of its templates.

In `playbooks/group_vars/all/main.yml`: the panel's hostname, subdomain, UI
port, state dir, stats dir and default theme. Both halves read those, and
`reconcile/roles/oauth2_proxy` resolves `catena_admin_hostname` by NAME at a point where
neither role's defaults are reliably in scope. A role default is only dependable
once that role has run, so a value two roles either side of the boundary share
belongs to the product.

## Ordering

Immediately before `reconcile/roles/catena-admin` in `site.yml`, which is where these
tasks ran when they were a file inside it. It is deliberately absent from
`playbooks/reconcile.yml`.
