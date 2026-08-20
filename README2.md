# Catena-CE

Catena installs a curated list of open-source services and software to a computer or VPS, making it a suitable production environment in which to run business applications. Some of the highlights include:
- Automated installation to a server with [Ansible](https://docs.ansible.com/)
- Fully hardened installation with [Tailscale](https://tailscale.com/)/[Headscale](https://headscale.net)-only administrator access
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
- Tailscale Oauth client id/secret pair or a working Headscale control server and credentials
- Tailscale client installed on your local computer, with `tailscale` running and connected to the tailnet. Visit the [tailscale download page](https://tailscale.com/download)
- `uv` installed on your local machine for python virtual environment management: `wget -qO- https://astral.sh/uv/install.sh | sh`
- A fresh VPS or server running `Debian 13` (tested)

#### Post-install
- Cloudflare account and a domain name, along with an API token with `Account -> Cloudflare Tunnel -> Edit` and `Zone -> DNS -> Edit` permissions for your domain
- An S3 Object storage endpoint along with access/secret keys pair for your backups

### Steps


1. Clone this repo and configure variables:
```sh
git clone -b main https://github.com/catenahq/catena-ce.git
cd catena-ce
cp -r ansible/inventory/example ansible/inventory/prod
mv ansible/inventory/prod/.env.example ansible/inventory/prod/.env
```

1. Edit `ansible/inventory/prod/.env` with your own values
2. Launch installation:

```sh
./catena prod install
```

4. Enter your tailscale client id/secret and VPS user's password when prompted. **Those are not persisted anywhere after the install**. The installer with then complete the installation on your VPS.

5. Save your **restic encryption key**, **ops user password** and **application admin password** shown at the end of installation to your password manager along with your Taiscale credentials and Cloudflare API token. You need the restic password to read and restore from your backups, and the ops password to log in to your VPS from your provider's console if tailscale access is somehow unavailable.

6. Login to the Catena-Admin interface at `http://<your-vps-tailnet-ip>:9010` with the admin email and password and go to the `Settings` tab to finish installation.
   - Enter your Cloudflare API token to activate access to your apps through their subdomain URLs
   - Enter your S3 credentials to enable backups
  
7. Install applications from the [Catena templates](https://github.com/catenahq/catena-templates) catalog or configure your own based on them.


## Why Tailscale and Cloudflared

1. Access without a public/static IP: tunnels provide a direct access to the server regardless of whether it's being CGNAT or on an internal or public network
2. No firewall configuration: the tunnels are created directly between the server and the tunnel access provider, bypassing routers and port forwarding
3. DDoS and spam protection: with no open ports and no direct access through the IP address, all connections go through the tunnels and, in the case of Cloudflare, through their firewalls.

## Day 2 operations

The `catena` cli provides other options than `install`:

```sh
./catena prod converge   # re-apply after a configuration or app change
./catena prod validate   # on-host + tailnet + external health checks
./catena prod backup     # take an on-demand snapshot
./catena prod restore    # in-place whole-host restore
./catena prod recover    # rebuild onto a FRESH replacement box
./catena prod uninstall  # hand unattended-upgrades back to the OS
```

Some of these options are also available from the Catena-Admin app. The recommended method is to always use the Catena-Admin app running on the server to perform the operations, but the `catena` CLI is available as a break-glass should the panel be unavailable (which could happen if the disk is full).
