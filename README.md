# Catena Community

Catena installs a curated catalog of open-source business apps onto a
single VPS, wires them behind one sign-on, and adds monitoring, backups
and whole-host restore -- all driven from one command. No telemetry, no
license required, no vendor lock-in. Source-available (fair-code).

Community is a complete, standalone product, not a trial edition: the
full app catalog plus the whole base lifecycle -- one-command install,
single sign-on across every app, monitoring basics, a weekly backup and
on-demand snapshots, and whole-host restore onto a fresh replacement
box. Every operation runs through the `catena` CLI, and every byte of
data stays in standard formats readable with standard tools, with or
without Catena.

The north star is one sentence, and everything else in this repository
serves it:

> A Catena server can be rebuilt from nothing but its backup storage
> endpoint and the backup key.

> A managed **Catena Pro** edition adds the catena-admin web panel plus
> operated extras on top (offsite immutable backups, automated updates,
> and more). The panel is a convenience layer over the same host-native
> automation in this repository -- removing it takes away neither the
> data nor the ability to operate the suite. Details at
> [catena.run](https://catena.run).

## Install

[INSTALL.md](INSTALL.md) -- prerequisites, the vendor credentials to
have ready, the install command, and the day-two operations.

## How it works

A deployment passes through five flows, in order:

```
preflight  ->  bootstrap  ->  site  ->  validate          (+ restore for DR)
```

- **preflight** checks the supplied Tailscale OAuth client before any
  VPS is touched.
- **bootstrap** hardens a fresh VPS (user, SSH, ufw, docker) and joins it
  to the private network.
- **site** is the converge: networking, Portainer, sign-on, backup, the
  admin panel. Safe to re-run; it is how every later change is applied.
- **validate** checks the result from three vantage points -- on the
  host, over the private network, and from the public internet.
- **restore** is the disaster-recovery path.

The installer composes them. Nothing in this tree is invoked with raw
`ansible-playbook`.

## Layout

| Path | What is in it |
| --- | --- |
| [ansible/](ansible/) | Everything that deploys a server. Start at [ansible/README.md](ansible/README.md). |
| [ansible/catena](ansible/catena) | The installer / CLI entry point. |
| [ansible/playbooks/](ansible/playbooks/) | The five flows plus the day-two operations. |
| [ansible/bootstrap/roles/](ansible/bootstrap/roles/) | Operator-run, from outside the server (account, network, disk, container engine). |
| [ansible/reconcile/roles/](ansible/reconcile/roles/) | What a server runs against itself (traefik, postgres, keycloak, backup, ...). |
| [ansible/helpers/](ansible/helpers/) | Python shared by the installer, the roles, and three host-side reconcilers. |
| [ansible/scripts/](ansible/scripts/) | Executables installed on the server and run there. |
| [ansible/inventory/](ansible/inventory/) | Per-deployment configuration. Only `example/` is tracked. |
| [ansible/tests/](ansible/tests/) | Unit tests, plus the external probes `validate.yml` runs. |

Every one of those directories carries its own `README.md` describing
its children.

| File | What it is |
| --- | --- |
| [SPEC.md](SPEC.md) | What this repository promises. Hand-written; every claim points at a machine-checked gate, and a claim whose gate disappears fails the build. |
| [VALIDATION.md](VALIDATION.md) | What is tested and how much. Generated; drift fails CI. |
| [ansible/SECRETS.md](ansible/SECRETS.md) | Every secret: who holds it, where it lives, what happens if it is lost. |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability. |
| [LICENSE](LICENSE) | Fair-code terms. |

## What is not in this repository

The catena-admin web panel is not built from this tree. It ships as a
public container image, `ghcr.io/catenahq/catena-admin`, pullable
anonymously by anyone: the binary ships as built, the image is signed,
and its component inventory (CycloneDX SBOM) is published alongside it,
so the thing that runs on a server can be inspected and scanned without
asking Catena for anything.
[Verify what you run](https://docs.catena.run/en/trust/verify-what-you-run/)
walks through it.

The panel is a convenience layer over the host-native automation here.
Every operation it drives -- backup, restore, validate, converge -- runs
from this repository without it, which is what keeps its absence from
being a lock-in.

## Develop

Requires `uv`. This tree is Ansible and Python only; there is no Go in
it.

```
cd ansible
uv run pytest
```
