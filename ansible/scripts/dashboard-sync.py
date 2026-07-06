#!/usr/bin/env python3
"""Auto-discover Portainer-deployed stacks and sync their Traefik gate-route
files. Runs via systemd timer (every 5 min) and on-demand via the
catena-admin Actions tab "Sync all" button."""
# Managed by Ansible (roles/infrastructure). Do not edit by hand.
# /usr/local/bin/dashboard-sync -- generate per-app *-auto-gate.yml route
# files under {{ traefik_dynamic_dir }} based on compose
# vps.auth.* labels.
#
# Phase 4b retired the Homepage services.yaml write path. The catena-admin
# Apps tab now renders the launcher tile grid live from the Dokploy API
# at request time (no precomputed YAML), so this script's only remaining
# job is gate-route synthesis + the per-app oauth2-proxy provisioning that
# enforces it.
#
# This file is a THIN ORCHESTRATOR. The concerns were carved out of the
# former ~760-line monolith (see BACKLOG_TECHNICAL.md "dashboard-sync.py
# decomposition") into sibling stdlib-only modules installed beside it by
# roles/infrastructure/tasks/dashboard_sync.yml:
#   dokploy_api          -- Dokploy REST + generic JSON HTTP.
#   gate_routes          -- walk projects, write *-auto-gate.yml route files.
#   route_synth          -- render route YAML; resolve_access (delegates to
#                           the canonical helpers/labels_schema).
#   clients_provisioner  -- build + (re)deploy the oauth2-proxy-clients compose.
#   keycloak_client      -- union per-app /oauth2/callback redirect URIs.
#   labels_schema        -- canonical vps.* label vocabulary (shipped flat;
#                           imported by gate_routes/route_synth). The former
#                           inlined copy + its drift test are gone.
#   mailbox_sync         -- (opt-in) docker-mailserver mailbox reconciler.
#
# Uses stdlib-only (urllib) so there are no Python deps on the host
# beyond the default python3 install.
#
# Env vars (rendered by roles/infrastructure into /etc/catena/dashboard-sync.env):
#   PORTAINER_API_BASE     -- e.g. http://<tailnet-ip>:9000/api
#   PORTAINER_API_KEY      -- from vault (vault_portainer_api_key)
#   (Client-app hosts come from the compose vps.route.host label, not a
#    domain API; infra stacks to skip come from INFRA_COMPOSE_NAMES below.)
#
# Gate-route auto-discovery + per-app proxy provisioning (always on):
#   TRAEFIK_DYNAMIC_DIR   -- Dokploy's Traefik dynamic-config dir
#   AUTH_HOSTNAME         -- auth.<zone>; never gates itself
#   INFRA_COMPOSE_NAMES   -- comma-separated infra compose appNames whose
#                            gate routes + proxies are Ansible-managed
#   AUTH_FORCE_HTTPS_MW   -- middleware name (force X-Forwarded-Proto: https)
#   OAUTH2_PROXY_*        -- image/port/cookie/issuer/client + shared
#                            secrets for the per-app instances (see
#                            clients_provisioner.build_clients_compose)
#   KEYCLOAK_TOKEN_URL / KEYCLOAK_CLIENTS_API / DASHBOARD_SYNC_CLIENT_* --
#                            manage-clients service account for the
#                            redirect-URI union (see
#                            keycloak_client.sync_redirect_uris)
#
# CONVENTION FOR AUTO-GATING TO WORK: a stack deployed via Portainer
# (outside the Ansible-managed infra list) must declare a public host via
# `vps.route.host` AND include a stable network alias on catena-network
# matching the LOWERCASED-SLUGIFIED form of its Portainer stack Name
# (i.e., lowercase + non-[a-z0-9] replaced with `-`). Examples:
#     stack "myblog"  -> alias `myblog`
#     stack "MyBlog"  -> alias `myblog`
#     stack "B2-Test" -> alias `b2-test`
#
# Matching compose snippet:
#
#     services:
#       app:
#         networks:
#           catena-network:
#             aliases: [b2-test]
#
# Without the alias, dashboard-sync still writes the route, but Traefik
# can't resolve the backend hostname and the app returns 502.

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clients_provisioner  # noqa: E402
import gate_routes  # noqa: E402
import keycloak_client  # noqa: E402
from dokploy_api import http_json  # noqa: E402


def _env(name, default=None, required=True):
    v = os.environ.get(name, default)
    if required and not v:
        print(f"dashboard-sync: missing env var {name}", file=sys.stderr)
        sys.exit(2)
    return v


def main():
    api_base = _env("PORTAINER_API_BASE")
    api_key = _env("PORTAINER_API_KEY")

    dyn_dir = Path(_env("TRAEFIK_DYNAMIC_DIR"))
    proxy_port = _env("OAUTH2_PROXY_INTERNAL_PORT", default="4180", required=False)

    written, removed, specs, hosts = gate_routes.sync_gate_routes(
        api_base=api_base,
        api_key=api_key,
        dyn_dir=dyn_dir,
        infra_compose_names=_env("INFRA_COMPOSE_NAMES"),
        auth_hostname=_env("AUTH_HOSTNAME"),
        force_https_mw=_env("AUTH_FORCE_HTTPS_MW"),
        proxy_port=proxy_port,
    )
    if written or removed:
        print(f"dashboard-sync: gate routes -- wrote {written}, removed "
              f"{removed} (in {dyn_dir})")

    # Provision the per-app oauth2-proxy instances + register their
    # callback redirect URIs. Both read the OAUTH2_PROXY_* / KEYCLOAK_* /
    # DASHBOARD_SYNC_* env rendered by roles/infrastructure.
    env = {
        k: os.environ.get(k, "")
        for k in (
            "OAUTH2_PROXY_CLIENTS_COMPOSE", "OAUTH2_PROXY_IMAGE",
            "OAUTH2_PROXY_INTERNAL_PORT", "OAUTH2_PROXY_COOKIE_NAME",
            "OAUTH2_PROXY_OIDC_ISSUER", "OAUTH2_PROXY_LOGIN_URL",
            "OAUTH2_PROXY_REDEEM_URL", "OAUTH2_PROXY_JWKS_URL",
            "OAUTH2_PROXY_CLIENT_ID",
            "OAUTH2_PROXY_CLIENT_SECRET", "OAUTH2_PROXY_COOKIE_SECRET",
            "CLOUDFLARE_ZONE", "KEYCLOAK_TOKEN_URL", "KEYCLOAK_CLIENTS_API",
            "DASHBOARD_SYNC_CLIENT_ID", "DASHBOARD_SYNC_CLIENT_SECRET",
        )
    }
    clients_provisioner.provision_clients_compose(
        api_base, api_key, specs, env)
    keycloak_client.sync_redirect_uris(hosts, env)

    # Mailbox provisioning for the (opt-in) mailserver template. Isolated in
    # its own module so a failure here cannot affect the auth-provisioning
    # path above; non-fatal. No-ops when the template is not deployed.
    try:
        import mailbox_sync
        mailbox_sync.reconcile(env, http_json)
    except Exception as e:  # noqa: BLE001 -- never let mailbox sync break the run
        print(f"dashboard-sync/warn: mailbox sync failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
