# Inventory

One subdirectory per deployment: `inventory/<name>/`. The installer
writes it during `catena install` and rewrites parts of it as the flows
learn things (`bootstrap.yml` records the tailnet address a host got).
Hand-editing works too, and nothing here is generated from a source
elsewhere.

Real inventories are gitignored. Only `example/` is tracked, and it is
the documented schema rather than a working config.

| File | What it holds |
| --- | --- |
| `hosts.yml` | The two host groups: `vps` (steady state, reached over the tailnet) and `bootstrap` (public IP, used only by `bootstrap.yml` before port 22 closes). |
| `localhost.yml` | Anchors localhost to this inventory so controller-side plays such as `preflight.yml` have an `inventory_dir`. Committed as-is. |
| `.env` | Non-secret deployment configuration: domain, zone, sizing, feature toggles. |
| `group_vars/all/main.yml` | Ansible variables that do not belong in `.env`. |
| `.bootstrap-output.yml` | Written by `bootstrap.yml`; carries facts later flows read back. |

## No secrets here

An inventory holds configuration, never credentials. The vendor
credentials `catena install` collects go to a transient 0600 file that
the CLI passes to the converge and then deletes; everything else is
minted on the server and stored at `/etc/catena/config.json`. See
[../SECRETS.md](../SECRETS.md) for the full classification.

That is why a lost laptop costs nothing: the inventory is reconstructible
configuration, and no copy of it decrypts a backup.
