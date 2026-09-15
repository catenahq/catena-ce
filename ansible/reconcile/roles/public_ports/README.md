# reconcile/roles/public_ports

The declarative public-port registry: the single applier for every port this
server exposes outside the Cloudflare tunnel.

Infra roles (coturn, portainer, infrastructure) drop a JSON fragment into
`public_ports_fragment_dir` declaring their ports; app templates declare theirs
through `vps.expose.*` compose labels, harvested live.
`scripts/catena-public-ports.py` merges both and applies ufw and DOCKER-USER
rules. That script carries the full rationale.

## Why it is its own role

It was task file inside `bootstrap/roles/common`, which is bootstrap-side -- so the
boundary declared it reconcile-side and nothing could act on that, because
`common/tasks/main.yml` imported it unconditionally.

The distinction is real. The firewall's default-deny policy is bootstrap's: get
it wrong and there is no way back in. What this installs is a reconciler that
opens what the deployed applications declare, which is version-shaped work a
host can redo for itself whenever the product ships a better version of the
script.

## Where its variables live

`public_ports_lib_dir` and `public_ports_fragment_dir` are in
`playbooks/group_vars/all/main.yml`, not here. Six roles read the fragment dir,
and a role default is only reliably in scope once that role has run -- a
constant six roles share belongs to the product rather than to one of them.

The cadence (`public_ports_timer_on_boot`, `public_ports_timer_interval`) is
here, because only this role's own timer template reads it.

## Ordering

It runs early, before the roles that drop fragments, which is where it sat when
it was part of `common`. Nothing it does needs Docker: it creates the
`docker.service.d` drop-in directory itself, and systemd reads drop-ins only
once `docker.service` exists.
