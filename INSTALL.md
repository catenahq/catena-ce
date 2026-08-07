# Installing Catena Community

## Before starting

One fresh Debian VPS, and [`uv`](https://docs.astral.sh/uv/) on the
machine driving the install. That is the whole toolchain -- `uv` pulls in
ansible-core, and there is no encryption tool to install and no key to
have in scope.

Four vendor credentials have to exist first. Catena cannot generate
these; they come from each provider's console:

| Credential | Where it comes from | Used for |
| --- | --- | --- |
| Cloudflare API token | Cloudflare dashboard: `Account > Cloudflare Tunnel > Edit` plus `Zone > DNS > Edit` | The encrypted tunnel and its DNS records |
| Tailscale OAuth client id + secret | Tailscale admin console, scope `Auth Keys: Write`, tag `tag:vps` | Joining the server to the private network |
| S3 access key + secret key | The object-storage provider holding the backup bucket | The restic backup repository |
| SMTP or mail-relay password | The mail provider | Admin emails, password resets, etc. |

## Install

```
./catena install
```

`install` collects the configuration, mints every internal secret on the
server itself, then runs the four flows in order:
`preflight -> bootstrap -> site -> validate`.

Run `./catena` with no arguments for an interactive menu of every
operation.

For an unattended run, answer the questions once into a file and pass it:

```
./catena install -i install.yaml --no-confirm
```

## Write down what the installer shows once

At the end of a successful install, Catena prints three values it cannot
recover later:

- **Admin password** -- signs in to Portainer and Keycloak.
- **Restic encryption password**, alongside the **repo URL** and **S3
  keys** already supplied.

Those last three together are the entire disaster-recovery keyset: with
them alone a wiped VPS can be rebuilt onto fresh hardware. Without the
restic password the backups cannot be decrypted by anyone, Catena
included. They belong in a password manager before the terminal closes.

Every other secret -- database passwords, OIDC client secrets, service
tokens -- stays on the server at `/etc/catena/config.json` and rides
every backup, so it never has to be held anywhere else. Full
classification: [ansible/SECRETS.md](ansible/SECRETS.md).

## Day two

```
./catena converge   # re-apply after a configuration or app change
./catena validate   # on-host + tailnet + external health checks
./catena backup     # take an on-demand snapshot
./catena restore    # in-place whole-host restore
./catena recover    # rebuild onto a FRESH replacement box
./catena uninstall  # hand unattended-upgrades back to the OS
```

Each takes `--inventory <name>` to pick a deployment when there is more
than one.

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
