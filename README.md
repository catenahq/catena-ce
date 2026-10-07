# Catena-CE

Catena installs a curated list of open-source services and software to a computer or VPS, making it a suitable production environment in which to run business applications. A thin dashboard wrapper allows for configuration and orchestration of this infrastructure. Some of the highlights include:
- Automated installation to a server: the server installs itself with [Ansible](https://docs.ansible.com/), from the release it runs
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
- `uv` on your local machine, Windows, macOS or Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`, or on Windows `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- A fresh VPS or server running the current Debian stable release, its public address and its initial login (`root`, `debian`...)
- An SSH key pair on your local machine, or let the installer create one. Most providers install the public key given when the server is ordered; otherwise the installer installs it once with the provider's password for the initial login
- The email address of the administrator, which is the panel's login

The installer reaches the server over SSH and asks for nothing else. The server downloads the Catena release and installs itself with that release's own code, which the installer streams back to your screen.

#### In the panel, afterwards
- An S3 Object storage endpoint along with access/secret keys pair for your backups
- Optionally, either or both of:
  - a Tailscale OAuth client id/secret pair with the `Auth Keys -> Write` and `Devices -> Core -> Read` scopes, or a working Headscale control server and credentials, with the `tailscale` client running and connected to that tailnet on your local computer. Visit the [Tailscale download page](https://tailscale.com/download)
  - a Cloudflare account and a domain name, along with an API token with `Account -> Cloudflare Tunnel -> Edit` and `Zone -> DNS -> Edit` permissions for your domain

### Steps

1. Start the installer:
```sh
uvx catena-installer
```
It opens a browser page on this machine. The `Inventory` tab lists your inventories and creates new ones; they are kept in your user data directory (`~/.local/share/catena/inventory` on Linux, `~/Library/Application Support/catena/inventory` on macOS, `%APPDATA%\catena\inventory` on Windows). The `Installation` tab asks for the target (its address, SSH port, initial user and that user's password) and the configuration (the admin email and the SSH key file, which it can create), each field with its explanation behind a `(?)`. `Verify configuration and start installation` saves the page to the inventory's `.env`, so a closed installer resumes where it stopped, tests it against the server (its SSH banner, then whether the key opens the initial login, else whether the initial user's password does) and starts the install, whose output shows on the page. **The initial user's password is never saved**: it stays in memory until the install ends. The `Catena server access` section below the output shows the passwords as soon as the server has generated them, and links to the panel once the install ends.

   The console is the alternative. `init` creates the inventory and its `.env`, every key at its default and explained; fill it in, then run the install:
```sh
uvx catena-installer init --inventory prod
uvx catena-installer install --inventory prod
```
   It asks for the provider's password for the initial login only when your key does not open it already. **The password is not persisted anywhere after the install**.

2. The installer logs in, has the server fetch the newest Catena release and install itself, logs in again as `ops` with your key before anything closes the provider's login, and checks the server from your machine when the install ends. Public SSH stays open until you choose `Lockdown` in the panel.

3. Save the **admin password**, the **console password for `ops`** and the **journal verification key** to your password manager. The installer shows them once the server is bootstrapped, a few minutes in, while the rest of the install runs, and again when it ends; the page shows them in its `Catena server access` section. You need the console password to log in from your provider's console if SSH is unavailable. Running the install again shows the passwords again; the journal key is shown once.

4. Log in to the Catena-Admin interface with the admin email and password. The installer keeps a connection to the server open while it runs, through the `panel` account, which can do nothing but forward, and its page links the panel and Portainer. `uvx catena-installer connect --inventory prod` opens that connection again; from a machine without the installer, the same forward is:
```sh
ssh -N -L 9010:127.0.0.1:9010 -L 9000:127.0.0.1:9000 panel@<your-server-address>
```
   The server's address is its public IP, or its tailnet IP once the lockdown has closed public SSH. Then go to the `Settings` tab to finish the installation:
   - Enter your S3 endpoint and credentials, then `Generate backup encryption password` and save it to your password manager: you need it to read and restore your backups
   - Enter the Cloudflare domain and API token to publish your apps; the panel is then also at `https://dash.<your-domain>`
   - Enter the tailnet credentials, then apply the `Lockdown` to join the tailnet and close public SSH. The server reopens public SSH by itself while its tailnet is down, and closes it again when the tailnet is back
   - Keep your Tailscale credentials and Cloudflare API token in your password manager

5. Install applications from the [Catena templates](https://github.com/catenahq/catena-templates) catalogue or configure your own based on them.


## Why Tailscale and Cloudflared

1. Access without a public/static IP: tunnels provide a direct access to the server regardless of whether it's being CGNAT or on an internal or public network
2. No firewall configuration: the tunnels are created directly between the server and the tunnel access provider, bypassing routers and port forwarding
3. DDoS and spam protection: with no open ports but key-only SSH, which the lockdown closes too once administration is on the tailnet, all connections go through the tunnels and, in the case of Cloudflare, through their firewalls.

Alternatives to Tailscale include Headscale and Netbird. Cloudflare alternatives include Pangolin. Hosting these services securely requires an additional VPS and creates a single, unprotected point of entry. This is a privacy vs security trade-off.

## Day 2 operations

Day 2 operations run from the Catena-Admin panel on the server: settings, backups, the tunnel, the private network, the lockdown, updates and restores. A rollback restores an earlier snapshot on the same server, and a lost server is recovered by installing Catena on a new one and restoring from the old server's backup repository.

The installer's console verbs:

```sh
uvx catena-installer install   --inventory prod                   # re-apply the release the server runs
uvx catena-installer install   --inventory prod --release vX.Y.Z  # move the server to that release
uvx catena-installer connect   --inventory prod                   # open the panel and Portainer here
uvx catena-installer uninstall --inventory prod                   # hand unattended-upgrades back to the OS
```

`install` run again on an installed server asks nothing, re-applies everything with the release the server records, the install-only roles the panel cannot apply included, validates the server, and shows the passwords again. It is the way in should the panel be unavailable, which could happen if the disk is full. `--release` moves the server to that release with that release's own code and records it, the same end state as the panel's update: the way forward when the panel's own update cannot run. Once the panel's `Lockdown` has closed public SSH, add `--address <tailnet-ip>` for that run, or set `HOST_SSH_ADDRESS` in the inventory's `.env` to keep using it; `--tags` limits the converge to some roles.

From a checkout of this repository, `uv run catena-installer` runs the same installer, with the inventories in `ansible/inventory/`.