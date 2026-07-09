#!/usr/bin/env python3
"""On-box config store for Catena (0b client-owned config).

Single plaintext source of truth at ``/etc/catena/config.json`` (0600 root).
Two top-level sections:

    {
      "secrets": {            # category (a) external + (b) internal-generated
        "vault_admin_password": "...",
        "vault_catena_postgres_password": "...",
        ...
      },
      "config": {             # category (c) non-secret
        "CLOUDFLARE_ZONE": "...",
        ...
      }
    }

The converge loads this store, mints any MISSING *internal* secret
(reconcile-not-overwrite -- an existing value is never touched), writes the
store back 0600, and emits the merged secret view as JSON for an Ansible
``set_fact``. It replaces the laptop-side SOPS vault: the box is the source
of truth, and ``/etc`` is in ``roles/backup`` ``backup_paths`` so the store
rides every restic snapshot -- a restore returns every secret with the data.

Design constraints:
  - stdlib only. Runs on a minimal target host whose system python has no
    PyYAML (same reason ``extract_secrets_core`` hand-rolls its YAML). JSON
    is stdlib and round-trips base64 / url-safe secret values exactly.
  - INTERNAL secrets are minted here; EXTERNAL secrets (vendor creds the
    client supplies) are only ever *stored*, never generated -- they arrive
    via the two-phase bootstrap or the catena-admin settings API.
  - Format contracts for the minted values match seed.py exactly (oauth2
    cookie length-after-decode, Healthchecks 32-char API keys, url-safe
    ping key, 20-char admin password).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets as _secrets
import sys
from pathlib import Path
from typing import Callable

DEFAULT_STORE_PATH = "/etc/catena/config.json"


# --- minters (format contracts mirror seed.py) ------------------------------
def mint_strong_password() -> str:
    """48 random bytes -> 64 base64 chars. Matches ``openssl rand -base64 48``."""
    return base64.b64encode(os.urandom(48)).decode("ascii")


def mint_hc_api_key() -> str:
    """Healthchecks API keys must be EXACTLY 32 chars. 16 bytes hex = 32."""
    return _secrets.token_hex(16)


def mint_url_safe() -> str:
    """URL-path-safe 32-char string (Healthchecks ping_key lives in URL paths)."""
    return _secrets.token_urlsafe(24)


def mint_oauth2_proxy_cookie_secret() -> str:
    """oauth2-proxy decodes --cookie-secret with base64.RawURLEncoding and
    rejects any decoded length that isn't 16/24/32 bytes. Mint 32 bytes
    url-safe; oauth2-proxy trims ``=`` padding before decoding."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def mint_admin_password() -> str:
    """20-char url-safe admin password (Portainer + Keycloak). token_urlsafe(15)
    returns ceil(15*4/3) = 20 chars."""
    return _secrets.token_urlsafe(15)


# --- secret registries ------------------------------------------------------
# INTERNAL: generated on-box, reconcile-not-overwrite. No human ever supplies
# these. Mirrors seed.py's _resolve_admin_password / _resolve_restic_password /
# _resolve_service_secrets groups.
INTERNAL_SECRETS: dict[str, Callable[[], str]] = {
    "vault_admin_password": mint_admin_password,
    "vault_backup_restic_password": mint_strong_password,
    "vault_catena_postgres_password": mint_strong_password,
    "vault_turn_static_auth_secret": mint_strong_password,
    # SSO service credentials.
    "vault_keycloak_db_password": mint_strong_password,
    "vault_oauth2_proxy_cookie_secret": mint_oauth2_proxy_cookie_secret,
    "vault_oauth2_proxy_client_secret": mint_strong_password,
    "vault_dashboard_sync_client_secret": mint_strong_password,
    "vault_nextcloud_oidc_client_secret": mint_strong_password,
    "vault_element_oidc_client_secret": mint_strong_password,
    "vault_mailserver_oidc_client_secret": mint_strong_password,
    "vault_mailserver_introspect_client_secret": mint_strong_password,
    # Healthchecks (self-hosted heartbeat instance).
    "vault_healthchecks_secret_key": mint_strong_password,
    "vault_healthchecks_superuser_password": mint_strong_password,
    "vault_healthchecks_ping_key": mint_url_safe,
    "vault_healthchecks_api_key_readonly": mint_hc_api_key,
    "vault_healthchecks_api_key_readwrite": mint_hc_api_key,
    # Nextcloud Talk + HPB bearer secrets.
    "vault_nextcloud_talk_signaling_secret": mint_strong_password,
    "vault_nextcloud_talk_internal_secret": mint_strong_password,
    # Rocket.Chat-bundled Jitsi component secrets.
    "vault_jitsi_prosody_password": mint_strong_password,
    "vault_jitsi_jicofo_auth_password": mint_strong_password,
    "vault_jitsi_jicofo_component_secret": mint_strong_password,
    "vault_jitsi_jvb_auth_password": mint_strong_password,
    # Element-bundled Jitsi + jigasi secrets.
    "vault_element_jitsi_jicofo_auth_password": mint_strong_password,
    "vault_element_jitsi_jicofo_component_secret": mint_strong_password,
    "vault_element_jitsi_jvb_auth_password": mint_strong_password,
    "vault_element_jigasi_xmpp_password": mint_strong_password,
    # Beszel resource-monitor credentials (minted unconditionally; idle until
    # BESZEL_ENABLED=true).
    "vault_beszel_admin_password": mint_strong_password,
    "vault_beszel_universal_token": mint_url_safe,
}

# EXTERNAL: vendor credentials the client supplies. Stored, never minted. The
# optional ones may legitimately be empty. vault_portainer_api_key is minted
# by Portainer itself (bootstrap_portainer_admin.py), not here -- it is
# neither internal-minted nor client-supplied, so it lives in neither set and
# is written into the store by the portainer role after it mints it.
EXTERNAL_SECRETS: frozenset[str] = frozenset({
    "vault_tailscale_oauth_client_id",
    "vault_tailscale_oauth_client_secret",
    "vault_cloudflare_api_token",
    "vault_backup_s3_access_key",
    "vault_backup_s3_secret_key",
    "vault_backup_worm_access_key",
    "vault_backup_worm_secret_key",
    "vault_nextcloud_worm_access_key",
    "vault_nextcloud_worm_secret_key",
    "vault_smtp_password",
    "vault_mailserver_relay_password",
    "vault_mailserver_spamhaus_dqs_key",
    "vault_nextcloud_s3_access_key",
    "vault_nextcloud_s3_secret_key",
})


# --- store I/O --------------------------------------------------------------
def load(path: str | Path = DEFAULT_STORE_PATH) -> dict:
    """Load the store. Returns a dict with 'secrets' and 'config' sub-dicts
    (empty when the file is absent). A malformed file is a hard error -- we
    must never silently start minting fresh secrets over a corrupt store."""
    p = Path(path)
    if not p.exists():
        return {"secrets": {}, "config": {}}
    raw = json.loads(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level is not an object")
    return {
        "secrets": dict(raw.get("secrets") or {}),
        "config": dict(raw.get("config") or {}),
    }


def dump(store: dict, path: str | Path = DEFAULT_STORE_PATH) -> None:
    """Atomically write the store 0600 root. Write to a temp sibling then
    rename so a crash mid-write can't leave a half-written store."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"secrets": store.get("secrets", {}), "config": store.get("config", {})},
        indent=2,
        sort_keys=True,
    )
    tmp = p.with_suffix(p.suffix + ".tmp")
    # 0600 from creation: never let the store exist group/other-readable even
    # for the window between write and chmod.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(p))
    os.chmod(str(p), 0o600)


# --- reconcile --------------------------------------------------------------
def ensure_internal_secrets(store: dict) -> list[str]:
    """Mint every INTERNAL secret missing (or blank) from the store. Never
    overwrites an existing non-empty value (reconcile-not-overwrite -- an
    operator/settings rotation is preserved). Returns the list of keys minted."""
    secrets_map = store.setdefault("secrets", {})
    minted: list[str] = []
    for key, minter in INTERNAL_SECRETS.items():
        cur = secrets_map.get(key)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            secrets_map[key] = minter()
            minted.append(key)
    return minted


def adopt(store: dict, mapping: dict | None) -> list[str]:
    """Capture pre-existing secret values into the store, fill-only. Used once
    at migration to seed the store from the values already in scope (the SOPS
    vault the converge is transitioning off of). Never overwrites a value
    already in the store and never stores a blank. Accepts ANY key -- the
    caller pre-filters to vault_* -- so vault_portainer_api_key and any
    out-of-registry secret are captured too. Returns the keys adopted."""
    secrets_map = store.setdefault("secrets", {})
    adopted: list[str] = []
    for key, val in (mapping or {}).items():
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        if secrets_map.get(key):
            continue
        secrets_map[key] = val
        adopted.append(key)
    return adopted


def apply_inputs(
    store: dict,
    *,
    secrets_in: dict | None = None,
    config_in: dict | None = None,
    overwrite: bool = False,
) -> list[str]:
    """Merge caller-supplied external secrets + non-secret config into the
    store. By default only fills MISSING/blank keys (first-bootstrap seeding);
    ``overwrite=True`` replaces existing values (a settings-page edit). Blank
    provided values are ignored -- they never clear an existing secret. Only
    keys in EXTERNAL_SECRETS are accepted into secrets (a typo can't smuggle
    an internal-secret override in). Returns the list of keys changed."""
    changed: list[str] = []
    if secrets_in:
        secrets_map = store.setdefault("secrets", {})
        for key, val in secrets_in.items():
            if key not in EXTERNAL_SECRETS:
                raise ValueError(
                    f"{key} is not an external secret; internal secrets are "
                    "minted on-box and cannot be supplied"
                )
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            if not overwrite and secrets_map.get(key):
                continue
            if secrets_map.get(key) != val:
                secrets_map[key] = val
                changed.append(key)
    if config_in:
        config_map = store.setdefault("config", {})
        for key, val in config_in.items():
            if not overwrite and key in config_map:
                continue
            if config_map.get(key) != val:
                config_map[key] = val
                changed.append(key)
    return changed


# --- CLI --------------------------------------------------------------------
def _parse_kv(pairs: list[str]) -> dict:
    out: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"expected KEY=VALUE, got {pair!r}")
        k, _, v = pair.partition("=")
        out[k.strip()] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=DEFAULT_STORE_PATH,
                    help=f"store path (default {DEFAULT_STORE_PATH})")
    ap.add_argument("--set-secret", action="append", default=[], metavar="KEY=VAL",
                    help="set an external secret (repeatable)")
    ap.add_argument("--set-config", action="append", default=[], metavar="KEY=VAL",
                    help="set a non-secret config value (repeatable)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace existing values instead of filling only blanks")
    ap.add_argument("--no-mint", action="store_true",
                    help="do not mint missing internal secrets (seed-only)")
    ap.add_argument("--adopt-stdin", action="store_true",
                    help="read a JSON object of existing {key: value} secrets "
                         "from stdin and adopt them fill-only before minting "
                         "(one-time migration capture from the SOPS vault)")
    ap.add_argument("--emit", choices=["secrets", "all", "none"], default="secrets",
                    help="what to print as JSON on stdout (default: secrets, "
                         "for an Ansible set_fact of the vault_* names)")
    args = ap.parse_args(argv)

    store = load(args.path)
    if args.adopt_stdin:
        raw = sys.stdin.read().strip()
        if raw:
            incoming = json.loads(raw)
            if not isinstance(incoming, dict):
                raise SystemExit("--adopt-stdin: expected a JSON object")
            adopt(store, incoming)
    apply_inputs(
        store,
        secrets_in=_parse_kv(args.set_secret),
        config_in=_parse_kv(args.set_config),
        overwrite=args.overwrite,
    )
    if not args.no_mint:
        ensure_internal_secrets(store)
    dump(store, args.path)

    if args.emit == "secrets":
        print(json.dumps(store.get("secrets", {})))
    elif args.emit == "all":
        print(json.dumps(store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
