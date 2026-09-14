# SPEC.md

Catena-ce is an Ansible-based installer for Catena. It prepares a new Debian 13 server to host dockerized applications by providing a secure, ready-to-deploy platform.

AI agents: do not modify this file.

## Bootstrap vs Reconcile
The Ansible playbooks are split in two broad categories: Bootstrap and Reconcile
- `Bootstrap` includes all features that can not self repair. For example, using the initial default user to create a passwordless sudoer, setting its password and deleting the default user. This step expects the default user to exist and therefore can not be re-run.
- `Reconcile` includes everything else that can be repaired by re-running the playbooks.

## Bootstrap features
- Create a passwordless `ops` sudoer and generate its password
- Install and connect `Tailscale` to the user's mesh network
- Harden the server: close TCP/UDP ports
- Install the user's SSH public key to the server

## Reconcile Features
- Apply package updates
- Install and update Docker
- Install the Infrastructure Docker containers: `cloudflared`, `portainer`, `phasetwo-keycloak`, `gatus`, `beszel`, `healthchecks`, `catena-admin`


## CI
- Use https://github.com/catenahq/renovate and https://github.com/catenahq/renovate-config to keep dependencies up to date
- Use https://github.com/catenahq/scanctl to scan dependencies for vulnerabilities before merging the renovate PRs

## Installation

See README.md
