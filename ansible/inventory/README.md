# Inventory

One subdirectory per deployment: `inventory/<name>/`. Copy
`example/.env.example` here as `.env` and fill it in -- `catena <name>
install` then reads it rather than writing it, and creates `hosts.yml` +
`localhost.yml` itself from `../skel/` on that same first run.
`bootstrap.yml` rewrites one field afterward (the tailnet address a host
got, once it joins). `-i install.yaml` generates everything from scratch
instead (the bench / power-user path); nothing here is generated from a
source elsewhere either way.

Real inventories are gitignored. Only `example/.env.example` is tracked as
the documented schema; `hosts.yml`/`localhost.yml` have no per-deployment
variation to document -- `../skel/` carries those.

| File | What it holds |
| --- | --- |
| `hosts.yml` | The two host groups: `vps` (steady state, reached over the tailnet) and `bootstrap` (public IP, used only by `bootstrap.yml` before port 22 closes). Auto-created from `../skel/hosts.yml.example` on first `catena install` -- every field reads from `.env`. |
| `localhost.yml` | Anchors localhost to this inventory so controller-side plays such as `preflight.yml` have an `inventory_dir`. Auto-created from `../skel/localhost.yml`, committed as-is. |
| `.env` | Non-secret deployment configuration: host IP, domain, zone, sizing, feature toggles. |
| `.bootstrap-output.yml` | Written by `bootstrap.yml`; carries facts later flows read back. |

Ansible variables that do not belong in `.env` (structure reading it, not
per-deployment values) live once at
[../playbooks/group_vars/all/main.yml](../playbooks/group_vars/all/main.yml),
shared across every inventory. A deployment that needs its own addition
drops a file under its own `group_vars/all/`.

## No secrets here

An inventory holds configuration, never credentials. The vendor
credentials `catena install` collects go to a transient 0600 file that
the CLI passes to the converge and then deletes; everything else is
minted on the server and stored at `/etc/catena/config.json`. See
[../SECRETS.md](../SECRETS.md) for the full classification.

That is why a lost laptop costs nothing: the inventory is reconstructible
configuration, and no copy of it decrypts a backup.
