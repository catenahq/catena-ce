# Inventory

One subdirectory per deployment: `inventory/<name>/`. The graphical
installer (`uv run catena-gui`) creates one and writes its `.env` page by
page; by hand, copy `example/.env.example` here as `.env` and fill it in.
`catena-cli install --inventory <name>` then reads it rather than writing
it, and creates `hosts.yml` itself from `../skel/` on that same first
run. `bootstrap.yml` records the address it installed on,
and `catena-cli` folds it into `hosts.yml`. `-i install.yaml` generates
everything from scratch instead (the bench / power-user path).

Real inventories are gitignored. Only `example/.env.example` is tracked,
and it is GENERATED from
[../helpers/knobs.yml](../helpers/knobs.yml) by
`python3 helpers/render_knobs.py --write`: a key, its default and its
explanation are declared there once, for this template and for the store
and panel that read the same value later. `hosts.yml` has no
per-deployment variation to document -- `../skel/` carries it.

| File | What it holds |
| --- | --- |
| `hosts.yml` | The two host groups: `vps` (the ops account at the address bootstrap recorded) and `bootstrap` (the provider's initial login at the public IP, used only by `bootstrap.yml`). Auto-created from `../skel/hosts.yml.example` on first `catena-cli install` -- every field reads from `.env`. |
| `.env` | What the installer needs to reach and install the server: its address and login, the SSH key, the admin email, the storage layout. Everything else is set in the panel. |
| `.bootstrap-output.yml` | Written by `bootstrap.yml`; carries the install address later flows read back. |
| `.catena-gui.json` | Written by the graphical installer: its page, the install's state, the keyset acknowledgement. |

Ansible variables that do not belong in `.env` (structure reading it, not
per-deployment values) live once at
[../playbooks/group_vars/all/main.yml](../playbooks/group_vars/all/main.yml),
shared across every inventory. A deployment that needs its own addition
drops a file under its own `group_vars/all/`.

## No secrets here

An inventory holds configuration, never credentials. The install takes
none but an optional admin password pin, which goes to a transient 0600
file that the CLI passes to the converge and then deletes; vendor
credentials are entered in the panel, and everything else is minted on the
server and stored at `/etc/catena/config.json`. See
[../SECRETS.md](../SECRETS.md) for the full classification.

That is why a lost laptop costs nothing: the inventory is reconstructible
configuration, and no copy of it decrypts a backup.
