# reconcile/roles/public_ports

Wires the declarative public-port registry into the host. The registry is the
single applier for every port this server exposes outside the Cloudflare
tunnel.

Infra roles (coturn, portainer, infrastructure, catena-admin) drop a JSON
fragment into `public_ports_fragment_dir` declaring their ports; app templates
declare theirs through `vps.expose.*` compose labels, harvested live. The
reconciler that merges both and applies ufw and DOCKER-USER rules ships in the
catena-admin payload (`payload/lanes/catena-public-ports.py`, its modules in
`payload/lib`, its units in `payload/lanes/systemd`).

This role owns what belongs to the host: the fragment directory, the
`docker.service` drop-in that re-applies the rules on every docker start
(Docker recreates DOCKER-USER empty), and enabling the timer. It fails the
converge when the payload it follows has no reconciler, because that host
would publish its restricted ports unguarded.

## Where its variables live

`public_ports_fragment_dir` is in `playbooks/group_vars/all/main.yml`, not
here. Six roles write into it, and a role default is only reliably in scope
once that role has run -- a constant six roles share belongs to the product
rather than to one of them.

## Ordering

It runs right after `reconcile/roles/payload`, which installs the reconciler
and its units, and before every role that drops a fragment.
