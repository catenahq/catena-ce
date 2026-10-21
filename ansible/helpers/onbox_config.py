#!/usr/bin/env python3
"""On-box config store for Catena.

Single plaintext source of truth at ``/etc/catena/config.json`` (0600 root).
Two top-level sections belong to this helper:

    {
      "secrets": {            # external + internal-minted + role-minted
        "admin_password": "...",
        "catena_postgres_password": "...",
        ...
      },
      "config": {             # non-secret
        "CLOUDFLARE_ZONE": "...",
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
laptop, and ``/etc`` is in ``reconcile/roles/backup`` ``backup_paths`` so the store
rides every restic snapshot -- a restore returns every secret with the data.

Design constraints:
  - stdlib only on a host, whose system python has no PyYAML. JSON is stdlib
    and round-trips base64 / url-safe secret values exactly.
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
    settings save), and they remain ADOPTABLE: a value handed to the loader
    is kept rather than replaced by a fresh mint.
  - Format contracts for the minted values: oauth2 cookie length-after-decode,
    Healthchecks 32-char API keys, url-safe ping key, 20-char admin password,
    64-char base64 restic password.
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

# --- the knob registry ------------------------------------------------------
#
# Which vendor credentials the store accepts, which config keys it owns, and
# which the inventory keeps, are DECLARED in helpers/knobs.yml.
#
# On a host the registry is JSON, because this module runs there on a python
# with no PyYAML: the converge stages it (playbooks/tasks/stage_knob_registry.yml)
# and the payload installs it. A checkout reads knobs.yml itself, on a
# controller that has PyYAML. Resolution order:
#
#   CATENA_KNOBS              an explicit JSON path (the converge, tests)
#   CATENA_PAYLOAD_LIB        the payload's lib dir, when it is set
#   /usr/local/lib/catena     where the payload installs it
#   this script's directory   knobs.yml, in the catena-ce checkout
#
# A MISSING REGISTRY RAISES. Falling back to empty sets would leave
# apply_inputs refusing every credential the client supplies and the converge
# publishing no config facts at all -- both silent, both indistinguishable from
# a host nobody has configured. An install missing the file is broken, and this
# is the only place that can say so.
_KNOBS_FILENAME = "knobs.json"


def _knobs_path() -> Path:
    candidates = []
    explicit = os.environ.get("CATENA_KNOBS", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    payload_lib = os.environ.get("CATENA_PAYLOAD_LIB", "").strip()
    if payload_lib:
        candidates.append(Path(payload_lib) / _KNOBS_FILENAME)
    candidates.append(Path("/usr/local/lib/catena") / _KNOBS_FILENAME)
    candidates.append(Path(__file__).resolve().parent / "knobs.yml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"the knob registry is not at any of {[str(c) for c in candidates]}. "
        "The converge stages it and the payload installs it; without it this "
        "host would refuse every credential and publish no config."
    )


def _load_knobs() -> dict:
    path = _knobs_path()
    if path.suffix == ".yml":
        import yaml

        doc = yaml.safe_load(path.read_text())
    else:
        doc = json.loads(path.read_text())
    if doc.get("version") != 1:
        raise ValueError(
            f"{path.name} declares version {doc.get('version')!r}; "
            "this reader knows version 1"
        )
    return doc


_KNOBS = _load_knobs()


# --- minters ----------------------------------------------------------------
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
    accept it."""
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
# user-held DR keyset / first-login credential, generated on-box but surfaced
# once for the user's password manager (see USER_HELD_SECRETS).
INTERNAL_SECRETS: dict[str, Callable[[], str]] = {
    "catena_postgres_password": mint_strong_password,
    "turn_static_auth_secret": mint_strong_password,
    # Signs the catena-admin native-login session cookie (the host-published
    # tailnet listener). Minted once, stable across converges, rides the backup.
    "catena_admin_session_key": mint_strong_password,
    # The password the panel's sign-in gate sends with every request it
    # forwards, and the only proof the panel accepts that a request came
    # through the gate: its listener is reachable from every container on
    # catena-network, which can set any identity header it likes.
    "catena_admin_gate_secret": mint_strong_password,
    # The token the host's dashboard-sync lane sends in a header to the panel's
    # Keycloak forward on the host's loopback, and the only proof the forward
    # accepts that a call came from the lane. Minted once, stable across
    # converges, rides the backup.
    "catena_marketplace_token": mint_url_safe,
    # SSO service credentials.
    "keycloak_db_password": mint_strong_password,
    # The master realm's automation client (reconcile/roles/keycloak): every
    # realm import and kcadm session the converge makes signs in with it, so
    # the master admin's own password can carry a second factor.
    "keycloak_automation_client_secret": mint_strong_password,
    # The primary domain's secret each client app's gate derives its own cookie
    # secret from (catena-admin payload/lib/clients_provisioner.py).
    "oauth2_proxy_cookie_secret": mint_oauth2_proxy_cookie_secret,
    # One per admin tool's gate, each signing a cookie for that tool's host
    # alone (reconcile/roles/oauth2_proxy oauth2_proxy_apps cookie_secret_var),
    # so a session cookie a client app or another tool sees opens none of them.
    "catena_admin_gate_cookie_secret": mint_oauth2_proxy_cookie_secret,
    "healthchecks_gate_cookie_secret": mint_oauth2_proxy_cookie_secret,
    "gatus_gate_cookie_secret": mint_oauth2_proxy_cookie_secret,
    "beszel_gate_cookie_secret": mint_oauth2_proxy_cookie_secret,
    "portainer_gate_cookie_secret": mint_oauth2_proxy_cookie_secret,
    "oauth2_proxy_client_secret": mint_strong_password,
    "dashboard_sync_client_secret": mint_strong_password,
    # The admin panel's own realm service account: users, groups and
    # memberships. Separate from dashboard_sync_client_secret because the
    # two hold DIFFERENT realm-management roles -- one manages clients, the
    # other manages people -- and sharing a secret would collapse that.
    "catena_admin_panel_client_secret": mint_strong_password,
    # The identity-posture probe's realm service account. A THIRD credential
    # rather than a reuse of either above: the probe needs view-realm, which
    # the panel is deliberately denied, and must not hold manage-users, which
    # the panel has. A supervision credential that could change what it
    # supervises could hide a drift by correcting it.
    "catena_identity_probe_client_secret": mint_strong_password,
    "mailserver_oidc_client_secret": mint_strong_password,
    "mailserver_introspect_client_secret": mint_strong_password,
    # Healthchecks (self-hosted heartbeat instance).
    "healthchecks_secret_key": mint_strong_password,
    "healthchecks_superuser_password": mint_strong_password,
    "healthchecks_ping_key": mint_url_safe,
    "healthchecks_api_key_readonly": mint_hc_api_key,
    "healthchecks_api_key_readwrite": mint_hc_api_key,
    # Beszel resource-monitor token. Beszel is always deployed, so this is
    # always in use. The hub LOGIN is deliberately NOT minted here: it is the
    # shared admin_password (USER_HELD, surfaced once at install). A login
    # minted in this table would be one the operator is never shown, for a hub
    # the panel links to as a tab.
    "beszel_universal_token": mint_url_safe,
    # Beszel's OIDC client secret: "Sign in with Catena" on the hub
    # (scripts/beszel-seed.py).
    "beszel_oidc_client_secret": mint_strong_password,
    # The auth header on the ZAP daemon's REST API while a pen-test scan is
    # running. Minted regardless of bench mode so a one-off scan against any
    # inventory needs no extra setup.
    "zap_api_key": mint_url_safe,
}

# USER_HELD: the DR keyset + first-login credential, generated on-box and shown
# ONCE so the user keeps an off-box copy in their password manager. NOT in
# EXTERNAL_SECRETS, so the config-write API (settings save) cannot set them:
#   - admin_password    -- first-login credential (Portainer + Keycloak).
#     Minted right after bootstrap by playbooks/show-keyset.yml, which shows
#     it then, or by any converge that finds it absent.
#   - console_recovery_password -- the ops account's break-glass password
#     for the provider KVM / serial console (bootstrap/roles/common sets it;
#     key-only SSH keeps it console-only). Minted and shown like the admin
#     password: a credential whose whole purpose is the case where the normal
#     path is gone, so a copy that lives only inside the box is no copy at all.
#   - backup_restic_password -- encrypts the backup repo. Never minted by a
#     converge (MINTED_ON_REQUEST): the client generates it in catena-admin >
#     Settings > Backup, beside the repository it encrypts, and saves it then
#     (scripts/catena-restic-key.py `generate`). A whole-server restore brings
#     back the password that opened the repository. A rotation is a deliberate
#     `restic key passwd` action in catena-admin, not a store write.
USER_HELD_SECRETS: dict[str, Callable[[], str]] = {
    "admin_password": mint_admin_password,
    "backup_restic_password": mint_strong_password,
    # url-safe, not base64: this one gets TYPED at a serial console.
    "console_recovery_password": mint_admin_password,
}

# The user-held secrets a converge leaves alone: each is generated when the
# client asks for it, where the client is looking when it is shown.
MINTED_ON_REQUEST: frozenset[str] = frozenset({"backup_restic_password"})

# EXTERNAL: vendor credentials the client HOLDS (never on-box-minted). Stored,
# never minted here -- they arrive via the catena-admin settings API. The
# optional ones may legitimately be empty.
#
# This set is also the ALLOWLIST apply_inputs enforces, so a credential a role
# tells the client to "enter in catena-admin > Settings" and that is NOT
# declared in the registry has a documented path that raises. The panel's
# schema is held against the same declaration by its own test, so the two
# cannot disagree about which credentials exist.
#
# The offsite-copy credentials are not flat keys at all: they are two per
# declared copy inside the store's ``offsite_copies`` list, which catena-admin
# owns end to end. Nothing in this repo reads them, so nothing here names them.
EXTERNAL_SECRETS: frozenset[str] = frozenset(
    entry["key"] for entry in _KNOBS["secrets"]
)

# ROLE_MINTED: minted by the SERVICE, captured by the role that provisioned it.
# Neither internal-minted (this module never generates them) nor client-supplied
# (no human ever sees one), so they belong to no set above -- but "belongs to no
# set" has to be a category with members, not a sentence in a comment, or the
# provenance gate has nothing to check them against and the next one to appear
# is invisible.
#
# Value is the role that writes it into the store, so a failure names the owner.
#   - portainer_api_key -- Portainer's own token API mints it
#     (helpers/bootstrap_portainer_admin.py); reconcile/roles/portainer adopts it, with
#     --overwrite, because a /data restore invalidates the stored one.
#
# NOT minted here and NOT adoptable through apply_inputs: a role-minted secret
# has exactly one writer, and that writer is the role.
ROLE_MINTED_SECRETS: dict[str, str] = {
    "portainer_api_key": "portainer",
}

# PANEL: the secrets the settings API's `read` hands catena-admin, beside the
# whole non-secret `config` (panel_view). The panel is the most exposed
# component on the host, and a credential it never reads is one a compromised
# panel cannot take: the master realm's automation client, Keycloak's database
# password, the console break-glass password and Portainer's API key stay on
# the host. A settings write merges into the whole file (dump), so a key
# outside this set survives every write the panel makes.
#   - each client-supplied secret the Settings page offers (a knob with a
#     `panel`): the page reports which are set, and the tailnet and backup
#     checks send the stored ones;
#   - backup_restic_password: the disaster-recovery keyset the page reveals;
#   - admin_password: the panel's native sign-in and the Beszel hub's superuser
#     sign-in.
PANEL_SECRETS: frozenset[str] = frozenset(
    entry["key"] for entry in _KNOBS["secrets"] if "panel" in entry
) | {"backup_restic_password", "admin_password"}

# The repositories a client application may be added from, and their tokens.
# Both are top-level store blocks owned by catena-admin's catena-apps, like
# ``offsite_copies``: a settings write merges past them (dump) and the panel's
# read never carries them, so a compromised panel reads no token. The panel
# lists and edits the repositories through the host's apps-repositories action.
#   - app_repositories: [{"url", "trusted"}]; absent means Catena's catalog
#     alone, trusted.
#   - app_repository_tokens: {url: token} for private repositories, root only.
APP_REPOSITORIES_KEY = "app_repositories"
APP_REPOSITORY_TOKENS_KEY = "app_repository_tokens"


# --- non-secret config: who owns which key (SECRETS.md category 4) ----------
#
# Two owners, one boundary, and the boundary is the two-phase install.
#
# BOOTSTRAP keys are what the installer needs before there is a box to ask:
# the ops user, the SSH key, the storage layout. They come from the inventory
# `.env` and stay there.
#
# SETTINGS keys live in the on-box store and are changed in catena-admin. A
# few of them (ENV_SEEDED_CONFIG) are needed by the first converge, before the
# panel exists: the `.env` is their first-install SEED, adopted fill-only and
# never read again, except the few it keeps editing (ENV_RESEEDED_CONFIG). The
# rest have no `.env` line at all.
#
# Value is the Ansible variable the loader publishes the stored value as: the
# registry's `var`.
#
# A stored knob with NO declared variable is deliberately absent from this map:
# a runtime lane reads it straight off the store with no converge in between,
# so there is no fact to publish.
SETTINGS_CONFIG: dict[str, str] = {
    entry["key"]: entry["var"]
    for entry in _KNOBS["config"]
    if entry["residence"] == "store" and "var" in entry
}

# The store keys the inventory `.env` seeds on the first converge. Every other
# store key is written by catena-admin only.
ENV_SEEDED_CONFIG: frozenset[str] = frozenset(
    entry["key"] for entry in _KNOBS["config"]
    if entry["residence"] == "store" and "env" in entry
)

# The seeded keys the `.env` keeps editing: every converge run from the
# inventory writes their non-blank `.env` value over the stored one. Each has
# no panel field, so the `.env` is the one place a change to it is made.
ENV_RESEEDED_CONFIG: frozenset[str] = frozenset(
    entry["key"] for entry in _KNOBS["config"]
    if entry["key"] in ENV_SEEDED_CONFIG and entry["env"].get("reseed")
)

# Read from the inventory `.env` at converge time, by design: the installer
# needs them before the box exists.
#
# A config knob belongs to exactly one residence, which the registry states and
# its renderer enforces, so a key owned by neither of these two is not a state
# this module has to guard against. The third residence is `host` -- the target
# identity seed writes into hosts.yml -- and it is owned by neither of the maps
# here for the same reason the `.env` does not own a stored value.
BOOTSTRAP_CONFIG: frozenset[str] = frozenset(
    entry["key"] for entry in _KNOBS["config"]
    if entry["residence"] == "controller"
)


# Ansible variables the store may not decide, whatever it holds.
#
# THE REASON THIS EXISTS. The registries above answer "which keys may a client
# set". This one answers the question underneath it: which decisions stop being
# the operator's the moment the panel can write them.
#
# The panel writes the store by design, and the converge reads the store as
# root. So every value that reaches a role from the store is a value whoever
# controls the panel controls -- through a bug in the web app, a stolen admin
# session, or a client who edits config.json directly. For most keys that is
# exactly right: a backup endpoint, a mail provider and a schedule are the
# client's to choose. For the ones below it is not, because each of them is
# part of how root is reached, and a compromise that can rewrite them is a
# compromise that keeps itself.
#
# So these stay CONSTANTS in the role defaults. Not "should be"; the gate in
# tests/unit/test_store_is_the_enforcement_point.py reads this table and fails
# the build if any of them is ever defined from a cfg_* fact.
#
# Image references are governed differently and deliberately: a store pin MAY
# move a service's version and may NEVER move its repository, which
# playbooks/filter_plugins/image_pin.py enforces by ignoring a pin whose
# repository disagrees with the floor's. The same gate asserts that property
# holds, so the two rules are declared in one place.
STORE_MAY_NOT_DECIDE: dict[str, str] = {
    # The env allow-list gates both sshd's AcceptEnv and the sudoers env_keep.
    # The payload's drop-ins can never add to it either (see the payload
    # overlay note in reconcile/roles/catena-admin/templates/admin-actions.j2): the set of
    # inputs root will accept is not an input.
    "catena_admin_dispatch_env_passthrough":
        "the set of environment values root accepts from a dispatch",
    # The dispatcher root lanes run directly, linked to the one the forced
    # command runs. A store key here would let the panel choose the program
    # root runs for every action at once.
    "catena_admin_dispatcher_path":
        "the dispatcher root runs for every action",
    # Where root sources action drop-ins from. The dispatcher already refuses a
    # group- or world-writable file; pointing it at another directory would
    # route around that check rather than fail it.
    "catena_admin_actions_overlay_dir":
        "the directory root sources action definitions from",
    "catena_admin_allowed_actions_path":
        "the record of which action names this host authorises",
    # Who the dispatch runs as, and whose authorized_keys carries the forced
    # command.
    "catena_admin_runner_user":
        "the account the dispatch authenticates as",
    "catena_admin_runner_home":
        "the home directory holding the forced command's authorized_keys",
    # Whose key the forced command is authorized for, and the host key a
    # dispatch is checked against.
    "catena_admin_host_ssh_dir":
        "the directory holding the key the dispatch path authorizes",
    # Who may open an SSH session at all.
    "ssh_allowed_users":
        "who may sign in to this host over SSH",
    # What a server taking this one over signs in as, where sshd reads its
    # key, and what it runs.
    "catena_migrate_user":
        "the account a server taking this one over authenticates as",
    "catena_migrate_grant_file":
        "the only file sshd reads the migration account's key from",
    "catena_migrate_connect":
        "the program the migration account's forced command runs",
}


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


def panel_view(store: dict) -> dict:
    """What the settings API's `read` returns: the store's `config`, and its
    secrets named in PANEL_SECRETS."""
    return {
        "secrets": {k: v for k, v in (store.get("secrets") or {}).items()
                    if k in PANEL_SECRETS},
        "config": dict(store.get("config") or {}),
    }


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

    This helper owns `secrets` and `config`, and MERGES
    them into whatever else the file holds. Two other writers share it:
    catena-schedule owns `schedules` and `backup_retention`, and the
    managed-update lane owns `image_pins`. Serialising just the keys this
    helper knows about deletes the others on every converge -- and `image_pins`
    exists precisely so the converge can ask what the on-host lane applied, so a
    converge that wiped it on the way past would answer its own question with
    nothing, every time."""
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


def ensure_user_held_secrets(store: dict) -> list[str]:
    """Mint every USER_HELD secret the install owns (admin + console
    passwords) missing or blank from the store, reconcile-not-overwrite. Runs
    AFTER adopt/apply_inputs so a value handed to the loader is preserved and
    only a store with none mints fresh. MINTED_ON_REQUEST keys are skipped.
    Returns the keys minted -- the installer surfaces these once for the
    user's password manager."""
    secrets_map = store.setdefault("secrets", {})
    minted: list[str] = []
    for key, minter in USER_HELD_SECRETS.items():
        if key in MINTED_ON_REQUEST:
            continue
        cur = secrets_map.get(key)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            secrets_map[key] = minter()
            minted.append(key)
    return minted


TAILNET_PROVIDERS = ("none", "tailscale", "headscale")


def settle_tailnet_provider(store: dict) -> bool:
    """Make TAILNET_PROVIDER state whether this host joins a tailnet.

    It is always stored, `none` included, and it is the one answer: the access
    method follows from it. A store written by an earlier release says the same
    thing differently -- an ACCESS_METHOD key, or a blank provider beside the
    tailnet's own settings -- and keeps the posture it describes: public_ssh is
    `none`, a control-server address is Headscale, a tailnet method or a
    Tailscale OAuth client is Tailscale, and nothing at all is `none`, which is
    every host installed over SSH alone. Returns whether the store changed.
    """
    config = store.setdefault("config", {})
    method = str(config.pop("ACCESS_METHOD", "") or "").strip()
    provider = str(config.get("TAILNET_PROVIDER") or "").strip().lower()
    if method == "public_ssh":
        provider = "none"
    elif provider not in TAILNET_PROVIDERS:
        secrets_map = store.get("secrets") or {}
        if str(config.get("TAILNET_CONTROL_URL") or "").strip():
            provider = "headscale"
        elif method == "tailnet" or secrets_map.get("tailscale_oauth_client_id"):
            provider = "tailscale"
        else:
            provider = "none"
    changed = bool(method) or config.get("TAILNET_PROVIDER") != provider
    config["TAILNET_PROVIDER"] = provider
    return changed


def adopt(store: dict, mapping: dict | None, *, overwrite: bool = False) -> list[str]:
    """Capture secret values into the store. Fill-only by default: the
    converge loader passes every declared secret in scope, and a value already
    in the store is the source of truth. Never stores a blank. Accepts ANY key
    -- the caller pre-filters against secret_names() -- so the
    ROLE_MINTED_SECRETS are captured too, which is why this is separate from
    apply_inputs and its EXTERNAL_SECRETS allowlist.

    ``overwrite=True`` replaces an existing value. Used by reconcile/roles/portainer
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


def preserve_outgoing_zone_token(store: dict, config_in: dict | None) -> str | None:
    """Record the outgoing domain's Cloudflare token before the new one lands.

    Every public hostname is <sub>.<zone>, so changing CLOUDFLARE_ZONE moves the
    whole published surface. What it does NOT do by itself is take down the
    `*.<old zone>` record still pointing at this host's tunnel -- so every name
    under the domain the client left keeps resolving, to the ingress catch-all's
    418. Retiring it is catena-cloudflared-sync's job (RetireZones), and that
    needs a credential for a zone the new token usually does not cover.

    The client had one: it is the token being replaced in this very request. By
    the time the engine runs, the singular `cloudflare_api_token` holds the NEW
    domain's token and the old one is gone, so the only moment it can be kept is
    here, in the sole writer, with both halves of the change in hand.

    It goes into the per-zone map the engine already consults
    (`secrets.cloudflare_api_tokens`, zone -> token), keyed by the domain being
    left. catena-cloudflared-sync drops the entry once the wildcard is gone, so
    a replaced credential does not outlive the one operation it exists for.

    Returns the domain whose token was preserved, or None when this write is not
    a domain change (a first-time set has no outgoing domain, and a host with no
    token has nothing to keep).
    """
    if not config_in or "CLOUDFLARE_ZONE" not in config_in:
        return None
    new_zone = str(config_in.get("CLOUDFLARE_ZONE") or "").strip()
    old_zone = str((store.get("config") or {}).get("CLOUDFLARE_ZONE") or "").strip()
    if not new_zone or not old_zone or new_zone == old_zone:
        return None
    secrets_map = store.get("secrets") or {}
    outgoing = str(secrets_map.get("cloudflare_api_token") or "").strip()
    if not outgoing:
        return None
    tokens = secrets_map.get("cloudflare_api_tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    if tokens.get(old_zone) == outgoing:
        return None
    tokens[old_zone] = outgoing
    store.setdefault("secrets", {})["cloudflare_api_tokens"] = tokens
    return old_zone


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
    # BEFORE either map is written: the outgoing token is only readable while
    # the store still holds it, and this same request usually replaces both the
    # domain and the token.
    if preserve_outgoing_zone_token(store, config_in):
        changed.append("cloudflare_api_tokens")
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
    mint = ap.add_mutually_exclusive_group()
    mint.add_argument("--no-mint", action="store_true",
                      help="do not mint missing internal secrets (seed-only)")
    mint.add_argument("--mint-user-held", action="store_true",
                      help="mint only the missing user-held passwords the "
                           "installer shows, after bootstrap and before the "
                           "converge mints the internal secrets")
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
                             "env-seed-names", "env-reseed-names",
                             "config-vars", "image-pins"],
                    default="secrets",
                    help="what to print as JSON on stdout (default: secrets, "
                         "for an Ansible set_fact of the store's keys). "
                         "secret-names prints the declared key list WITHOUT "
                         "touching the store -- the converge loader reads it "
                         "to know which in-scope variables to capture. "
                         "env-seed-names prints the store keys the inventory "
                         ".env seeds, also without touching the store, so the "
                         "loader knows which .env values to read; "
                         "env-reseed-names prints the subset it writes on every "
                         "converge, with --overwrite. config-vars prints "
                         "the store's config projected onto Ansible variable "
                         "names, for a set_fact that outranks the role "
                         "defaults. image-pins prints what the on-host update "
                         "lane last applied, as {repository: image_ref}, also "
                         "without touching the store.")
    ap.add_argument("--dispatch-stdin", action="store_true",
                    help="serve the catena-admin settings API: read a JSON "
                         "request {op: read|write, secrets, config} from "
                         "stdin. read -> print the panel's view of the store "
                         "(the config and PANEL_SECRETS); write -> apply the "
                         "external creds/config "
                         '(overwrite, no mint) and print {"ok": true}, '
                         "rejecting internal-secret keys.")
    args = ap.parse_args(argv)

    # A pure query, answered before any store I/O: the converge loader asks
    # for this BEFORE the store exists on a fresh box, and reading the answer
    # must never mint or write anything.
    if args.emit == "secret-names":
        print(json.dumps(secret_names()))
        return 0
    if args.emit == "env-seed-names":
        print(json.dumps(sorted(ENV_SEEDED_CONFIG)))
        return 0
    if args.emit == "env-reseed-names":
        print(json.dumps(sorted(ENV_RESEEDED_CONFIG)))
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
            print(json.dumps(panel_view(store)))
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
        adopt(store, incoming, overwrite=args.overwrite)
    apply_inputs(
        store,
        secrets_in=_parse_kv(args.set_secret),
        config_in=_parse_kv(args.set_config),
        overwrite=args.overwrite,
    )
    settle_tailnet_provider(store)
    if args.mint_user_held:
        ensure_user_held_secrets(store)
    elif not args.no_mint:
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
