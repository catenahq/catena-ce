# Catena-CE

Catena installs a curated list of open-source services and software to a computer or VPS, making it a suitable production environment in which to run business applications. A thin dashboard wrapper allows for configuration and orchestration of this infrastructure. Some of the highlights include:
- Automated installation to a server with [Ansible](https://docs.ansible.com/)
- Fully hardened installation, with administrator access over [Tailscale](https://tailscale.com/)/[Headscale](https://headscale.net) and public SSH closed once that path is proven
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

#### Pre-install
- `uv` installed on your local machine for python virtual environment management: `wget -qO- https://astral.sh/uv/install.sh | sh`
- A fresh VPS or server running `Debian 13` (tested), its public address and its initial login (`root`, `debian`...). The graphical installer needs the server to already accept your SSH key for that login, which most providers set up from the public key given when the server is ordered; the command-line installer can instead install the key with the provider's password
- At least one way into the Catena-Admin panel once the install ends, or both:
  - a Tailscale OAuth client id/secret pair with the `Auth Keys -> Write` and `Devices -> Core -> Read` scopes, or a working Headscale control server and credentials (an API key lets a lockdown applied later from the panel confirm the server is online), with the `tailscale` client running and connected to that tailnet on your local computer. Visit the [Tailscale download page](https://tailscale.com/download)
  - a Cloudflare account and a domain name, along with an API token with `Account -> Cloudflare Tunnel -> Edit` and `Zone -> DNS -> Edit` permissions for your domain

  The install runs over SSH to the server's public address either way. The one left out is entered in the panel's `Settings` afterwards.

#### Post-install
- An S3 Object storage endpoint along with access/secret keys pair for your backups. The graphical installer also accepts them on its `Backup` page

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
It opens a browser page on this machine. The first page lists the inventories in `ansible/inventory/` and creates new ones. Every field starts from its default value where one makes sense, each page checks its answers before moving on, and the answers are saved to `ansible/inventory/<name>/.env` as the pages advance, so a closed installer resumes where it stopped. **Credentials (Tailscale, Headscale, Cloudflare, S3) are never saved**: they stay in memory until the install ends and are asked for again when the inventory is reopened. The last page starts the install, and its output shows in the console and on the page.

   The command-line installer is the alternative. Copy the template, fill in the `.env`, then run the install:
```sh
mkdir ansible/inventory/prod
cp ansible/inventory/example/.env.example ansible/inventory/prod/.env
uv run catena-cli install --inventory prod
```
   It asks for the provider's password for the initial login (blank when the server already accepts your key), the tailnet credentials when the access method is the tailnet, and the Cloudflare API token when the `.env` names a domain. **Those are not persisted anywhere after the install**.

3. The installer completes the installation on your server. With a tailnet, its last step joins the server to it and closes public SSH.

4. Save the **admin password**, the **restic backup password**, the **console password for `ops`** and the **journal verification key** shown at the end of the installation to your password manager, along with your Tailscale credentials and Cloudflare API token. You need the restic password to read and restore your backups, and the console password to log in from your provider's console if the tailnet is unavailable. `uv run catena-cli show-keyset --inventory <name>` shows the passwords again; the journal key is shown once.

5. Log in to the Catena-Admin interface at `https://dash.<your-domain>` (with Cloudflare) or `http://<your-server-tailnet-ip>:9010` (with a tailnet) with the admin email and password, and go to the `Settings` tab to finish the installation:
   - Enter the access method you did not give at install: the Cloudflare domain and API token to publish your apps, or the tailnet credentials, then apply the lockdown to join the tailnet and close public SSH
   - Enter your S3 credentials to enable backups, if the installer did not take them

6. Install applications from the [Catena templates](https://github.com/catenahq/catena-templates) catalogue or configure your own based on them.


## Why Tailscale and Cloudflared

1. Access without a public/static IP: tunnels provide a direct access to the server regardless of whether it's being CGNAT or on an internal or public network
2. No firewall configuration: the tunnels are created directly between the server and the tunnel access provider, bypassing routers and port forwarding
3. DDoS and spam protection: with no open ports and no direct access through the IP address, all connections go through the tunnels and, in the case of Cloudflare, through their firewalls.

Alternatives to Tailscale include Headscale and Netbird. Cloudflare alternatives include Pangolin. Hosting these services securely requires an additional VPS and creates a single, unprotected point of entry. This is a privacy vs security trade-off.

## Day 2 operations

The `catena-cli` CLI provides other options than `install`. Run them from the
repository root, verb first:

```sh
uv run catena-cli converge         --inventory prod  # re-apply after a configuration or app change
uv run catena-cli validate         --inventory prod  # on-host + tailnet + external health checks
uv run catena-cli backup           --inventory prod  # take an on-demand snapshot
uv run catena-cli rotate-tunnel    --inventory prod  # mint a new Cloudflare tunnel
uv run catena-cli rotate-tailscale --inventory prod  # re-authenticate to the private network
uv run catena-cli show-keyset      --inventory prod  # show the passwords and first-login URLs again
uv run catena-cli uninstall        --inventory prod  # hand unattended-upgrades back to the OS
```

A verb that runs one playbook carries that playbook's name, so
`catena-cli converge` runs `playbooks/converge.yml`. Running `catena-cli` with no
arguments prompts for the inventory and the operation.

Restores run from the Catena-Admin panel: a rollback restores an earlier
snapshot on the same server, and a lost server is recovered by installing
Catena on a new one and restoring from the old server's backup repository.

Some of these options are also available from the Catena-Admin app. The recommended method is to always use the Catena-Admin app running on the server to perform the operations, but the `catena-cli` CLI is available as a break-glass should the panel be unavailable (which could happen if the disk is full).