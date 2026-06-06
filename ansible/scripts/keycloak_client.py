"""Keycloak admin-REST helper for dashboard-sync: union each gated host's
/oauth2/callback into the shared oauth2-proxy client's redirectUris.

Carved out of dashboard-sync.py (see BACKLOG_TECHNICAL.md "dashboard-sync.py
decomposition"). Stdlib-only; installed beside dashboard-sync on the host.
"""
from __future__ import annotations

import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dokploy_api  # noqa: E402


def sync_redirect_uris(hosts, env):
    """UNION each gated host's /oauth2/callback into the shared
    oauth2-proxy Keycloak client's redirectUris via the manage-clients
    service account. Never removes URIs -- a converge re-import of
    realm-oauth2-proxy.yaml.j2 resets the list to infra hosts; this re-adds
    the client-app ones on the next run. No-op without the SA secret or
    when there is nothing to add. Non-fatal on error."""
    secret = (env.get("DASHBOARD_SYNC_CLIENT_SECRET") or "").strip()
    if not secret:
        print("dashboard-sync: no service-account secret; skipping redirect-URI sync.")
        return
    if not hosts:
        return
    want = {f"https://{h}/oauth2/callback" for h in hosts}
    try:
        tok = dokploy_api.http_json(
            env["KEYCLOAK_TOKEN_URL"],
            {"content-type": "application/x-www-form-urlencoded"},
            body=urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": env["DASHBOARD_SYNC_CLIENT_ID"],
                "client_secret": secret,
            }),
            method="POST",
        )
        access = tok["access_token"]
        auth_hdr = {"authorization": f"Bearer {access}", "accept": "application/json"}
        clients = dokploy_api.http_json(
            f"{env['KEYCLOAK_CLIENTS_API']}?clientId={env['OAUTH2_PROXY_CLIENT_ID']}",
            auth_hdr, method="GET",
        )
        if not clients:
            print(
                f"dashboard-sync/warn: oauth2-proxy client "
                f"{env['OAUTH2_PROXY_CLIENT_ID']!r} not found; skipping "
                f"redirect-URI sync.",
                file=sys.stderr,
            )
            return
        client = clients[0]
        existing = set(client.get("redirectUris") or [])
        if want <= existing:
            return
        client["redirectUris"] = sorted(existing | want)
        dokploy_api.http_json(
            f"{env['KEYCLOAK_CLIENTS_API']}/{client['id']}",
            {"authorization": f"Bearer {access}", "content-type": "application/json"},
            body=client, method="PUT",
        )
        print(f"dashboard-sync: added {len(want - existing)} redirect URI(s) to "
              f"{env['OAUTH2_PROXY_CLIENT_ID']}.")
    except (RuntimeError, KeyError, IndexError) as e:
        print(f"dashboard-sync/warn: redirect-URI sync failed: {e}", file=sys.stderr)
