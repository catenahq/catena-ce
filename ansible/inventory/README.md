# Inventory

One subdirectory per deployment: `inventory/<name>/`, gitignored.
`catena-cli init --inventory <name>` creates one with its `.env`, every key at
its default and explained; the graphical installer (`uv run catena-gui`)
creates one and writes the `.env` from its form. Both render it from
[../helpers/knobs.yml](../helpers/knobs.yml), where a key, its default and its
explanation are declared once. `catena-cli install --inventory <name>` then
reads it rather than writing it, and creates `hosts.yml` itself from
`../skel/` on that same first run. `bootstrap.yml` records the address it
installed on, and `catena-cli` folds it into `hosts.yml`. `-i install.yaml`
generates everything from scratch instead (the bench / power-user path).

| File | What it holds |
| --- | --- |
| `hosts.yml` | The two host groups: `vps` (the ops account at the address bootstrap recorded) and `bootstrap` (the provider's initial login at the SSH address, `HOST_SSH_ADDRESS` or else the public IP, used only by `bootstrap.yml`). Auto-created from `../skel/hosts.yml.example` on first `catena-cli install` -- every field reads from `.env`. |
| `.env` | What the installer needs to reach and install the server: its address and login, the SSH key, the admin email, the storage layout. Everything else is set in the panel. |
| `.bootstrap-output.yml` | Written by `bootstrap.yml`; carries the install address later flows read back. |
| `.catena-gui.json` | Written by the graphical installer: the install's state and the launcher that runs it. |

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
