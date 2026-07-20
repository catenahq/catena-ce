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
``set_fact``. It supersedes the plaintext group_vars vault (SOPS+age was
dropped, 0b): the box is the source of truth, and ``/etc`` is in
``roles/backup`` ``backup_paths`` so the store
rides every restic snapshot -- a restore returns every secret with the data.

Design constraints:
  - stdlib only. Runs on a minimal target host whose system python has no
    PyYAML. JSON is stdlib and round-trips base64 / url-safe secret values
    exactly.
  - INTERNAL secrets are minted here; EXTERNAL secrets (vendor creds the
    client supplies) are only ever *stored*, never generated -- they arrive
    via the two-phase bootstrap or the catena-admin settings API.
  - USER_HELD secrets (the admin + restic passwords) are minted on-box IF
    ABSENT (like internal), but are the DR / first-login keyset the user must
    hold a copy of: the installer reads them back and shows them ONCE for the
    user's password manager. They are NOT settable through the config-write
    API (a restic-password change is a deliberate re-key action, not a passive
    settings save), and they remain ADOPTABLE so `catena recover` seeds the
    user's saved restic password into the store BEFORE the restore decrypts
    the backup.
  - Format contracts for the minted values match the historical seed.py
    (oauth2 cookie length-after-decode, Healthchecks 32-char API keys,
    url-safe ping key, 20-char admin password, 64-char base64 restic password).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
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
    """token_urlsafe(15) -> 20 url-safe chars. Portainer + Keycloak both
    accept it; matches the historical seed auto-mint length."""
    return _secrets.token_urlsafe(15)


# --- per-zone (multi-domain SSO island) helpers -----------------------------
def zone_slug(zone: str) -> str:
    """Key/filesystem-safe slug of a Cloudflare zone name: lowercase, every
    run of non-alphanumerics collapsed to a single underscore. e.g.
    ``example.com`` -> ``example_com``."""
    return re.sub(r"[^a-z0-9]+", "_", zone.strip().lower()).strip("_")


def zone_cookie_secret_key(zone: str) -> str:
    """Store key for a zone's oauth2-proxy cookie secret. In multi-domain mode
    each SSO island (one Cloudflare zone) gets its own cookie secret so a
    session cookie minted for one domain cannot be replayed against another."""
    return f"vault_oauth2_proxy_cookie_secret_{zone_slug(zone)}"


def configured_zone_names(zones: object) -> list[str]:
    """Extract zone names from a CLOUDFLARE_ZONES config value. Accepts a JSON
    string or a native list, each element either a plain zone string or a dict
    with a ``zone`` key. Order-preserving; blanks dropped."""
    if isinstance(zones, str):
        try:
            zones = json.loads(zones)
        except (ValueError, TypeError):
            return []
    out: list[str] = []
    for z in zones or []:
        name = z.get("zone") if isinstance(z, dict) else z
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


# --- secret registries ------------------------------------------------------
# INTERNAL: generated ON-BOX by the converge loader, reconcile-not-overwrite.
# No human ever supplies these and they NEVER leave the box (they are minted
# here, ride the restic backup inside the store, and return with the data on a
# restore). The admin + restic-backup passwords are NOT here: they are the
# user-held DR keyset / first-login credential -- also on-box-minted-if-absent
# but surfaced once for the user's password manager (see USER_HELD_SECRETS).
INTERNAL_SECRETS: dict[str, Callable[[], str]] = {
    "vault_catena_postgres_password": mint_strong_password,
    "vault_turn_static_auth_secret": mint_strong_password,
    # Signs the catena-admin native-login session cookie (the host-published
    # tailnet listener). Minted once, stable across converges, rides the backup.
    "vault_catena_admin_session_key": mint_strong_password,
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

# USER_HELD: the DR keyset + first-login credential. Minted on-box IF ABSENT
# (same reconcile-not-overwrite as INTERNAL), but the installer shows them
# ONCE so the user keeps an off-box copy in their password manager. NOT in
# EXTERNAL_SECRETS, so the config-write API (settings save) cannot set them:
#   - vault_admin_password    -- first-login credential (Portainer + Keycloak).
#   - vault_backup_restic_password -- encrypts the backup repo. Minting it
#     on-box would trap it inside the very snapshot it decrypts IF the user
#     lost their copy -- so it is surfaced once at install for the password
#     manager, and `catena recover` ADOPTS the user's saved value into the
#     store BEFORE the restore runs (adopt is fill-only, so the freshly-minted
#     value is only used on a first install, never a recover). A rotation is a
#     deliberate `restic key passwd` action in catena-admin, not a store write.
USER_HELD_SECRETS: dict[str, Callable[[], str]] = {
    "vault_admin_password": mint_admin_password,
    "vault_backup_restic_password": mint_strong_password,
}

# EXTERNAL: vendor credentials the client HOLDS (never on-box-minted). Stored,
# never minted here -- they arrive via the transient bootstrap adopt file or
# the catena-admin settings API. The optional ones may legitimately be empty.
# vault_portainer_api_key is minted by Portainer itself
# (bootstrap_portainer_admin.py), not here -- it is neither internal-minted nor
# client-supplied, so it lives in neither set and is written into the store by
# the portainer role after it mints it.
EXTERNAL_SECRETS: frozenset[str] = frozenset({
    "vault_tailscale_oauth_client_id",
    "vault_tailscale_oauth_client_secret",
    # Self-hosted Headscale control server (alternative to Tailscale SaaS).
    # api_key mints a short-lived pre-auth key per converge (preferred); the
    # static preauth_key is the fallback. Optional -- empty on Tailscale hosts.
    "vault_headscale_api_key",
    "vault_headscale_preauth_key",
    "vault_cloudflare_api_token",
    # Per-client GHCR pull token for the PRIVATE catena-admin image
    # (read:packages on ghcr.io/catenahq/catena-admin). Operator-issued;
    # the converge feeds it to Portainer as a registry credential so the
    # stack deploy can pull. Empty = catena-admin deploy is skipped.
    "vault_ghcr_pull_token",
    # Multi-domain (EE): JSON map zone -> API token. Each token is one-zone
    # scoped; catena-admin verifies the single-zone grant before storing. The
    # scalar vault_cloudflare_api_token stays for the CE single-domain path.
    "vault_cloudflare_api_tokens",
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
    # Per-zone oauth2-proxy cookie secrets: one SSO island per configured
    # Cloudflare zone (multi-domain, EE). Reconcile-not-overwrite like the
    # static internal set. Single-domain hosts have no CLOUDFLARE_ZONES entry
    # and mint nothing extra (the base vault_oauth2_proxy_cookie_secret stands).
    zones = store.get("config", {}).get("CLOUDFLARE_ZONES")
    for zone in configured_zone_names(zones):
        key = zone_cookie_secret_key(zone)
        cur = secrets_map.get(key)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            secrets_map[key] = mint_oauth2_proxy_cookie_secret()
            minted.append(key)
    return minted


def ensure_user_held_secrets(store: dict) -> list[str]:
    """Mint every USER_HELD secret (admin + restic passwords) missing or blank
    from the store, reconcile-not-overwrite. Runs AFTER adopt/apply_inputs so a
    value the user re-entered on `catena recover` (adopted before the restore)
    is preserved and only a genuine first install mints fresh. Returns the keys
    minted -- the installer surfaces these once for the user's password
    manager."""
    secrets_map = store.setdefault("secrets", {})
    minted: list[str] = []
    for key, minter in USER_HELD_SECRETS.items():
        cur = secrets_map.get(key)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            secrets_map[key] = minter()
            minted.append(key)
    return minted


def adopt(store: dict, mapping: dict | None) -> list[str]:
    """Capture pre-existing secret values into the store, fill-only. Used once
    at migration to seed the store from the values already in scope (the
    plaintext group_vars vault the converge seeds from). Never overwrites a value
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
                         "(migration capture from the plaintext group_vars vault)")
    ap.add_argument("--adopt-file", metavar="PATH",
                    help="like --adopt-stdin but read the JSON object from a "
                         "file (the Ansible loader stages the capture map to a "
                         "0600 host file; this ansible-core's script module has "
                         "no stdin passthrough)")
    ap.add_argument("--emit", choices=["secrets", "all", "none"], default="secrets",
                    help="what to print as JSON on stdout (default: secrets, "
                         "for an Ansible set_fact of the vault_* names)")
    ap.add_argument("--dispatch-stdin", action="store_true",
                    help="serve the catena-admin settings API: read a JSON "
                         "request {op: read|write, secrets, config} from stdin. "
                         "read -> print the full store; write -> apply the "
                         "external creds/config (overwrite, no mint) and print "
                         '{"ok": true}. Rejects internal-secret keys.')
    args = ap.parse_args(argv)

    # Settings-API dispatch (driven by the host runner on behalf of the admin
    # container / the bench). A distinct, minimal surface: no minting on write
    # (secrets mint at converge, not when the client edits vendor creds).
    if args.dispatch_stdin:
        req = json.loads(sys.stdin.read() or "{}")
        if not isinstance(req, dict):
            raise SystemExit("--dispatch-stdin: expected a JSON object")
        store = load(args.path)
        op = req.get("op")
        if op == "read":
            print(json.dumps(store))
            return 0
        if op == "write":
            apply_inputs(store, secrets_in=req.get("secrets"),
                         config_in=req.get("config"), overwrite=True)
            dump(store, args.path)
            print(json.dumps({"ok": True}))
            return 0
        raise SystemExit(f"--dispatch-stdin: unknown op {op!r}")

    store = load(args.path)
    adopt_raw = ""
    if args.adopt_stdin:
        adopt_raw = sys.stdin.read().strip()
    elif args.adopt_file:
        adopt_raw = Path(args.adopt_file).read_text().strip()
    if adopt_raw:
        incoming = json.loads(adopt_raw)
        if not isinstance(incoming, dict):
            raise SystemExit("--adopt-*: expected a JSON object")
        adopt(store, incoming)
    apply_inputs(
        store,
        secrets_in=_parse_kv(args.set_secret),
        config_in=_parse_kv(args.set_config),
        overwrite=args.overwrite,
    )
    if not args.no_mint:
        ensure_internal_secrets(store)
        ensure_user_held_secrets(store)
    dump(store, args.path)

    if args.emit == "secrets":
        print(json.dumps(store.get("secrets", {})))
    elif args.emit == "all":
        print(json.dumps(store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
