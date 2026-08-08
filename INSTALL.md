# Installing Catena Community

## Before starting

One fresh Debian VPS, and [`uv`](https://docs.astral.sh/uv/) on the
machine driving the install. That is the whole toolchain -- `uv` pulls in
ansible-core, and there is no encryption tool to install and no key to
have in scope.

Only one vendor credential has to exist first -- Catena cannot generate it,
and the server needs it to join the private network before anything else
can happen:

| Credential | Where it comes from | Used for |
| --- | --- | --- |
| Tailscale OAuth client id + secret | Tailscale admin console, scope `Auth Keys: Write`, tag `tag:vps` | Joining the server to the private network |

Everything else -- the Cloudflare API token, the S3 backup keys, SMTP --
is entered later, in catena-admin > Settings, once the server exists. None
of it is needed to install.

## Configure

Copy `.env.example` and fill it in:

```
mkdir -p ansible/inventory/prod
cp ansible/inventory/example/.env.example ansible/inventory/prod/.env
$EDITOR ansible/inventory/prod/.env
```

`.env` is commented inline -- what each field does, its default, when it's
safe to leave blank. `hosts.yml` needs no copying and no editing: `catena
install` creates it from `.env` on first run.

## Install

```
./catena prod install
```

`install` collects the Tailscale credential (the one thing `.env` can't
hold -- it's a secret), mints every internal secret on the server itself,
then runs the four flows in order: `preflight -> bootstrap -> site ->
validate`.

Run `./catena` with no arguments for an interactive menu that prompts for
the inventory name and the operation.

For an unattended run -- CI, a bench, an operator scripting many
installs -- generate the inventory instead of hand-editing it, from a
single answers file (the inventory name, host details, the same fields
as `.env`, plus the Tailscale credential; see `uv run ansible/seed.py
--help`):

```
./catena install -i install.yaml --no-confirm
```

## Write down what the installer shows once

At the end of a successful install, Catena prints three values it cannot
recover later:

- **Admin password** -- signs in to Portainer and Keycloak.
- **Restic encryption password** -- the backup encryption key.
- **Console password** -- break-glass login at the provider's KVM/serial
  console when the network path is gone (SSH refuses it; key-only).

Set the backup repo URL and S3 keys in catena-admin > Settings next, then
save those alongside the restic password: repo URL + S3 keys + restic
password together are the entire disaster-recovery keyset, and a wiped VPS
rebuilds from backup with nothing else. Without the restic password the
backups cannot be decrypted by anyone, Catena included. All three belong
in a password manager before the terminal closes.

Every other secret -- database passwords, OIDC client secrets, service
tokens -- stays on the server at `/etc/catena/config.json` and rides
every backup, so it never has to be held anywhere else. Full
classification: [ansible/SECRETS.md](ansible/SECRETS.md).

## Day two

```
./catena prod converge   # re-apply after a configuration or app change
./catena prod validate   # on-host + tailnet + external health checks
./catena prod backup     # take an on-demand snapshot
./catena prod restore    # in-place whole-host restore
./catena prod recover    # rebuild onto a FRESH replacement box
./catena prod uninstall  # hand unattended-upgrades back to the OS
```

`prod` is the inventory name -- whatever was used at install, to pick a
deployment when there is more than one.

`uninstall` deletes no apps and no data. It re-enables Debian's
`apt-daily-upgrade.timer` so the box keeps patching itself once Catena
stops managing it, then prints the remaining teardown steps (Portainer,
Cloudflare, Tailscale, the backup bucket) to be done deliberately.

## Where to look next

- [ansible/README.md](ansible/README.md) -- what each flow does and how
  the pieces fit together.
- [ansible/SECRETS.md](ansible/SECRETS.md) -- every secret, who holds it,
  and where it lives.
- [SPEC.md](SPEC.md) -- what this repository promises, with a
  machine-checked gate behind each promise.
