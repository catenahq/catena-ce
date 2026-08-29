#!/usr/bin/env python3
"""On-box config store for Catena (0b client-owned config).

Single plaintext source of truth at ``/etc/catena/config.json`` (0600 root).
Three top-level sections belong to this helper:

    {
      "secrets": {            # external + internal-minted + role-minted
        "admin_password": "...",
        "catena_postgres_password": "...",
        ...
      },
      "config": {             # non-secret
        "CLOUDFLARE_ZONE": "...",
        ...
      },
      "client_app_secrets": { # per-deploy credentials of the client's apps
        "nextcloud-s3-oidc/DB_PASSWORD": "...",
        ...
      }
    }

Other blocks in the same file belong to other writers (`schedules` and
`backup_retention` to catena-schedule, `image_pins` to the managed-update
lane); this helper preserves them untouched.

The converge loads this store, mints any MISSING *internal* secret
(reconcile-not-overwrite -- an existing value is never touched), writes the
store back 0600, and emits the merged secret view as JSON for an Ansible
``set_fact``. The box is the source of truth: nothing secret is kept on the
laptop, and ``/etc`` is in ``roles/backup`` ``backup_paths`` so the store
rides every restic snapshot -- a restore returns every secret with the data.

Design constraints:
  - stdlib only. Runs on a minimal target host whose system python has no
    PyYAML. JSON is stdlib and round-trips base64 / url-safe secret values
    exactly.
  - INTERNAL secrets are minted here; EXTERNAL secrets (vendor creds the
    client supplies) are only ever *stored*, never generated -- they arrive
    via the two-phase bootstrap or the catena-admin settings API.
  - ROLE_MINTED secrets are minted by the service itself and captured by the
    role that provisioned it. This module neither generates nor accepts them;
    it only names them, so "belongs to no category" is not a valid state.
  - USER_HELD secrets (the admin, restic + console-recovery passwords) are
    minted on-box IF ABSENT (like internal), but are the DR / break-glass /
    first-login keyset the user must hold
    a copy of: the installer reads them back and shows them ONCE for the
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
import string
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


# --- client-app env secrets (the marketplace's per-deploy values) -----------
#
# A catalog template declares some of its env defaults as
# ``{{ lookup('password', '/dev/null length=N chars=...') }}``. Those are the
# app's OWN credentials -- its database password, its admin password, its
# session key -- and they are per-deploy, not per-product: nothing outside the
# app reads them, and two clients must not share one.
#
# They are minted HERE, on the box, for the same reason every other secret is:
# the box is the source of truth, /etc rides the restic snapshot, and a restore
# returns the value with the data. The panel asks for them by name and gets
# back what is stored, minting only what is missing -- so asking twice returns
# the same value and a re-render of the catalog cannot re-key a running app.
#
# They live in their OWN top-level block rather than under ``secrets`` because
# they are not product secrets: nothing in the converge reads them, they are
# not classified in any registry here, and the count of them grows with what
# the client deploys rather than with what the product ships.
CLIENT_APP_SECRETS_KEY = "client_app_secrets"

# Alphabets the catalog actually asks for. ``chars=hexdigits`` is always piped
# through ``| lower`` in the catalog, so the lowercase alphabet IS the rendered
# one -- minting from it directly is the same character set, drawn uniformly.
APP_SECRET_CHARSETS: dict[str, str] = {
    "alnum": string.ascii_letters + string.digits,
    "hex": string.digits + "abcdef",
}

# Bounds on a requested length. The floor is a strength floor; the ceiling
# stops a malformed request from writing an unbounded value into the store.
APP_SECRET_MIN_LENGTH = 16
APP_SECRET_MAX_LENGTH = 256

# ``<template-id>/<ENV_KEY>``. Constrained because this is the one write the
# admin CONTAINER can ask for by name: the pattern keeps a forged request
# inside this block, where the worst it can do is add an entry no app reads.
_APP_SECRET_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}/[A-Z][A-Z0-9_]{0,63}$")


def app_secret_key(template_id: str, env_key: str) -> str:
    """The store key one template's env value is remembered under."""
    return f"{template_id}/{env_key}"


def mint_app_secret(length: int, charset: str) -> str:
    """Mint one client-app env secret of `length` chars from `charset`."""
    alphabet = APP_SECRET_CHARSETS[charset]
    return "".join(_secrets.choice(alphabet) for _ in range(length))


def client_app_secrets(path: str | Path = DEFAULT_STORE_PATH) -> dict:
    """What has been minted for client apps, as {key: value}.

    Read-only and total, the same posture as ``image_pins``: an absent store,
    an absent key and a key holding something else all read as "nothing minted
    yet", which on a host that has deployed no app is the truth. A malformed
    store is still a hard error."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level is not an object")
    minted = raw.get(CLIENT_APP_SECRETS_KEY)
    if not isinstance(minted, dict):
        return {}
    return {str(k): str(v) for k, v in minted.items() if v}


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
    return f"oauth2_proxy_cookie_secret_{zone_slug(zone)}"


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
    "catena_postgres_password": mint_strong_password,
    "turn_static_auth_secret": mint_strong_password,
    # Signs the catena-admin native-login session cookie (the host-published
    # tailnet listener). Minted once, stable across converges, rides the backup.
    "catena_admin_session_key": mint_strong_password,
    # SSO service credentials.
    "keycloak_db_password": mint_strong_password,
    "oauth2_proxy_cookie_secret": mint_oauth2_proxy_cookie_secret,
    "oauth2_proxy_client_secret": mint_strong_password,
    "dashboard_sync_client_secret": mint_strong_password,
    # The admin panel's own realm service account: users, groups and
    # memberships. Separate from dashboard_sync_client_secret because the
    # two hold DIFFERENT realm-management roles -- one manages clients, the
    # other manages people -- and sharing a secret would collapse that.
    "catena_admin_panel_client_secret": mint_strong_password,
    "nextcloud_oidc_client_secret": mint_strong_password,
    "element_oidc_client_secret": mint_strong_password,
    "mailserver_oidc_client_secret": mint_strong_password,
    "mailserver_introspect_client_secret": mint_strong_password,
    # Healthchecks (self-hosted heartbeat instance).
    "healthchecks_secret_key": mint_strong_password,
    "healthchecks_superuser_password": mint_strong_password,
    "healthchecks_ping_key": mint_url_safe,
    "healthchecks_api_key_readonly": mint_hc_api_key,
    "healthchecks_api_key_readwrite": mint_hc_api_key,
    # Nextcloud Talk + HPB bearer secrets.
    "nextcloud_talk_signaling_secret": mint_strong_password,
    "nextcloud_talk_internal_secret": mint_strong_password,
    # Rocket.Chat-bundled Jitsi component secrets.
    "jitsi_prosody_password": mint_strong_password,
    "jitsi_jicofo_auth_password": mint_strong_password,
    "jitsi_jicofo_component_secret": mint_strong_password,
    "jitsi_jvb_auth_password": mint_strong_password,
    # Element-bundled Jitsi + jigasi secrets.
    "element_jitsi_jicofo_auth_password": mint_strong_password,
    "element_jitsi_jicofo_component_secret": mint_strong_password,
    "element_jitsi_jvb_auth_password": mint_strong_password,
    "element_jigasi_xmpp_password": mint_strong_password,
    # Beszel resource-monitor token. Beszel is always deployed, so this is
    # always in use. The hub LOGIN is deliberately NOT minted here: it is the
    # shared admin_password (USER_HELD, surfaced once at install). A login
    # minted in this table would be one the operator is never shown, for a hub
    # the panel links to as a tab.
    "beszel_universal_token": mint_url_safe,
    # Beszel's OIDC client secret, so the hub can offer "Sign in with Catena"
    # against Keycloak instead of a second password prompt behind the
    # oauth2-proxy the client has already passed. Password login stays ON
    # (DISABLE_PASSWORD_AUTH is deliberately never set): Beszel is base-plane
    # infrastructure, and a monitoring tool that can only be reached through
    # the SSO tool cannot be used to diagnose the SSO tool.
    "beszel_oidc_client_secret": mint_strong_password,
    # The auth header on the ZAP daemon's REST API while a pen-test scan is
    # running. Minted regardless of bench mode so a one-off scan against any
    # inventory needs no extra setup.
    "zap_api_key": mint_url_safe,
}

# USER_HELD: the DR keyset + first-login credential. Minted on-box IF ABSENT
# (same reconcile-not-overwrite as INTERNAL), but the installer shows them
# ONCE so the user keeps an off-box copy in their password manager. NOT in
# EXTERNAL_SECRETS, so the config-write API (settings save) cannot set them:
#   - admin_password    -- first-login credential (Portainer + Keycloak).
#   - backup_restic_password -- encrypts the backup repo. Minting it
#     on-box would trap it inside the very snapshot it decrypts IF the user
#     lost their copy -- so it is surfaced once at install for the password
#     manager, and `catena recover` ADOPTS the user's saved value into the
#     store BEFORE the restore runs (adopt is fill-only, so the freshly-minted
#     value is only used on a first install, never a recover). A rotation is a
#     deliberate `restic key passwd` action in catena-admin, not a store write.
#   - console_recovery_password -- the ops account's break-glass password
#     for the provider KVM / serial console (roles/common sets it; key-only SSH
#     keeps it console-only). Same shape as the restic password: a credential
#     whose whole purpose is the case where the normal path is gone, so a copy
#     that lives only inside the box is no copy at all.
USER_HELD_SECRETS: dict[str, Callable[[], str]] = {
    "admin_password": mint_admin_password,
    "backup_restic_password": mint_strong_password,
    # url-safe, not base64: this one gets TYPED at a serial console.
    "console_recovery_password": mint_admin_password,
}

# EXTERNAL: vendor credentials the client HOLDS (never on-box-minted). Stored,
# never minted here -- they arrive via the transient bootstrap adopt file or
# the catena-admin settings API. The optional ones may legitimately be empty.
#
# This set is also the ALLOWLIST apply_inputs enforces, so a credential a role
# tells the client to "enter in catena-admin > Settings" and that is NOT listed
# here has a documented path that raises. Keep it a superset of the settings
# schema (catena-admin shell/settings/settings.go Fields).
#
# The offsite-copy credentials are NOT here and are not flat keys at all. Six
# of them were, describing exactly two hardcoded copies; they are now two per
# declared copy inside the store's ``offsite_copies`` list, which catena-admin
# owns end to end. Nothing in this repo reads them, so nothing here has to
# name them.
EXTERNAL_SECRETS: frozenset[str] = frozenset({
    "tailscale_oauth_client_id",
    "tailscale_oauth_client_secret",
    # Self-hosted Headscale control server (alternative to Tailscale SaaS).
    # api_key mints a short-lived pre-auth key per converge (preferred); the
    # static preauth_key is the fallback. Optional -- empty on Tailscale hosts.
    "headscale_api_key",
    "headscale_preauth_key",
    "cloudflare_api_token",
    # Multi-domain (EE): JSON map zone -> API token. Each token is one-zone
    # scoped; catena-admin verifies the single-zone grant before storing. The
    # scalar cloudflare_api_token stays for the CE single-domain path.
    "cloudflare_api_tokens",
    "backup_s3_access_key",
    "backup_s3_secret_key",
    "smtp_password",
    "mailserver_relay_password",
    "mailserver_spamhaus_dqs_key",
    # CIFS credentials for the optional bulk mount (roles/storage bulk.yml,
    # storage_bulk_type=cifs). Client-held: the share is the client's NAS.
    # NFS authenticates by source IP and supplies neither.
    "storage_bulk_username",
    "storage_bulk_password",
    # Business licence token. Client-held like any other external credential:
    # the client is given it on purchase and pastes it into catena-admin >
    # Settings, and the panel plus the host engines read it back from here. It
    # is a signed claim rather than a shared secret, so this repo neither mints
    # nor verifies it -- it only stores it.
    "catena_license",
    # Stripe live + webhook keys for the optional client portal. Never minted:
    # the operator pastes them from the Stripe dashboard. Billing routes
    # degrade gracefully while they are blank.
    "portal_stripe_secret_key",
    "portal_stripe_webhook_secret",
})

# ROLE_MINTED: minted by the SERVICE, captured by the role that provisioned it.
# Neither internal-minted (this module never generates them) nor client-supplied
# (no human ever sees one), so they belong to no set above -- but "belongs to no
# set" has to be a category with members, not a sentence in a comment, or the
# provenance gate has nothing to check them against and the next one to appear
# is invisible.
#
# Value is the role that writes it into the store, so a failure names the owner.
#   - portainer_api_key -- Portainer's own token API mints it
#     (helpers/bootstrap_portainer_admin.py); roles/portainer adopts it, with
#     --overwrite, because a /data restore invalidates the stored one.
#
# NOT minted here and NOT adoptable through apply_inputs: a role-minted secret
# has exactly one writer, and that writer is the role.
ROLE_MINTED_SECRETS: dict[str, str] = {
    "portainer_api_key": "portainer",
}


# --- non-secret config: who owns which key (SECRETS.md category 4) ----------
#
# Two owners, one boundary, and the boundary is the two-phase install.
#
# BOOTSTRAP keys are what the installer needs before there is a box to ask:
# the zone, the subdomains, the ops user, the storage layout. They come from
# the inventory `.env` and stay there.
#
# SETTINGS keys are everything the client changes AFTER the install, in
# catena-admin. Those belong to the on-box store, and the `.env` is only their
# first-install SEED -- adopted fill-only on the first converge, exactly like
# an external secret, and never read again.
#
# Before this split both were live read paths at once. run-backup.sh had to
# reconcile them in shell at runtime, retention drifted into two copies with
# the converge's silently dead (see the note in backup.env.j2), and a
# tag-scoped converge that skipped the loader fell back to a stale `.env`
# value with no signal -- which is how a poisoned postgres password survived
# `--tags postgres` (fi_s3, site.yml:45-51).
#
# Value is the Ansible variable the loader publishes the stored value as. The
# projection is DECLARED rather than derived by lowercasing: two keys already
# do not follow the rule (BACKUP_RESTIC_REPO -> backup_restic_repo is fine,
# NTFY_SERVER -> ntfy_server is fine, but MAILSERVER_CERTBOT_STAGING ->
# mailserver_certbot_staging and CATENA_ACME_* -> coturn_acme_* are not), and
# a derived mapping fails silently by publishing a fact nothing reads.
SETTINGS_CONFIG: dict[str, str] = {
    # Backup: repo + cadence + the alert lanes.
    "BACKUP_RESTIC_REPO": "cfg_backup_restic_repo",
    "BACKUP_WEEKLY_TIMER_ONCALENDAR": "cfg_backup_weekly_timer_oncalendar",
    "BACKUP_HEALTHCHECK_URL": "cfg_backup_healthcheck_url",
    "BACKUP_HEALTHCHECK_ATTEMPTED_URL": "cfg_backup_healthcheck_attempted_url",
    "BACKUP_HEALTHCHECK_URL_CLIENT": "cfg_backup_healthcheck_url_client",
    "BACKUP_HEALTHCHECK_URL_OPERATOR": "cfg_backup_healthcheck_url_operator",
    # Offsite copy lane. WHICH buckets it copies is not here: that is the
    # store's ``offsite_copies`` list, read straight off disk by the lane so
    # adding a copy takes effect with no converge in between. These two are
    # only the lane's dead-man endpoints, and both default to the on-box
    # Healthchecks -- an operator overrides them to point somewhere else.
    "OFFSITE_HEALTHCHECK_URL": "cfg_offsite_healthcheck_url",
    "OFFSITE_HEALTHCHECK_ATTEMPTED_URL": "cfg_offsite_healthcheck_attempted_url",
    # Outbound mail. The password is an EXTERNAL_SECRET; these are its
    # non-secret companions.
    #
    # SMTP_PROVIDER is the whole routing decision: resend | brevo | server.
    # It used to be inferred from WHICH of three sender-address keys was
    # non-empty, with Resend silently winning when two were filled -- so the
    # answer to "where does mail go" was spread across three fields and a
    # precedence rule, and a client who switched providers without clearing the
    # old address kept sending through the old one.
    #
    # SMTP_HOST / SMTP_PORT / SMTP_USER apply to the `server` choice; SMTP_USER
    # also carries the Brevo login, which is account-specific. Resend needs
    # neither: its host and its literal `resend` username are constants.
    "SMTP_PROVIDER": "cfg_smtp_provider",
    "SMTP_SENDER": "cfg_smtp_sender",
    "SMTP_HOST": "cfg_smtp_host",
    "SMTP_PORT": "cfg_smtp_port",
    "SMTP_USER": "cfg_smtp_user",
    # Alert delivery. Both blank by default; see roles/infrastructure.
    "NTFY_SERVER": "cfg_ntfy_server",
    "NTFY_TOPIC": "cfg_ntfy_topic",
    # Egress proxies / mirrors -- a site policy, not an install input.
    "APT_PROXY_URL": "cfg_apt_proxy_url",
    "DOCKER_REGISTRY_MIRROR_URL": "cfg_docker_registry_mirror_url",
    # Mailserver toggles.
    "MAILSERVER_CERTBOT_STAGING": "cfg_mailserver_certbot_staging",
}

# Read from the inventory `.env` at converge time, by design: the installer
# needs them before the box exists. Declared so a key that is in NEITHER set
# is a gate failure rather than an unnoticed third owner.
BOOTSTRAP_CONFIG: frozenset[str] = frozenset({
    "ADMIN_EMAIL",
    "AUTH_SUBDOMAIN",
    "BESZEL_SUBDOMAIN",
    "CATENA_ADMIN_SUBDOMAIN",
    "CATENA_ADMIN_UI_PORT",
    "CLOUDFLARED_TUNNEL_NAME_PREFIX",
    "CLOUDFLARE_ZONE",
    "COMMON_LOCALE",
    "COMMON_TIMEZONE",
    "DASH_SUBDOMAIN",
    "DISPLAY_NAME",
    "GATUS_SUBDOMAIN",
    "HEADSCALE_USER",
    "HEARTBEAT_SUBDOMAIN",
    "OPS_USER",
    "PORTAINER_SUBDOMAIN",
    "PORTAINER_UI_PORT",
    "SSH_PRIVATE_KEY",
    "SSH_PUBLIC_KEY_FILE",
    "STORAGE_BLOCK_DEVICE",
    "STORAGE_BULK_ENABLED",
    "STORAGE_BULK_MOUNT_POINT",
    "STORAGE_MODE",
    "STORAGE_MOUNT_POINT",
    "TAILNET_CONTROL_URL",
    "TAILSCALE_ACCEPT_DNS",
    "TAILSCALE_TAGS",
    # Bench / dev overrides for the ACME path. Not client-facing: they point
    # coturn's cert issuance at a local Pebble instead of Let's Encrypt, which
    # is an inventory-level decision made before the host exists.
    "CATENA_ACME_CA_BUNDLE_PEM_B64",
    "CATENA_ACME_DIRECTORY_URL",
    "CATENA_ACME_HOST_IP",
    "COTURN_CERTBOT_STAGING",
})


def config_names() -> list[str]:
    """Every non-secret config key with a declared owner."""
    return sorted(set(SETTINGS_CONFIG) | BOOTSTRAP_CONFIG)


def settings_config_vars(config: dict) -> dict:
    """Project the store's config section onto the Ansible variable names the
    converge reads. Unknown keys are ignored: the settings API accepts forward
    keys a given catena-ce may not know yet, and a converge must not invent a
    fact from one."""
    out: dict = {}
    for key, var in SETTINGS_CONFIG.items():
        if key in (config or {}):
            out[var] = config[key]
    return out


def secret_names() -> list[str]:
    """Every key the store recognises, across all four categories.

    This is the converge loader's discriminator: it selects which in-scope
    Ansible variables get captured into the store. Naming secrets here, by
    declared name rather than by a spelling convention, means the four
    category tables above are the single declaration, and a key that belongs
    to no category is already impossible by construction.

    The loader must still capture BY NAME and never evaluate the whole
    variable set: resolving every in-scope var aborts a converge on role
    defaults that only resolve inside their own role.
    """
    return sorted(
        set(INTERNAL_SECRETS) | set(USER_HELD_SECRETS)
        | set(EXTERNAL_SECRETS) | set(ROLE_MINTED_SECRETS)
    )


# --- store I/O --------------------------------------------------------------
def load(path: str | Path = DEFAULT_STORE_PATH) -> dict:
    """Load the store. Returns a dict with 'secrets' and 'config' sub-dicts
    (empty when the file is absent). A malformed file is a hard error -- we
    must never silently start minting fresh secrets over a corrupt store."""
    p = Path(path)
    if not p.exists():
        return {"secrets": {}, "config": {}, CLIENT_APP_SECRETS_KEY: {}}
    raw = json.loads(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level is not an object")
    return {
        "secrets": dict(raw.get("secrets") or {}),
        "config": dict(raw.get("config") or {}),
        CLIENT_APP_SECRETS_KEY: dict(raw.get(CLIENT_APP_SECRETS_KEY) or {}),
    }


def image_pins(path: str | Path = DEFAULT_STORE_PATH) -> dict:
    """What the on-host update lane last applied, as {repository: image_ref}.

    Read-only and total: an absent store, an absent key and a key holding
    something other than an object all read as "nothing pinned", because on
    this path the converge falls back to its own floor and a fresh host must
    converge rather than fail. A malformed store is still a hard error -- the
    same rule load() follows, for the same reason."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level is not an object")
    pins = raw.get("image_pins")
    if not isinstance(pins, dict):
        return {}
    return {str(k): str(v) for k, v in pins.items() if v}


def dump(store: dict, path: str | Path = DEFAULT_STORE_PATH) -> None:
    """Atomically write the store 0600 root. Write to a temp sibling then
    rename so a crash mid-write can't leave a half-written store.

    This helper owns `secrets`, `config` and `client_app_secrets`, and MERGES
    them into whatever else the file holds. It is not the only writer:
    catena-schedule owns `schedules` and `backup_retention`, and the
    managed-update lane owns `image_pins`. Serialising just the keys this
    helper knows about deleted the others on every converge -- and `image_pins`
    exists precisely so the converge can ask what the on-host lane applied, so a
    converge that wiped it on the way past would answer its own question with
    nothing, every time.

    `client_app_secrets` is written only when the caller's store CARRIES it.
    `load()` always supplies it, so a load-modify-dump round trip preserves it;
    a caller that builds a store dict by hand (the converge loader passes
    `{"secrets": ..., "config": ...}`) has no opinion about the block and must
    not blank it -- which is the same trap the paragraph above describes."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc: dict = {}
    if p.exists():
        raw = json.loads(p.read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"{p}: top level is not an object")
        doc = raw
    doc["secrets"] = store.get("secrets", {})
    doc["config"] = store.get("config", {})
    if CLIENT_APP_SECRETS_KEY in store:
        doc[CLIENT_APP_SECRETS_KEY] = store[CLIENT_APP_SECRETS_KEY]
    payload = json.dumps(doc, indent=2, sort_keys=True)
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
    # and mint nothing extra (the base oauth2_proxy_cookie_secret stands).
    zones = store.get("config", {}).get("CLOUDFLARE_ZONES")
    for zone in configured_zone_names(zones):
        key = zone_cookie_secret_key(zone)
        cur = secrets_map.get(key)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            secrets_map[key] = mint_oauth2_proxy_cookie_secret()
            minted.append(key)
    return minted


def ensure_app_secrets(store: dict, wanted: object) -> dict:
    """Resolve the client-app env secrets in `wanted`, minting what is absent.

    `wanted` is ``{"<template-id>/<ENV_KEY>": {"length": N, "charset": name}}``
    -- the shape the marketplace renderer reads off the catalog's
    ``lookup('password', ...)`` expressions. Returns ``{key: value}`` for
    EVERY requested key, so the caller renders from the return value and never
    has to decide whether a value was new.

    Reconcile-not-overwrite, and that is the load-bearing property: the catalog
    is re-rendered on every marketplace fetch, and a mint on each one would hand
    the client's Portainer a different database password every time they opened
    the page -- while the app that was already deployed kept the first.

    Every request is validated before anything is written. This is the one
    store write the unprivileged admin container can ask for by name, so a
    forged request must not be able to name a key outside this block or store a
    value of its own choosing: the key shape is fixed, the length is bounded,
    the charset is a closed set, and the VALUE is always minted here -- the
    request never carries one."""
    if wanted is None:
        return {}
    if not isinstance(wanted, dict):
        raise ValueError("app secrets request: expected an object")
    minted_map = store.setdefault(CLIENT_APP_SECRETS_KEY, {})
    out: dict = {}
    for key, spec in wanted.items():
        if not isinstance(key, str) or not _APP_SECRET_KEY_RE.match(key):
            raise ValueError(f"app secrets request: bad key {key!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"app secrets request: {key} spec is not an object")
        charset = spec.get("charset", "alnum")
        if charset not in APP_SECRET_CHARSETS:
            raise ValueError(
                f"app secrets request: {key} charset {charset!r} is not one of "
                f"{sorted(APP_SECRET_CHARSETS)}"
            )
        length = spec.get("length")
        if not isinstance(length, int) or isinstance(length, bool):
            raise ValueError(f"app secrets request: {key} length is not an integer")
        if not APP_SECRET_MIN_LENGTH <= length <= APP_SECRET_MAX_LENGTH:
            raise ValueError(
                f"app secrets request: {key} length {length} outside "
                f"{APP_SECRET_MIN_LENGTH}..{APP_SECRET_MAX_LENGTH}"
            )
        cur = minted_map.get(key)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            minted_map[key] = mint_app_secret(length, charset)
        out[key] = minted_map[key]
    return out


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


def adopt(store: dict, mapping: dict | None, *, overwrite: bool = False) -> list[str]:
    """Capture secret values into the store. Fill-only by default: the
    converge loader passes every declared secret in scope, and a value already
    in the store is the source of truth. Never stores a blank. Accepts ANY key
    -- the caller pre-filters against secret_names() -- so the
    ROLE_MINTED_SECRETS are captured too, which is why this is separate from
    apply_inputs and its EXTERNAL_SECRETS allowlist.

    ``overwrite=True`` replaces an existing value. Used by roles/portainer
    when Portainer REJECTS the stored API key (the admin was recreated, or a
    /data restore replaced the BoltDB the token lived in): the freshly minted
    key has to win, and fill-only would keep serving the dead one.

    Returns the keys written."""
    secrets_map = store.setdefault("secrets", {})
    adopted: list[str] = []
    for key, val in (mapping or {}).items():
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        if not overwrite and secrets_map.get(key):
            continue
        if secrets_map.get(key) == val:
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
                    help="replace existing values instead of filling only "
                         "blanks (applies to --adopt-* as well as --set-*)")
    ap.add_argument("--no-mint", action="store_true",
                    help="do not mint missing internal secrets (seed-only)")
    ap.add_argument("--adopt-stdin", action="store_true",
                    help="read a JSON object of {key: value} secrets from "
                         "stdin and adopt them before minting (fill-only "
                         "unless --overwrite)")
    ap.add_argument("--adopt-file", metavar="PATH",
                    help="like --adopt-stdin but read the JSON object from a "
                         "file (the Ansible loader stages the capture map to a "
                         "0600 host file; this ansible-core's script module has "
                         "no stdin passthrough)")
    ap.add_argument("--emit",
                    choices=["secrets", "all", "none", "secret-names",
                             "settings-config-names", "config-vars",
                             "image-pins"],
                    default="secrets",
                    help="what to print as JSON on stdout (default: secrets, "
                         "for an Ansible set_fact of the store's keys). "
                         "secret-names prints the declared key list WITHOUT "
                         "touching the store -- the converge loader reads it "
                         "to know which in-scope variables to capture. "
                         "settings-config-names prints the store-owned config "
                         "keys, also without touching the store, so the loader "
                         "knows which .env values to seed. config-vars prints "
                         "the store's config projected onto Ansible variable "
                         "names, for a set_fact that outranks the role "
                         "defaults. image-pins prints what the on-host update "
                         "lane last applied, as {repository: image_ref}, also "
                         "without touching the store.")
    ap.add_argument("--dispatch-stdin", action="store_true",
                    help="serve the catena-admin settings API: read a JSON "
                         "request {op: read|write|mint-app-secrets, secrets, "
                         "config, app_secrets} from stdin. read -> print the "
                         "full store; write -> apply the external creds/config "
                         '(overwrite, no mint) and print {"ok": true}, '
                         "rejecting internal-secret keys; mint-app-secrets -> "
                         "resolve the marketplace's per-deploy client-app env "
                         "values, minting only what is absent, and print them.")
    args = ap.parse_args(argv)

    # A pure query, answered before any store I/O: the converge loader asks
    # for this BEFORE the store exists on a fresh box, and reading the answer
    # must never mint or write anything.
    if args.emit == "secret-names":
        print(json.dumps(secret_names()))
        return 0
    if args.emit == "settings-config-names":
        print(json.dumps(sorted(SETTINGS_CONFIG)))
        return 0

    # A pure READ of somebody else's key. The managed-update lane writes
    # `image_pins` with no converge; the converge reads it so it can ASK what
    # image a service should run rather than assert the shipped floor at one.
    # Reading it must not mint, must not write, and must answer on a host that
    # has no store yet -- which is every host the first time round.
    if args.emit == "image-pins":
        print(json.dumps(image_pins(args.path)))
        return 0

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
        if op == "mint-app-secrets":
            # The marketplace renderer asking for one template's per-deploy
            # values. Unlike `write` this DOES mint -- that is its whole job --
            # but only inside client_app_secrets, only values it generates
            # itself, and only for keys that are absent. See ensure_app_secrets.
            values = ensure_app_secrets(store, req.get("app_secrets"))
            dump(store, args.path)
            print(json.dumps({"ok": True, "values": values}))
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
        adopt(store, incoming, overwrite=args.overwrite)
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
    elif args.emit == "config-vars":
        print(json.dumps(settings_config_vars(store.get("config", {}))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
