# Catena-CE

Catena installs a curated list of open-source, state-of-the-art services and software to a server or VPS, making it ready to run production services. Some of the highlights include:
- Automated installation to a server with [Ansible](https://docs.ansible.com/)
- Fully hardened installation with [Tailscale](https://tailscale.com/)/[Headscale](https://headscale.net) administrator access
- Secure application access and DDoS protection with [Cloudflared](https://github.com/p2-inc/phasetwo-containers)
- Scheduled, incremental, encrypted backups with [restic](https://restic.net/)
- Container management interface with [Portainer](https://www.portainer.io/)
- Identity and Access Management with [Phase Two](https://phasetwo.io/) [Keycloak](https://github.com/p2-inc/phasetwo-containers)
- Authentication for any application that does not include it with [OAuth2 Proxy](https://oauth2-proxy.github.io/oauth2-proxy/)
- Automated app routing with [Traefik](https://doc.traefik.io/)
- Application status, uptime monitoring, and alerts with [Gatus](https://gatus.io/) and [Healthchecks](https://healthchecks.io/)
- System state monitoring and alerts with [Beszel](https://www.beszel.dev/)
- Pre-configured [Catena templates](https://github.com/catenahq/catena-templates)
- Updates, CVE monitoring, and unified settings with Catena-Admin

## Installation

### Requirements

- Cloudflare account and a domain name, along with an API token with `Account -> Cloudflare Tunnel -> Edit` and `Zone -> DNS -> Edit` permissions for your domain
- Tailscale Oauth client id/secret pair or a working Headscale control server and credentials
- Tailscale client installed on your local computer, with `tailscale` running and connected to the tailnet. Visit the [tailscale download page](https://tailscale.com/download)
- `uv` installed on your local machine for python virtual environment management: `wget -qO- https://astral.sh/uv/install.sh | sh`
- A fresh VPS or server running `Debian 13` (tested)
- An S3 Object storage endpoint along with access/secret keys pair for your backups

### Steps


1. Clone this repo and configure variables:
```sh
git clone -b main https://github.com/catenahq/catena-ce.git
cd catena-ce
cp -r ansible/inventory/example ansible/inventory/prod
mv ansible/inventory/prod/.env.example ansible/inventory/prod/.env
```

2. Edit `ansible/inventory/prod/.env` with your own information, then:
3. Launch installation:

```sh
./catena prod install
```

4. Enter your VPS user's password and tailscale client id/secret when prompted. **Those are never written to disk**. The installer with then complete the installation on your VPS.

5. Save your **restic encryption key** and **ops user password** shown at the end of installation to your password manager along with your Taiscale credentials and Cloudflare API token. You need the restic password to read and restore from your backups, and the ops password to log in to your VPS from your provider's console if tailscale access is somehow unavailable.

6. Login to the Catena-Admin interface at `http://<your-vps-tailnet-ip>:9010` and go to the `Settings` tab to finish installation.
   - Enter your Cloudflare API token to activate access to your apps through their subdomain URLs
   - Enter your S3 credentials to enable backups
  
7. Install applications from the [Catena templates](https://github.com/catenahq/catena-templates) catalog or configure your own based on them.


