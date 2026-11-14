# Scripts

Executables copied onto the server by a role in `../reconcile/roles/`
and run there -- by a systemd timer, by a role task, or by the admin
panel through its dispatcher. That is the split against `../helpers/`:
helpers are imported, scripts are installed. Each file opens with a
comment that says what it does.

A file in this directory is part of the deployed system, so changing one
changes what a server does on its next converge.

The route + SSO reconciler and the public-port reconciler ship in the
catena-admin payload (`payload/lanes/` and `payload/lib/`): what a
`vps.*` label MEANS and how a declared port becomes a firewall rule
change when the product changes rather than when the host does, so they
travel with the image. What changes with the host stays with the
converge: dashboard-sync's env file and timer interval, and the port
declarations the roles write into `/etc/catena/public-ports.d/`.
