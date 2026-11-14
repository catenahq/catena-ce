# Playbooks

The playbooks a Catena server is installed and maintained with, all run
on the server itself. The install (`../install-host.sh`) runs
`accounts.yml` on its first leg, then `bootstrap.yml`,
`show-keyset.yml`, `converge.yml` and `validate.yml` in that order, and
`uninstall.yml` for `catena-installer uninstall`. The catena-admin
engines run `reconcile.yml` for the host's own converges, and
`lockdown.yml`, `rotate-tailscale.yml` and `rotate-tunnel.yml` for the
panel's actions. Each file here is a single atomic unit with no
cross-playbook imports, and opens with a comment that says what it does.

`ansible-playbook` is not a supported entry point -- install-host.sh and
the host engines write the inventory, thread the credentials and order
the runs.

Subdirectories: `filter_plugins/` and `lookup_plugins/` hold the Python
that Ansible loads by convention from beside the playbooks (non-trivial
transforms live in Python with a unit test rather than in Jinja);
`tasks/` holds task files included by more than one playbook.
