#!/usr/bin/env python3
"""Seed a new inventory/<name>/ for Community Catena.

Reads install.yaml (`-i`) for non-interactive values, prompts for anything
missing, and writes the NON-SECRET inventory files:
  - inventory/<name>/.env                            (non-secret config)
  - inventory/<name>/group_vars/all/main.yml         (copied from template)
  - inventory/<name>/hosts.yml                        (bootstrap + vps entries)
  - inventory/<name>/localhost.yml                    (preflight anchor)

No secret file is written into the inventory (0b: no persisted laptop vault).
The ONLY install-critical vendor cred is the Tailscale OAuth client id/secret
(needed to join the tailnet before any on-box surface exists): it is prompted,
live-validated, and written to the TRANSIENT 0600 file given by
`--secrets-out`. The installer (`catena`) threads that file onto the converge
as `-e @file` (so the on-box loader ADOPTS it into /etc/catena/config.json) and
deletes it; nothing secret persists on the laptop.

The Cloudflare API token is NEVER an install input. It is entered ONLY in
catena-admin > Settings, which writes it to /etc/catena/config.json; the
tunnel is deferred until then. So seed prompts nothing for Cloudflare beyond
the (non-secret) CLOUDFLARE_ZONE, and CLOUDFLARE_ACCOUNT_ID is left blank when
not supplied -- the host engine (catena-cloudflared-sync) resolves + persists
the account id from the token.

Everything else is minted ON-BOX by the converge loader
(helpers/onbox_config.py): the internal service secrets, plus the user-held
DR keyset -- the admin (first-login) + restic (backup) passwords -- which the
installer surfaces ONCE for the user's password manager. S3 backup creds +
repo + schedule are NOT collected here; the user sets them post-install in
catena-admin.

Nothing else. No ansible-playbook calls, no SSH; seed exits cleanly once files
are written.

Usage:
    python seed.py [-i install.yaml] [--inventory NAME] --secrets-out PATH
                   [--no-confirm]
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import json
from pathlib import Path

import yaml

# --- paths ------------------------------------------------------------------
# REPO_ROOT is the self-contained ansible/ tree (seed.py sits at its root).
REPO_ROOT = Path(__file__).resolve().parent
SKEL = REPO_ROOT / "inventory" / "example"
ENV_TEMPLATE = SKEL / ".env.example"
MAIN_YML_TEMPLATE = SKEL / "group_vars" / "all" / "main.yml.example"
LOCALHOST_YML_SKEL = SKEL / "localhost.yml"

# Make `from helpers import ...` resolve whether seed.py is run as a script
# or loaded via importlib spec_from_file_location (the test fixture pattern).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from helpers import net_retry  # noqa: E402

PLACEHOLDER_VALUES = {"REPLACE", "REPLACE-LONG-RANDOM-STRING"}


def _declared_secret_names() -> frozenset[str]:
    """Which install.yaml keys are secrets, per the on-box store's own
    registry. Imported lazily: onbox_config is stdlib-only by design (it runs
    on a minimal target host) and seed.py should not pull it in at import time
    just to answer a question about names.

    This replaced a `vault_` prefix test. A prefix made spelling load-bearing:
    an operator who wrote a credential without it had it silently filed as
    non-secret .env config."""
    from helpers import onbox_config

    return frozenset(onbox_config.secret_names())

# The ONLY secrets collected at install time: the Tailscale OAuth client
# id/secret, needed to join the tailnet before any on-box surface exists. They
# are prompted, live-validated, and written to the transient --secrets-out file
# (never a persisted inventory vault); the on-box loader adopts them on the
# first converge. The Cloudflare API token is NOT here -- it is entered ONLY in
# catena-admin > Settings, never at install. Every other secret -- internal
# service secrets AND the user-held admin/restic DR keyset -- is minted ON-BOX
# (helpers/onbox_config.py). S3 backup creds + repo are set post-install in
# catena-admin, not here.
INSTALL_EXTERNAL_KEYS: tuple[str, ...] = (
    "tailscale_oauth_client_id",
    "tailscale_oauth_client_secret",
)

# Minimum admin password length when a user PINS one via install.yaml (Portainer
# and the SSO provider both accept it). With no override the admin password is
# minted on-box and shown once -- seed never mints it.
ADMIN_PASSWORD_MIN_LEN = 16


# --- output helpers ---------------------------------------------------------
def banner(msg: str) -> None:
    print(f"\n\033[1;34m== {msg}\033[0m", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"\033[1;32m+\033[0m {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"\033[1;33m!\033[0m {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    print(f"\033[1;31mx\033[0m {msg}", file=sys.stderr)
    sys.exit(code)


def run_cmd(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess; abort on non-zero. Streams output to caller."""
    print(f"  $ {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        die(
            f"command failed (exit {result.returncode}): "
            f"{' '.join(str(c) for c in cmd)}",
            code=result.returncode,
        )
    return result


# --- validation -------------------------------------------------------------
def _http_json(url: str, *, headers: dict | None = None, data: bytes | None = None,
               timeout: float = 10.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    try:
        with net_retry.urlopen_retry(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except ValueError:
            return e.code, {"error": body[:200]}
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"error": str(e)}


def _check(label: str, ok_: bool, detail: str = "") -> bool:
    mark = "\033[1;32m+\033[0m" if ok_ else "\033[1;31mx\033[0m"
    suffix = f" -- {detail}" if detail else ""
    print(f"  {mark} {label}{suffix}", file=sys.stderr)
    return ok_


def validate_install_structural(
    inp: dict, env_keys: list, vault_keys: list
) -> int:
    """Field-level install.yaml checks. No network, no filesystem. Returns
    the problem count; prints per-field pass/fail lines to stderr."""
    host = inp.get("host") or {}
    env = inp.get("env") or {}
    vault = inp.get("vault") or {}
    inventory = inp.get("inventory")
    problems = 0

    banner("install.yaml -- structural checks")
    if not _check("inventory set", bool(inventory), str(inventory or "(missing)")):
        problems += 1

    for f in ("name", "public_ip", "initial_user"):
        if not _check(f"host_{f} set", _is_filled(host.get(f)), str(host.get(f) or "(missing)")):
            problems += 1

    if host.get("initial_password"):
        _check("host_initial_password provided (install_key.py won't prompt)", True)
    else:
        _check("host_initial_password blank (install_key.py will prompt)", True)

    # CLOUDFLARE_ACCOUNT_ID is auto-detected from the zone, so blank is ok.
    # SMTP_FROM has a non-empty placeholder default but blank is legitimate
    # (deploy without mail). All other "optional" env keys are inferred from
    # an empty template default -- the template author's signal that blank
    # is acceptable.
    env_allow_empty = {
        "CLOUDFLARE_ACCOUNT_ID",
        "SMTP_FROM",
    }
    for key, default in env_keys:
        val = env.get(key, default)
        eff = _effective_options(default, ENV_OPTIONS.get(key))
        is_optional = key in env_allow_empty or not default
        if is_optional and not _is_filled(val):
            continue
        # YAML bool -> "true"/"false".
        norm = ("true" if val else "false") if isinstance(val, bool) else val
        detail = str(norm) if _is_filled(norm) else "(missing)"
        if not _check(f"env.{key} set", _is_filled(norm), detail):
            problems += 1
            continue
        if eff and str(norm) not in eff:
            if not _check(f"env.{key} valid",
                          False,
                          f"{val!r} not in {'/'.join(eff)}"):
                problems += 1

    for key in vault_keys:
        val = vault.get(key, "")
        if not _check(f"vault.{key} set",
                      _is_filled(val), "***" if _is_filled(val) else "(missing)"):
            problems += 1

    def _check_s3_repo(key: str, value: str, *, required: bool) -> int:
        if not _is_filled(value):
            if required:
                if not _check(f"{key} format", False,
                              "expected s3:<endpoint>/<bucket>, got '' (blank)"):
                    return 1
            return 0
        if not (value.startswith("s3:") and "/" in value[3:]):
            if not _check(f"{key} format", False,
                          f"expected s3:<endpoint>/<bucket>, got {value!r}"):
                return 1
            return 1
        endpoint, bucket = value[3:].split("/", 1)
        # Reject the literal "<client>" sentinel from .env.example -- a
        # template-shaped value made it past prompt-fill.
        if "<client>" in value or "<client>" in bucket:
            if not _check(f"{key} format", False,
                          f"contains <client> placeholder; replace with a real bucket name -- got {value!r}"):
                return 1
            return 1
        _check(f"{key} format", True, f"endpoint={endpoint} bucket={bucket}")
        return 0

    # Backup repo is configured POST-INSTALL in catena-admin (deferred creds),
    # so a blank value at seed time is fine; only a malformed one fails.
    problems += _check_s3_repo(
        "BACKUP_RESTIC_REPO", env.get("BACKUP_RESTIC_REPO", ""), required=False
    )
    # The cold mirror's two repos, same rule: blank is legitimate (mirror off),
    # malformed is not. They are checked here rather than left to the panel
    # because they are seeded in the same file and a typo in a bucket name
    # surfaces at seed time or not until a mirror silently skips for weeks.
    for key in ("BACKUP_WORM_REPO", "NEXTCLOUD_WORM_REPO",
                "NEXTCLOUD_LIVE_REPO"):
        problems += _check_s3_repo(key, env.get(key, ""), required=False)
    return problems


def validate_install(inp: dict, env_keys: list, vault_keys: list) -> int:
    """Return the number of hard problems found. 0 = clean."""
    env = inp.get("env") or {}
    vault = inp.get("vault") or {}
    problems = validate_install_structural(inp, env_keys, vault_keys)

    banner("Local prerequisites")
    pub = os.path.expanduser(env.get("SSH_PUBLIC_KEY_FILE", ""))
    priv = os.path.expanduser(env.get("SSH_PRIVATE_KEY", ""))
    if pub and Path(pub).is_file():
        _check("SSH public key file exists", True, pub)
    else:
        _check("SSH public key file exists", False, f"{pub} (will be generated on install)")
    if priv and Path(priv).is_file():
        _check("SSH private key file exists", True, priv)
    else:
        _check("SSH private key file exists", False, f"{priv} (will be generated on install)")

    banner("Credentials -- live probes")

    ts_id = vault.get("tailscale_oauth_client_id", "")
    ts_secret = vault.get("tailscale_oauth_client_secret", "")
    if _is_filled(ts_id) and _is_filled(ts_secret):
        from base64 import b64encode
        auth = b64encode(f"{ts_id}:{ts_secret}".encode()).decode()
        status, body = _http_json(
            "https://api.tailscale.com/api/v2/oauth/token",
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data=b"grant_type=client_credentials",
        )
        if not _check("Tailscale OAuth token exchange", status == 200,
                      f"HTTP {status}" + (f" {body.get('error', '')}" if body else "")):
            problems += 1
    else:
        _check("Tailscale OAuth token exchange", False, "skipped -- creds not set")

    # No Cloudflare live-probe: the API token is entered in catena-admin >
    # Settings, never at install, so there is nothing to verify here. The
    # structural check above still requires CLOUDFLARE_ZONE (hostnames derive
    # from it); the account id + tunnel are resolved on-box from the token by
    # the host engine (catena-cloudflared-sync).
    _check("Cloudflare", True,
           "API token entered later in catena-admin > Settings (tunnel deferred)")

    print(file=sys.stderr)
    if problems:
        warn(f"{problems} problem(s) -- install.yaml is NOT ready to deploy.")
        return problems
    ok("install.yaml is ready to deploy.")
    return 0


# --- input loading ----------------------------------------------------------
HOST_PREFIX = "host_"


def _fill_smtp_defaults(env: dict, *, host: str, user: str, sender: str) -> None:
    defaults = {
        "SMTP_HOST":    host,
        "SMTP_PORT":    587,
        "SMTP_USER":    user,
        "SMTP_FROM":    sender,
        "SMTP_USE_TLS": True,
    }
    for k, v in defaults.items():
        existing = str(env.get(k, "")).strip()
        if existing and existing not in PLACEHOLDER_VALUES:
            continue
        env[k] = "true" if v is True else ("false" if v is False else str(v))


def apply_smtp_provider_shortcut(inp: dict) -> str | None:
    """Resend / Brevo shortcut: if only the sender email is set, derive the
    SMTP host/port/user so the operator does not have to know them."""
    env = inp.setdefault("env", {})
    resend_sender = str(env.get("RESEND_SENDER_EMAIL", "")).strip()
    brevo_sender = str(env.get("BREVO_SENDER_EMAIL", "")).strip()

    resend_set = resend_sender and resend_sender not in PLACEHOLDER_VALUES
    brevo_set = brevo_sender and brevo_sender not in PLACEHOLDER_VALUES

    if resend_set and brevo_set:
        warn(
            "Both RESEND_SENDER_EMAIL and BREVO_SENDER_EMAIL set -- using Resend. "
            "Clear one to silence this warning."
        )

    if resend_set:
        _fill_smtp_defaults(env, host="smtp.resend.com", user="resend", sender=resend_sender)
        return "Resend"

    if brevo_set:
        _fill_smtp_defaults(env, host="smtp-relay.brevo.com", user=brevo_sender, sender=brevo_sender)
        return "Brevo"

    return None


def split_install_dict(raw: dict) -> dict:
    """Split a parsed install.yaml top-level dict into the
    {inventory, host, env, vault} shape main() consumes.

    Accepts both the nested layout (top-level `host:`, `env:`, `vault:`
    mappings) and the flat layout (every key at the top, `host_`-prefixed for
    host fields, otherwise a secret if the store declares it and a .env value
    if not). A legacy `vault_password:` field is silently ignored.

    The flat layout used to spot secrets by a `vault_` prefix, which made a
    spelling load-bearing: an operator who wrote the key without it got their
    credential silently filed as non-secret .env config. onbox_config declares
    which names are secrets, so ask it."""
    if any(isinstance(raw.get(k), dict) for k in ("host", "env", "vault")):
        return {
            "inventory": raw.get("inventory"),
            "host": raw.get("host") or {},
            "env": raw.get("env") or {},
            "vault": raw.get("vault") or {},
        }

    host: dict = {}
    env: dict = {}
    vault: dict = {}
    for key, value in raw.items():
        if key == "inventory":
            continue
        if key == "vault_password":
            continue
        if key.startswith(HOST_PREFIX):
            host[key[len(HOST_PREFIX):]] = value
        elif key in _declared_secret_names():
            vault[key] = value
        else:
            env[key] = value
    return {
        "inventory": raw.get("inventory"),
        "host": host,
        "env": env,
        "vault": vault,
    }


def load_input(path: Path | None) -> dict:
    """Read install.yaml from disk and split it via split_install_dict.
    Pass None for an empty skeleton (used by interactive flows)."""
    if path is None:
        return {"inventory": None, "host": {}, "env": {}, "vault": {}}
    if not path.is_file():
        die(f"input file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    return split_install_dict(raw)


def parse_env_template(path: Path) -> tuple[list[tuple[str, str]], str]:
    text = path.read_text()
    keys: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        keys.append((key.strip(), value))
    return keys, text


# --- prompting --------------------------------------------------------------
def _is_filled(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    s = str(value).strip()
    return bool(s) and s not in PLACEHOLDER_VALUES


# Enumerated .env values, shown inline in the prompt. Booleans are
# auto-detected from the default (no entry needed). The managed-lifecycle
# knobs (auto-update mode/reboot/provider, scheduled backup tier) are
# Business features and absent from the Community template.
ENV_OPTIONS: dict[str, list[str]] = {
    "CATENA_DEFAULT_LANGUAGE": ["en", "fr"],
    "STORAGE_MODE": ["built_in", "attached"],
    "NEXTCLOUD_VERSIONS_RETENTION": ["auto, 7", "auto, 14", "auto, 30"],
}

_BOOL_VALUES = ("true", "false")


def _effective_options(default: str, options: list[str] | None) -> list[str] | None:
    """Auto-promote bool-like defaults to options=[true, false]; keep
    explicit options the caller passed untouched."""
    if options:
        return options
    if default in _BOOL_VALUES:
        return list(_BOOL_VALUES)
    return None


def _prompt_suffix(default: str, options: list[str] | None, secret: bool) -> str:
    if secret:
        return ""
    eff = _effective_options(default, options)
    if eff:
        return f" ({'/'.join(eff)}, default: {default})" if default else f" ({'/'.join(eff)})"
    if default:
        return f" [{default}]"
    return ""


def prompt(label: str, default: str = "", *, secret: bool = False,
           allow_empty: bool = False, options: list[str] | None = None) -> str:
    eff = _effective_options(default, options)
    suffix = _prompt_suffix(default, options, secret)
    while True:
        if secret:
            val = getpass.getpass(f"{label}: ")
        else:
            val = input(f"{label}{suffix}: ")
        raw = val.strip()
        val = raw or default
        if val and eff and val not in eff:
            warn(f"must be one of: {', '.join(eff)}")
            continue
        if val or allow_empty:
            return val
        warn("required -- please enter a value")


def fill(provided: dict, key: str, default: str, label: str | None = None,
         *, secret: bool = False, allow_empty: bool = False,
         options: list[str] | None = None) -> str:
    """Return the value for `key`, either from `provided` (install.yaml) or
    interactively.

    A constrained field (bool default or explicit `options`) rejects an
    invalid provided value with a warning and falls through to the prompt.
    When stdin is not a TTY (install.yaml-driven, CI), fall back to
    `default` for any key the caller did not supply; required fields with
    no default die with a clear message."""
    eff = _effective_options(default, options)
    if key in provided:
        raw = provided[key]
        if isinstance(raw, bool):
            return "true" if raw else "false"
        s = "" if raw is None else str(raw).strip()
        if s and s not in PLACEHOLDER_VALUES:
            if eff and s not in eff:
                warn(f"{key}={s!r} is not a valid value (expected one of "
                     f"{', '.join(eff)}); falling through to prompt.")
            else:
                return s
        elif allow_empty and not s:
            return ""
    if not sys.stdin.isatty():
        if default:
            return default
        if allow_empty:
            return ""
        die(f"{key} is required but not set in install.yaml "
            f"(running non-interactively, cannot prompt).")
    return prompt(label or key, default=default, secret=secret,
                  allow_empty=allow_empty, options=options)


# --- file emission ----------------------------------------------------------
def emit_env(template_text: str, values: dict[str, str], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if target.exists():
        for raw in target.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            existing[k.strip()] = v

    out: list[str] = []
    for raw in template_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(raw)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in existing:
            value = existing[key]
            if key in values and values[key] != value:
                warn(
                    f"{key}: existing .env keeps {value!r}; "
                    f"input value {values[key]!r} ignored"
                )
        else:
            value = values.get(key, "")
        if any(c in value for c in " \t#\"'$"):
            value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        out.append(f"{key}={value}")
    target.write_text("\n".join(out) + "\n")


def emit_main_yml(target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(MAIN_YML_TEMPLATE, target)


def emit_localhost_yml(target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LOCALHOST_YML_SKEL, target)


def emit_hosts_yml(
    target: Path, host_name: str, public_ip: str, tailnet_ip: str,
    initial_user: str,
) -> None:
    if target.exists():
        data = yaml.safe_load(target.read_text()) or {}
    else:
        data = {}
    children = data.setdefault("all", {}).setdefault("children", {})
    vps_hosts = children.setdefault("vps", {}).setdefault("hosts", {})
    bootstrap_hosts = children.setdefault("bootstrap", {}).setdefault("hosts", {})
    # bootstrap_initial_user must be an inventory var (not only the Phase 0.5
    # vars_prompt in bootstrap.yml): vars_prompt is play-scoped, so Phase 1
    # (harden) and Phase 2 (tailscale) -- separate plays that connect as this
    # user -- would otherwise see it undefined under a non-interactive
    # `catena install --no-confirm`. Seed already collected it, so pin it here.
    bootstrap_hosts[f"{host_name}-bootstrap"] = {
        "ansible_host": public_ip,
        "ansible_port": 22,
        "bootstrap_initial_user": initial_user,
    }
    # The bootstrap host's ansible_host is the public IP (how we reach a
    # fresh box before tailscale joins); the vps host's ansible_host is the
    # tailnet address. Roles that need the routable public IP (coturn TURN
    # URI / ICE candidates) read public_ip directly.
    vps_host_vars: dict = {
        "ansible_host": tailnet_ip,
        "ansible_user": "ops",
        "ansible_port": 22,
    }
    if public_ip:
        vps_host_vars["public_ip"] = public_ip
    vps_hosts[host_name] = vps_host_vars
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))


# --- prereqs ----------------------------------------------------------------
def ensure_ssh_key(privkey_path: str, pubkey_path: str) -> None:
    privkey = Path(os.path.expanduser(privkey_path))
    pubkey = Path(os.path.expanduser(pubkey_path))
    if privkey.exists() and pubkey.exists():
        ok(f"SSH key present at {privkey}")
        return
    print(f"\nNo SSH key at {privkey}.", file=sys.stderr)
    answer = input("Generate one now? [Y/n]: ").strip().lower()
    if answer not in ("", "y", "yes"):
        die("SSH key required to proceed.")
    privkey.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_cmd([
        "ssh-keygen", "-t", "ed25519",
        "-f", str(privkey),
        "-N", "",
        "-C", "catena",
    ])


# --- secret-resolution helpers ----------------------------------------------
def _absorb_provided_secrets(secret_values: dict[str, str], vault_provided: dict) -> None:
    """Pass through ANY declared cred supplied in install.yaml beyond the three
    prompted ones -- S3 keys, restic password, WORM/mail/nextcloud creds, etc.
    An interactive self-hoster only enters the three install-critical creds (the
    rest mint on-box or are set later in catena-admin); a fully-specified
    install.yaml (a power user, or the test bench) can supply the whole keyset,
    which the loader adopts on the first converge. Blank/placeholder values are
    dropped; admin_password is handled by _resolve_admin_override.

    An UNDECLARED key is skipped rather than passed through: it would reach the
    store as a name nothing reads, and a typo that silently becomes a stored
    secret is worse than one that visibly does nothing."""
    known = _declared_secret_names()
    for key, raw in (vault_provided or {}).items():
        if not isinstance(key, str) or key not in known:
            continue
        if key in secret_values or key == "admin_password":
            continue
        val = "" if raw is None else str(raw).strip()
        if val and val not in PLACEHOLDER_VALUES:
            secret_values[key] = val


def _resolve_admin_override(vault_values: dict[str, str], vault_provided: dict) -> None:
    """Honor an OPTIONAL install.yaml admin-password pin. With no override the
    admin password is minted ON-BOX by the converge loader and surfaced once by
    the installer (seed never mints it). A too-short pin is a hard error."""
    if "admin_password" in vault_values:
        return
    provided = str(vault_provided.get("admin_password", "")).strip()
    if not provided or provided in PLACEHOLDER_VALUES:
        return
    if len(provided) < ADMIN_PASSWORD_MIN_LEN:
        die(
            f"admin_password in install.yaml is only {len(provided)} "
            f"chars; need at least {ADMIN_PASSWORD_MIN_LEN}. Leave it out to "
            "have the box mint one and show it once."
        )
    vault_values["admin_password"] = provided
    ok("Admin password pinned from install.yaml.")


def write_secrets_out(path: Path, secrets: dict[str, str]) -> None:
    """Write the transient adopt map (install-critical vendor creds + any admin
    override) to a 0600 file. The installer threads it onto the converge as
    `-e @file` for the on-box loader to ADOPT, then deletes it -- so no secret
    ever persists on the laptop. Blank/placeholder values are dropped."""
    payload = {
        k: v for k, v in secrets.items()
        if isinstance(v, str) and v.strip() and v not in PLACEHOLDER_VALUES
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, yaml.safe_dump(payload, default_flow_style=False).encode())
    finally:
        os.close(fd)
    os.chmod(str(path), 0o600)


def _collect_control_server(
    env_provided: dict, defaults: dict[str, str],
) -> tuple[str, str]:
    """TAILNET_CONTROL_URL + HEADSCALE_USER together pick Tailscale SaaS vs a
    self-hosted Headscale server. install.yaml answering either, or a
    non-interactive run, falls through to the plain per-key default;
    interactive asks the choice once and only prompts the Headscale fields
    when that's the answer, instead of two blank-default prompts every time."""
    already_answered = (
        _is_filled(env_provided.get("TAILNET_CONTROL_URL"))
        or _is_filled(env_provided.get("HEADSCALE_USER"))
    )
    if already_answered or not sys.stdin.isatty():
        url = fill(env_provided, "TAILNET_CONTROL_URL",
                   defaults["TAILNET_CONTROL_URL"], allow_empty=True)
        user = fill(env_provided, "HEADSCALE_USER",
                    defaults["HEADSCALE_USER"], allow_empty=True)
        return url, user
    choice = prompt("Control server", default="tailscale",
                     options=["tailscale", "headscale"])
    if choice == "tailscale":
        return "", ""
    url = fill(env_provided, "TAILNET_CONTROL_URL", "",
               "Headscale base URL (e.g. https://headscale.example.net)")
    user = fill(env_provided, "HEADSCALE_USER", "",
                "Headscale user (tag:vps must be in its ACL tagOwners)")
    return url, user


def _collect_env_values(
    env_keys: list[tuple[str, str]],
    env_provided: dict,
) -> dict[str, str]:
    """Walk the .env template keys, prompting for each. CLOUDFLARE_ACCOUNT_ID is
    allow-empty: it is no longer auto-fetched at seed time (that needed the CF
    token, which is now Settings-only), so a blank flows through and the host
    engine resolves + persists it from the token."""
    banner("Configuration (.env)")
    print("(press Enter to accept the template default)\n", file=sys.stderr)
    env_values: dict[str, str] = {}
    # SMTP_FROM has a non-empty placeholder default but blank is a legitimate
    # answer (deploy without mail); CLOUDFLARE_ACCOUNT_ID is resolved on-box, so
    # a blank at seed time is fine even if the template carries a default.
    allow_empty_with_default = {"SMTP_FROM", "CLOUDFLARE_ACCOUNT_ID"}
    defaults = dict(env_keys)
    for key, default in env_keys:
        if key == "HEADSCALE_USER":
            continue  # resolved alongside TAILNET_CONTROL_URL below
        if key == "TAILNET_CONTROL_URL":
            env_values[key], env_values["HEADSCALE_USER"] = (
                _collect_control_server(env_provided, defaults)
            )
            continue
        allow_empty = (not default) or key in allow_empty_with_default
        env_values[key] = fill(
            env_provided, key, default, key,
            allow_empty=allow_empty,
            options=ENV_OPTIONS.get(key),
        )
    return env_values


def _collect_install_secrets(vault_provided: dict) -> dict[str, str]:
    """Prompt (hidden) for the install-critical vendor creds only -- the
    Tailscale OAuth id/secret. The Cloudflare API token is NOT collected here
    (Settings-only); everything else is minted on-box. These go to the
    transient --secrets-out file, never a persisted vault."""
    banner("Install-critical vendor credentials (not stored on this machine)")
    print("(input hidden; adopted on-box then discarded from the laptop)\n",
          file=sys.stderr)
    values: dict[str, str] = {}
    for key in INSTALL_EXTERNAL_KEYS:
        val = fill(vault_provided, key, "", key, secret=True)
        if val:
            values[key] = val
    return values


def _print_summary(
    *,
    inventory: str,
    host_name: str,
    public_ip: str,
    initial_user: str,
    tailnet_ip_provided: str,
    env_values: dict[str, str],
    secret_values: dict[str, str],
) -> None:
    banner("Summary")
    print(f"  Inventory:      {inventory}", file=sys.stderr)
    print(f"  Host:           {host_name}", file=sys.stderr)
    print(f"  Public IPv4:    {public_ip}", file=sys.stderr)
    print(f"  Initial user:   {initial_user}", file=sys.stderr)
    print(f"  Tailnet IPv4:   {tailnet_ip_provided or '(captured by bootstrap.yml post_task)'}", file=sys.stderr)
    print(f"  CF zone:        {env_values.get('CLOUDFLARE_ZONE')}", file=sys.stderr)
    print("  CF API token:   entered later in catena-admin > Settings "
          "(never at install)", file=sys.stderr)
    expected_creds = len(INSTALL_EXTERNAL_KEYS)
    print(f"  Vendor creds:   {len(secret_values)}/{expected_creds} "
          "collected (transient; adopted on-box, not stored here)", file=sys.stderr)
    print(file=sys.stderr)


def _write_inventory_files(
    *,
    inv_dir: Path,
    inventory: str,
    env_template: str,
    env_values: dict[str, str],
    host_name: str,
    public_ip: str,
    initial_user: str,
    tailnet_ip_provided: str,
) -> None:
    env_target = inv_dir / ".env"
    main_target = inv_dir / "group_vars" / "all" / "main.yml"
    hosts_target = inv_dir / "hosts.yml"
    banner(f"Writing inventory/{inventory}/ (non-secret files only)")
    emit_env(env_template, env_values, env_target)
    ok(f"wrote {env_target}")
    emit_main_yml(main_target)
    ok(f"wrote {main_target}")
    emit_localhost_yml(inv_dir / "localhost.yml")
    ok(f"wrote {inv_dir / 'localhost.yml'}")
    # No vault.yml: secrets never persist on the laptop (0b). Vendor creds go to
    # the transient --secrets-out file; everything else is minted on-box.
    # Placeholder tailnet IP; bootstrap.yml's post_task rewrites it after the
    # VPS joins the tailnet.
    initial_tailnet_ip = tailnet_ip_provided or "0.0.0.0"
    emit_hosts_yml(
        hosts_target, host_name, public_ip, initial_tailnet_ip, initial_user,
    )
    ok(f"wrote {hosts_target}")


# --- main -------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "-i", "--input",
        help="install.yaml with inventory/host/env/vault values. Missing "
             "values are prompted for interactively.",
    )
    ap.add_argument(
        "--inventory",
        help="Inventory name (directory under inventory/). Overrides the "
             "value in install.yaml and skips the prompt.",
    )
    ap.add_argument(
        "--inventory-path",
        help="Path to an inventory directory OUTSIDE this checkout (an operator "
             "seeding an ops-side inventory). Alternative to --inventory; the "
             "directory name is used as the inventory label.",
    )
    ap.add_argument(
        "--secrets-out",
        help="Path to write the TRANSIENT 0600 adopt map (install-critical "
             "vendor creds + any admin override) that the installer threads "
             "onto the converge as `-e @file` and then deletes. Defaults to a "
             "mkstemp temp file whose path is printed.",
    )
    ap.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip the 'Proceed?' prompt. Intended for non-interactive installs.",
    )
    args = ap.parse_args(argv)

    inp = load_input(Path(args.input) if args.input else None)

    provider = apply_smtp_provider_shortcut(inp)
    if provider:
        env = inp.get("env", {})
        sender_key = "RESEND_SENDER_EMAIL" if provider == "Resend" else "BREVO_SENDER_EMAIL"
        ok(f"{provider} SMTP shortcut: SMTP_* derived from {sender_key}={env.get(sender_key, '')}")

    banner("Target")
    if args.inventory_path:
        inv_dir = Path(args.inventory_path).expanduser()
        inventory = inv_dir.name
    else:
        inventory = (
            args.inventory
            or inp.get("inventory")
            or fill({}, "inventory", "prod", "Inventory name (directory under inventory/)")
        )
        inv_dir = REPO_ROOT / "inventory" / inventory
    if inv_dir.exists():
        warn(f"inventory '{inventory}' exists -- host will be merged into existing files.")

    host_data = inp.get("host", {})
    host_name = fill(host_data, "name", f"{inventory}1", "Host name (e.g. prod1)")
    public_ip = fill(host_data, "public_ip", "", "Public IPv4 (from provider)")
    initial_user = fill(host_data, "initial_user", "root",
                        "Initial SSH user (root, ubuntu, debian, ec2-user...)")
    tailnet_ip_provided = host_data.get("tailnet_ip", "").strip() if isinstance(host_data.get("tailnet_ip"), str) else ""

    env_keys, env_template = parse_env_template(ENV_TEMPLATE)
    env_provided = inp.get("env", {})
    env_values = _collect_env_values(env_keys, env_provided)

    vault_provided = inp.get("vault", {})
    secret_values = _collect_install_secrets(vault_provided)
    # A fully-specified install.yaml (power user / test bench) can supply the
    # whole keyset; pass any extra vault_* creds through to the adopt file.
    _absorb_provided_secrets(secret_values, vault_provided)
    # Optional install.yaml admin-password pin; otherwise the box mints it.
    _resolve_admin_override(secret_values, vault_provided)
    # Every other secret -- internal service secrets AND the user-held
    # admin/restic DR keyset -- is minted ON-BOX by the converge loader
    # (helpers/onbox_config.py), never here. The Cloudflare API token +
    # CLOUDFLARE_ACCOUNT_ID are resolved on-box too: the token is entered in
    # catena-admin > Settings, and the host engine derives + persists the
    # account id from it.

    # Validate before any destructive action. The install externals ride the
    # `vault` slot so the structural checks reach them.
    validation_inp = {
        "inventory": inventory,
        "host": {
            "name": host_name,
            "public_ip": public_ip,
            "initial_user": initial_user,
            "initial_password": host_data.get("initial_password") or "",
        },
        "env": env_values,
        "vault": secret_values,
    }
    problems = validate_install(validation_inp, env_keys, list(INSTALL_EXTERNAL_KEYS))
    if problems:
        die(f"{problems} problem(s) -- fix and re-run.")

    _print_summary(
        inventory=inventory, host_name=host_name, public_ip=public_ip,
        initial_user=initial_user, tailnet_ip_provided=tailnet_ip_provided,
        env_values=env_values, secret_values=secret_values,
    )
    if not args.no_confirm:
        answer = input("Proceed with seed (write inventory files)? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            die("Aborted.", code=130)

    banner("Prereqs")
    ensure_ssh_key(env_values["SSH_PRIVATE_KEY"], env_values["SSH_PUBLIC_KEY_FILE"])

    _write_inventory_files(
        inv_dir=inv_dir, inventory=inventory,
        env_template=env_template, env_values=env_values,
        host_name=host_name, public_ip=public_ip,
        initial_user=initial_user,
        tailnet_ip_provided=tailnet_ip_provided,
    )

    # Write the transient adopt map (never into the inventory).
    if args.secrets_out:
        secrets_out = Path(args.secrets_out).expanduser()
    else:
        fd, name = tempfile.mkstemp(prefix="catena-seed-secrets-", suffix=".yml")
        os.close(fd)
        secrets_out = Path(name)
    write_secrets_out(secrets_out, secret_values)
    ok(f"wrote transient adopt map {secrets_out} (0600) -- fed to the converge, "
       "then deleted; not stored in the inventory")

    ok(f"seed complete for inventory '{inventory}'")
    return 0


def _entrypoint() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print("\nCancelled (interrupted).", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
