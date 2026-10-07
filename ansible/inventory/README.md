# Inventory

One subdirectory per server: `inventory/<name>/`, gitignored. In a checkout
of this repository the installer (`uv run catena-installer`) keeps its
inventories here; installed from PyPI (`uvx catena-installer`) it keeps them
in the user's data directory. `catena-installer init --inventory <name>`
creates one with its `.env`, every key at its default and explained; the
installer page creates one and writes the `.env` from its form. Both render it
from [../helpers/knobs.yml](../helpers/knobs.yml), where a key, its default and
its explanation are declared once. `catena-installer install --inventory
<name>` then reads it rather than writing it, and creates `hosts.yml` from
`../skel/` on that same first run. `-i <file>` generates both from an input
file instead (the bench / operator path).

| File | What it holds |
| --- | --- |
| `.env` | What the installer needs to reach and install the server: its address and login, the SSH key, the admin email, the storage layout. Everything else is set in the panel. The installer sends it with each leg of the install, and the server reads it for that run only. |
| `hosts.yml` | The server's name (the first host of the `vps` group), which becomes its hostname, and an address there when one is written in (the bench repoints a copy at another machine that way). Auto-created from `../skel/hosts.yml.example`. |
| `.catena-installer.json` | Written by the installer page: the install's state and the installer that runs it. |

## No secrets here

An inventory holds configuration, never credentials. The install takes
none but an optional admin password pin, which goes to a transient 0600
file on the server for the run and is then deleted; vendor credentials are
entered in the panel, and everything else is minted on the server and
stored at `/etc/catena/config.json`. See [../SECRETS.md](../SECRETS.md) for
the full classification.

That is why a lost laptop costs nothing: the inventory is reconstructible
configuration, and no copy of it decrypts a backup.
