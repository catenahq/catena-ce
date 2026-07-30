# Security policy

## Reporting a vulnerability

Email **security@catena.run**. Please include a reproduction (or the
closest you can get), the affected component (admin shell, installer,
an Ansible role, a shipped script), and the impact as you understand
it. English or French.

- Acknowledgement within 2 business days.
- We ask for coordinated disclosure: give us 90 days (or a mutually
  agreed window) before publishing details.
- No bug bounty at this time; credit in the release notes on request.

## Scope

This repository ships the Catena Community base: the installer/CLI
(`ansible/catena`, `ansible/seed.py`), the Ansible roles + playbooks,
the host-side scripts under `ansible/scripts/`, and the admin panel's
deployment surface (`ansible/roles/catena-admin`, including the compose
file). There is no Go in this tree.

The admin panel binary is not built from this tree. It ships as a public
container image, `ghcr.io/catenahq/catena-admin`, pullable anonymously,
with a **plain, non-obfuscated build**: the image can be inventoried
against its published CycloneDX SBOM and scanned with any tooling.
Reports against the panel go to the same address, and the fastest report
is a scanner finding against the published digest -- see
[verify what you run](https://docs.catena.run/en/trust/verify-what-you-run/).

Reports against the hosted services (catena.run, docs.catena.run) are
welcome at the same address.

Out of scope: vulnerabilities in the upstream applications the catalog
deploys (Nextcloud, Keycloak, etc.) -- report those upstream; we track
and ship the fixed versions through the managed-update pipeline.

## Supported versions

Pre-1.0: only the latest `main` (and the most recent tagged release,
once tags are cut) receives fixes.
