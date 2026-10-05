# Catena-CE

Catena installs a curated list of open-source services and software to a computer or VPS, making it a suitable production environment in which to run business applications. A thin dashboard wrapper allows for configuration and orchestration of this infrastructure. Some of the highlights include:
- Automated installation to a server with [Ansible](https://docs.ansible.com/)
- Fully hardened installation: key-only SSH with no root and no password login, the admin panel reached through an SSH forward, and optionally [Tailscale](https://tailscale.com/)/[Headscale](https://headscale.net), over which the panel's Lockdown closes public SSH
- Secure application access and DDoS protection with [Cloudflared](https://github.com/cloudflare/cloudflared)
- Scheduled, incremental, encrypted backups with [restic](https://restic.net/)
- Container management interface with [Portainer](https://www.portainer.io/)
- Identity and Access Management with [Phase Two](https://phasetwo.io/) [Keycloak](https://github.com/p2-inc/phasetwo-containers)
- Authentication for any application that does not include it with [OAuth2 Proxy](https://oauth2-proxy.github.io/oauth2-proxy/)
- Automated app routing with [Traefik](https://doc.traefik.io/)
- Application status, uptime monitoring, and alerts with [Gatus](https://gatus.io/) and [Healthchecks](https://healthchecks.io/)
- System state monitoring and alerts with [Beszel](https://www.beszel.dev/)
- Pre-configured [Catena templates](https://github.com/catenahq/catena-templates)
- Unified settings with Catena-Admin container

## Installation

### Requirements

#### Install
- `uv` installed on your local machine for python virtual environment management: `wget -qO- https://astral.sh/uv/install.sh | sh`
- A fresh VPS or server running the current Debian stable release, its public address and its initial login (`root`, `debian`...)
- An SSH key pair on your local machine. Most providers install the public key given when the server is ordered; otherwise the installer installs it once with the provider's password for the initial login
- The email address of the administrator, which is the panel's login

The installer reaches and installs the server over SSH, and asks for nothing else.

#### In the panel, afterwards
- An S3 Object storage endpoint along with access/secret keys pair for your backups
- Optionally, either or both of:
  - a Tailscale OAuth client id/secret pair with the `Auth Keys -> Write` and `Devices -> Core -> Read` scopes, or a working Headscale control server and credentials, with the `tailscale` client running and connected to that tailnet on your local computer. Visit the [Tailscale download page](https://tailscale.com/download)
  - a Cloudflare account and a domain name, along with an API token with `Account -> Cloudflare Tunnel -> Edit` and `Zone -> DNS -> Edit` permissions for your domain

### Steps

1. Clone this repository:
```sh
git clone -b main https://github.com/catenahq/catena-ce.git
cd catena-ce
```

2. Start the graphical installer from the repository root:
```sh
uv run catena-gui
```
It opens a browser page on this machine. The `Inventory` tab lists the inventories in `ansible/inventory/` and creates new ones. The `Installation` tab asks for the target (its address, SSH port, initial user and that user's password) and the configuration (the admin email and the SSH key file), each field with its explanation behind a `(?)`. `Verify configuration and start installation` saves the page to `ansible/inventory/<name>/.env`, so a closed installer resumes where it stopped, tests it against the server (its SSH banner, then whether the key opens the initial login, else whether the initial user's password does) and starts the install, whose output shows on the page. **The initial user's password is never saved**: it stays in memory until the install ends. The `Catena server access` section below the output shows the passwords as soon as the server has generated them, and the command that opens the panel.

   The command-line installer is the alternative. `init` creates the inventory and its `ansible/inventory/prod/.env`, every key at its default and explained; fill it in, then run the install:
```sh
uv run catena-cli init --inventory prod
uv run catena-cli install --inventory prod
```
   It asks for the provider's password for the initial login only when your key does not open it already. **The password is not persisted anywhere after the install**.

3. The installer completes the installation on your server. Public SSH stays open until you choose `Lockdown` in the panel.

4. Save the **admin password**, the **console password for `ops`** and the **journal verification key** to your password manager. The installer shows them once the server is bootstrapped, a few minutes in, while the rest of the install runs, and again when it ends; the graphical installer shows them in its `Catena server access` section. You need the console password to log in from your provider's console if SSH is unavailable. Running `uv run catena-cli install --inventory <name>` again shows the passwords again; the journal key is shown once.

5. Log in to the Catena-Admin interface with the admin email and password at `http://localhost:9010`, through an SSH forward as the `panel` account, which can do nothing but forward:
```sh
ssh -N -L 9010:127.0.0.1:9010 panel@<your-server-address>
```
   The server's address is its public IP, or its tailnet IP once the lockdown has closed public SSH. Portainer is reached the same way on port `9000`. Then go to the `Settings` tab to finish the installation:
   - Enter your S3 endpoint and credentials, then `Generate backup encryption password` and save it to your password manager: you need it to read and restore your backups
   - Enter the Cloudflare domain and API token to publish your apps; the panel is then also at `https://dash.<your-domain>`
   - Enter the tailnet credentials, then apply the `Lockdown` to join the tailnet and close public SSH. The server reopens public SSH by itself while its tailnet is down, and closes it again when the tailnet is back
   - Keep your Tailscale credentials and Cloudflare API token in your password manager

6. Install applications from the [Catena templates](https://github.com/catenahq/catena-templates) catalogue or configure your own based on them.


## Why Tailscale and Cloudflared

1. Access without a public/static IP: tunnels provide a direct access to the server regardless of whether it's being CGNAT or on an internal or public network
2. No firewall configuration: the tunnels are created directly between the server and the tunnel access provider, bypassing routers and port forwarding
3. DDoS and spam protection: with no open ports but key-only SSH, which the lockdown closes too once administration is on the tailnet, all connections go through the tunnels and, in the case of Cloudflare, through their firewalls.

Alternatives to Tailscale include Headscale and Netbird. Cloudflare alternatives include Pangolin. Hosting these services securely requires an additional VPS and creates a single, unprotected point of entry. This is a privacy vs security trade-off.

## Day 2 operations

Day 2 operations run from the Catena-Admin panel on the server: settings, backups, the tunnel, the private network, the lockdown, updates and restores. A rollback restores an earlier snapshot on the same server, and a lost server is recovered by installing Catena on a new one and restoring from the old server's backup repository.

Besides `init`, the `catena-cli` CLI has two verbs. Run them from the repository root, verb first:

```sh
uv run catena-cli install   --inventory prod  # install, or re-apply the configuration from this machine
uv run catena-cli uninstall --inventory prod  # hand unattended-upgrades back to the OS
```

`install` run again on an installed server asks nothing, re-applies everything, the operator-run roles the panel cannot apply included, validates the server, and shows the passwords again. It is the way in should the panel be unavailable, which could happen if the disk is full. Once the panel's `Lockdown` has closed public SSH, add `--address <tailnet-ip>` for that run, or set `HOST_SSH_ADDRESS` in the inventory's `.env` to keep using it; `--tags` limits the converge to some roles. Running `catena-cli` with no arguments prompts for the inventory and the operation.