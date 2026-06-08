#!/usr/bin/env python3
"""Seed a new inventory/<name>/ for Community Catena.

Reads install.yaml (`-i`) for non-interactive values, prompts for
anything missing, mints service secrets, and writes:
  - inventory/<name>/.env                            (non-secret config)
  - inventory/<name>/group_vars/all/main.yml         (copied from template)
  - inventory/<name>/group_vars/all/vault.sops.yml   (SOPS+age, self-recipient)
  - inventory/<name>/.sops.yaml                       (recipient policy)
  - inventory/<name>/hosts.yml                        (bootstrap + vps entries)
  - inventory/<name>/localhost.yml                    (preflight anchor)

Nothing else. No ansible-playbook calls, no SSH. The installer (`./catena`)
wraps this for the full preflight -> bootstrap -> site -> validate flow;
seed exits cleanly once files are written.

Community is single-tenant self-hosted, so the vault is encrypted to ONE
age recipient: the self-hoster's own key. On first install the key is
minted (helpers/age_key), saved to a user-owned key file, and shown once;
its public half becomes the only recipient in the per-inventory
.sops.yaml. There is no operator + client dual-recipient handover (that is
a Business managed-service feature).

The vault decrypts with the same age private key via $SOPS_AGE_KEY (raw
key content). The installer loads it from the key file automatically on
subsequent runs; you can also paste it into the shell yourself:
    read -s SOPS_AGE_KEY && export SOPS_AGE_KEY

Usage:
    python seed.py [-i install.yaml] [--inventory NAME] [--no-confirm]
"""
from __future__ import annotations

import argparse
import base64
import getpass
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import json
from pathlib import Path
from typing import Callable

import yaml

# --- paths ------------------------------------------------------------------
# REPO_ROOT is the self-contained ansible/ tree (seed.py sits at its root).
REPO_ROOT = Path(__file__).resolve().parent
SKEL = REPO_ROOT / "inventory" / "example"
ENV_TEMPLATE = SKEL / ".env.example"
MAIN_YML_TEMPLATE = SKEL / "group_vars" / "all" / "main.yml.example"
VAULT_TEMPLATE = SKEL / "group_vars" / "all" / "vault.yml.example"
LOCALHOST_YML_SKEL = SKEL / "localhost.yml"

# Make `from helpers import ...` resolve whether seed.py is run as a script
# or loaded via importlib spec_from_file_location (the test fixture pattern).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from helpers import age_key  # noqa: E402
from helpers import net_retry  # noqa: E402
from helpers import sops_vault  # noqa: E402

# Default age key-file location. Honours the standard age/sops env override
# but otherwise uses the conventional XDG path. (sops_vault deliberately
# only reads SOPS_AGE_KEY raw content; the installer bridges this file into
# that env var so subsequent runs need no manual paste.)
DEFAULT_AGE_KEY_FILE = Path.home() / ".config" / "sops" / "age" / "keys.txt"

PLACEHOLDER_VALUES = {"REPLACE", "REPLACE-LONG-RANDOM-STRING"}

# Vault keys NOT prompted at seed time: either auto-generated below (restic
# password, SSO / Healthchecks service credentials, Dokploy postgres
# password) or minted post-install (Dokploy API key, optional SMTP
# password, optional Nextcloud-S3 credentials, opt-in mailserver secrets).
VAULT_SKIP_KEYS = {
    "vault_dokploy_api_key",
    "vault_dokploy_postgres_password",
    "vault_backup_restic_password",
    "vault_admin_password",
    "vault_keycloak_db_password",
    "vault_oauth2_proxy_cookie_secret",
    "vault_oauth2_proxy_client_secret",
    "vault_dashboard_sync_client_secret",
    "vault_nextcloud_oidc_client_secret",
    "vault_element_oidc_client_secret",
    # Mailserver INTERNAL SSO secrets -- auto-minted (see _resolve_service_secrets).
    "vault_mailserver_oidc_client_secret",
    "vault_mailserver_introspect_client_secret",
    "vault_element_jitsi_jicofo_auth_password",
    "vault_element_jitsi_jicofo_component_secret",
    "vault_element_jitsi_jvb_auth_password",
    "vault_element_jigasi_xmpp_password",
    "vault_healthchecks_secret_key",
    "vault_healthchecks_superuser_password",
    "vault_healthchecks_ping_key",
    "vault_healthchecks_api_key_readonly",
    "vault_healthchecks_api_key_readwrite",
    "vault_smtp_password",
    # Self-hosted mailserver (opt-in template) external secrets: the SMTP
    # smarthost password and the free Spamhaus DQS key. Operator-pasted,
    # same category as vault_smtp_password -- exempt from the preflight
    # presence check so installs without mail do not fail.
    "vault_mailserver_relay_password",
    "vault_mailserver_spamhaus_dqs_key",
    "vault_nextcloud_s3_access_key",
    "vault_nextcloud_s3_secret_key",
    # Auto-minted unconditionally; sit unused until BESZEL_ENABLED=true.
    "vault_beszel_admin_password",
    "vault_beszel_universal_token",
}

# Minimum admin password length -- Dokploy and the SSO provider both accept
# this. 20 is the auto-generate length; operator-supplied values must be at
# least 16 chars.
ADMIN_PASSWORD_MIN_LEN = 16
ADMIN_PASSWORD_AUTO_LEN = 20


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


# --- Cloudflare zone lookup -------------------------------------------------
def fetch_cloudflare_account_id(api_token: str, zone: str) -> str | None:
    """Auto-discover the Cloudflare account ID by reading account.id off the
    zone object. Uses Zone:DNS:Edit (already a required scope). Returns None
    on any failure (caller falls back to prompt)."""
    if not api_token or api_token in PLACEHOLDER_VALUES or not zone:
        return None
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones?name={zone}",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    try:
        with net_retry.urlopen_retry(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None
    if not data.get("success"):
        return None
    results = data.get("result") or []
    if len(results) == 1 and isinstance(results[0].get("account"), dict):
        return results[0]["account"].get("id")
    return None


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
        if key in VAULT_SKIP_KEYS:
            continue
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

    problems += _check_s3_repo(
        "BACKUP_RESTIC_REPO", env.get("BACKUP_RESTIC_REPO", ""), required=True
    )
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

    ts_id = vault.get("vault_tailscale_oauth_client_id", "")
    ts_secret = vault.get("vault_tailscale_oauth_client_secret", "")
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

    cf_token = vault.get("vault_cloudflare_api_token", "")
    cf_zone = env.get("CLOUDFLARE_ZONE", "")
    if _is_filled(cf_token):
        status, body = _http_json(
            "https://api.cloudflare.com/client/v4/user/tokens/verify",
            headers={"Authorization": f"Bearer {cf_token}"},
        )
        if not _check("Cloudflare token valid", status == 200 and body.get("success"),
                      f"HTTP {status}"):
            problems += 1
        if _is_filled(cf_zone):
            status, body = _http_json(
                f"https://api.cloudflare.com/client/v4/zones?name={cf_zone}",
                headers={"Authorization": f"Bearer {cf_token}"},
            )
            zones = body.get("result") or []
            if not _check(f"Cloudflare zone '{cf_zone}' reachable by token",
                          len(zones) == 1, f"{len(zones)} zone(s) matched"):
                problems += 1

        cf_acct = env.get("CLOUDFLARE_ACCOUNT_ID", "")
        if _is_filled(cf_acct):
            _check("CLOUDFLARE_ACCOUNT_ID provided", True, cf_acct)
        else:
            fetched = fetch_cloudflare_account_id(cf_token, cf_zone)
            _check("CLOUDFLARE_ACCOUNT_ID auto-fetchable from zone object",
                   fetched is not None,
                   fetched or "zone not visible to token or unexpected response; set manually")
    else:
        _check("Cloudflare token valid", False, "skipped -- token not set")

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
    mappings) and the flat layout (every key at the top with a `host_` /
    `vault_` prefix, everything else treated as a .env value). A legacy
    `vault_password:` field is silently ignored (ansible-vault era; SOPS+age
    replaces it with $SOPS_AGE_KEY)."""
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
        elif key.startswith("vault_"):
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


def parse_vault_template(path: Path) -> list[str]:
    parsed = yaml.safe_load(path.read_text()) or {}
    return list(parsed.keys())


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


def emit_vault(values: dict[str, str], target: Path) -> None:
    """Write the secrets dict to `target` as a SOPS-encrypted YAML file.
    Recipients come from the per-inventory .sops.yaml via
    sops_vault.encrypt_dict (--filename-override matches the creation_rule).

    An empty values dict is written as a single _initialized marker so the
    file always exists on disk after seed."""
    if target.exists():
        warn(
            f"{target} already exists; not overwriting. "
            f"Use `sops {target}` to change values."
        )
        return
    payload = dict(values) if values else {"_initialized": "true"}
    sops_vault.encrypt_dict(payload, target)


def emit_self_sops_yaml(inv_dir: Path, pubkey: str) -> None:
    """Write `inventory/<name>/.sops.yaml` with ONE creation_rule for
    `vault\\.sops\\.ya?ml$` listing the self-hoster's single age recipient.

    SOPS walks up from the file being encrypted/decrypted and uses the
    nearest .sops.yaml, so this per-inventory file scopes the recipient to
    this inventory's subtree. Idempotent (rewritten on every run -- cheap,
    no secret content)."""
    if not pubkey:
        die("no age public key to write as the vault recipient.")
    rules = [
        {
            "path_regex": r"vault\.sops\.ya?ml$",
            "key_groups": [{"age": [pubkey]}],
        },
    ]
    target = inv_dir / ".sops.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        "# Per-inventory SOPS recipient policy. Generated by seed.py.\n"
        "# Single recipient: your own age key (Community is self-hosted --\n"
        "# no operator). After changing it, run\n"
        "#   sops updatekeys group_vars/all/vault.sops.yml\n"
        "# inside this directory to re-wrap the data key.\n"
        + yaml.safe_dump(
            {"creation_rules": rules},
            default_flow_style=False,
            sort_keys=False,
        )
    )


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


# --- secret minting ---------------------------------------------------------
def _mint_strong_password() -> str:
    """48 random bytes -> 64 base64 chars. Matches `openssl rand -base64 48`."""
    raw = os.urandom(48)
    return base64.b64encode(raw).decode("ascii")


def _mint_hc_api_key() -> str:
    """Healthchecks API keys must be EXACTLY 32 chars. 16 bytes hex = 32 chars."""
    import secrets
    return secrets.token_hex(16)


def _mint_url_safe() -> str:
    """URL-path-safe 32-char string (Healthchecks ping_key lives in URL paths)."""
    import secrets
    return secrets.token_urlsafe(24)


def _mint_oauth2_proxy_cookie_secret() -> str:
    """oauth2-proxy decodes --cookie-secret with base64.RawURLEncoding (URL-safe
    alphabet, no padding) and rejects any decoded length that isn't 16/24/32
    bytes. Mint URL-safe; padding is stripped because oauth2-proxy trims `=`
    before decoding."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


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
def _auto_mint_group(
    vault_values: dict[str, str],
    *,
    existing_vault: bool,
    minters: dict[str, Callable[[], str]],
    ok_message: str,
) -> None:
    """First-install helper: for each (key, mint_fn), mint the secret if it's
    not already present. Skips entirely when an inventory vault already exists
    (re-runs leave existing secrets alone). Emits one ok() banner if anything
    was minted."""
    if existing_vault:
        return
    minted_any = False
    for k, mint_fn in minters.items():
        if k not in vault_values:
            vault_values[k] = mint_fn()
            minted_any = True
    if minted_any:
        ok(ok_message)


def _print_secret_block(
    title: str,
    body: str,
    lines: list[tuple[str, str]],
    *,
    no_confirm: bool,
) -> None:
    """Display a freshly-minted secret with a one-shot yellow-border + Enter
    prompt. `lines` is a list of (label, value)."""
    banner(title)
    print(body, file=sys.stderr)
    print("\033[1;33m" + ("=" * 70) + "\033[0m", file=sys.stderr)
    for label, value in lines:
        prefix = f"  {label}: " if label else "  "
        print(f"\033[1;33m{prefix}{value}\033[0m", file=sys.stderr)
    print("\033[1;33m" + ("=" * 70) + "\033[0m", file=sys.stderr)
    print(file=sys.stderr)
    if not no_confirm and sys.stdin.isatty():
        input("Press Enter once you've copied it to your password manager...")


def _resolve_self_age_key(*, no_confirm: bool, key_file: Path | None = None) -> str:
    """Ensure SOPS_AGE_KEY is set (for encryption) and return the matching
    age PUBLIC key -- the single recipient for this self-hosted vault.

    Order:
      1. SOPS_AGE_KEY already in the environment -> derive its pubkey.
      2. An existing key file -> load it into SOPS_AGE_KEY, derive its pubkey.
      3. Mint a fresh keypair, write it to the key file (0600), load it into
         SOPS_AGE_KEY, show it ONCE, return its pubkey."""
    key_file = key_file or DEFAULT_AGE_KEY_FILE

    raw = os.environ.get(sops_vault.SOPS_AGE_KEY_ENV_VAR)
    if raw and raw.strip():
        try:
            return age_key.pubkey_from_secret(raw)
        except age_key.AgeKeyError as exc:
            die(f"$SOPS_AGE_KEY is set but unusable: {exc}")

    if key_file.is_file():
        try:
            secret = age_key.extract_secret(key_file.read_text())
        except OSError as exc:
            die(f"could not read age key file {key_file}: {exc}")
        if not secret:
            die(f"{key_file} exists but holds no AGE-SECRET-KEY-1 line. "
                "Fix or remove it, or paste your key into $SOPS_AGE_KEY.")
        os.environ[sops_vault.SOPS_AGE_KEY_ENV_VAR] = secret
        ok(f"Loaded age key from {key_file}")
        try:
            return age_key.pubkey_from_secret(secret)
        except age_key.AgeKeyError as exc:
            die(str(exc))

    # First install: mint.
    try:
        minted = age_key.mint()
    except age_key.AgeKeyError as exc:
        die(str(exc))
    key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_file.write_text(age_key.keyfile_body(minted))
    key_file.chmod(0o600)
    os.environ[sops_vault.SOPS_AGE_KEY_ENV_VAR] = minted.privkey
    _print_secret_block(
        "Generated your Catena age key (vault encryption)",
        f"""\
This is YOUR age key. It encrypts and decrypts your secrets vault
(admin password, restic password, Keycloak DB, vendor tokens, ...).

It was saved to:
  {key_file}
Keep that file safe and BACK IT UP to your password manager -- if you
lose it you cannot decrypt your vault, and there is no operator with a
copy (Community is self-hosted).

The installer loads it from that file automatically on later runs. To use
it in a raw shell:
  export SOPS_AGE_KEY=$(grep AGE-SECRET-KEY {key_file})
""",
        [("public key", minted.pubkey), ("private key", minted.privkey)],
        no_confirm=no_confirm,
    )
    return minted.pubkey


def _resolve_admin_password(
    vault_values: dict[str, str],
    vault_provided: dict,
    *,
    existing_vault: bool,
    no_confirm: bool,
) -> None:
    """Resolve the shared Dokploy + Keycloak admin password. Order:
    install.yaml override (if long enough) > auto-mint on first install >
    leave alone on re-run."""
    if "vault_admin_password" in vault_values:
        return
    provided = str(vault_provided.get("vault_admin_password", "")).strip()
    if provided and provided not in PLACEHOLDER_VALUES:
        if len(provided) < ADMIN_PASSWORD_MIN_LEN:
            die(
                f"vault_admin_password in install.yaml is only "
                f"{len(provided)} chars; need at least "
                f"{ADMIN_PASSWORD_MIN_LEN}. Leave blank to auto-generate."
            )
        vault_values["vault_admin_password"] = provided
        ok("Admin password taken from install.yaml.")
        return
    if existing_vault:
        return
    import secrets as _secrets
    admin_pw = _secrets.token_urlsafe(
        # token_urlsafe(n) returns ceil(n*4/3) chars; 15 bytes -> 20 chars.
        15 if ADMIN_PASSWORD_AUTO_LEN == 20 else ADMIN_PASSWORD_AUTO_LEN
    )
    _print_secret_block(
        "Generated admin password (Dokploy + Keycloak)",
        """\
This is the shared admin password for both Dokploy and Keycloak. The
installer will provision the initial admin account on both with this
password. It's saved into the SOPS-encrypted vault.sops.yml; recover
later with:
  sops -d inventory/<name>/group_vars/all/vault.sops.yml | grep vault_admin_password

COPY IT TO YOUR PASSWORD MANAGER NOW -- it's shown only once.
""",
        [("", admin_pw)],
        no_confirm=no_confirm,
    )
    vault_values["vault_admin_password"] = admin_pw


def _resolve_restic_password(
    vault_values: dict[str, str],
    *,
    existing_vault: bool,
    no_confirm: bool,
) -> None:
    """Auto-mint the restic backup encryption password on first install.
    Shown once + saved to the SOPS vault."""
    if "vault_backup_restic_password" in vault_values or existing_vault:
        return
    restic_pw = _mint_strong_password()
    _print_secret_block(
        "Generated restic backup encryption password",
        """\
This password encrypts your restic backup repository. It's saved into
the SOPS-encrypted vault.sops.yml below; recover later with:
  sops -d inventory/<name>/group_vars/all/vault.sops.yml | grep vault_backup_restic_password
(provided you still have $SOPS_AGE_KEY set to your age key content).

For defense in depth, copy it to your password manager too.
""",
        [("", restic_pw)],
        no_confirm=no_confirm,
    )
    vault_values["vault_backup_restic_password"] = restic_pw


def _resolve_cloudflare_account(
    env_values: dict[str, str],
    vault_values: dict[str, str],
    env_provided: dict,
) -> None:
    """Resolve CLOUDFLARE_ACCOUNT_ID: explicit > auto-fetch from zone >
    prompt."""
    provided = str(env_provided.get("CLOUDFLARE_ACCOUNT_ID", "")).strip()
    if provided and provided not in PLACEHOLDER_VALUES:
        env_values["CLOUDFLARE_ACCOUNT_ID"] = provided
        return
    fetched = fetch_cloudflare_account_id(
        vault_values.get("vault_cloudflare_api_token", ""),
        env_values.get("CLOUDFLARE_ZONE", ""),
    )
    if fetched:
        ok(f"Cloudflare account auto-detected from zone: {fetched}")
        env_values["CLOUDFLARE_ACCOUNT_ID"] = fetched
        return
    env_values["CLOUDFLARE_ACCOUNT_ID"] = prompt(
        "CLOUDFLARE_ACCOUNT_ID (zone not visible to token -- enter manually)"
    )


def _collect_env_values(
    env_keys: list[tuple[str, str]],
    env_provided: dict,
) -> dict[str, str]:
    """Walk the .env template keys, prompting for each. Returns the full env
    dict EXCEPT CLOUDFLARE_ACCOUNT_ID (deferred until the vault token is
    collected so we can auto-fetch from the zone)."""
    banner("Configuration (.env)")
    print("(press Enter to accept the template default)\n", file=sys.stderr)
    env_values: dict[str, str] = {}
    deferred_keys = {"CLOUDFLARE_ACCOUNT_ID"}
    # SMTP_FROM has a non-empty placeholder default but blank is still a
    # legitimate answer (deploy without mail).
    allow_empty_with_default = {"SMTP_FROM"}
    for key, default in env_keys:
        if key in deferred_keys:
            continue
        allow_empty = (not default) or key in allow_empty_with_default
        env_values[key] = fill(
            env_provided, key, default, key,
            allow_empty=allow_empty,
            options=ENV_OPTIONS.get(key),
        )
    return env_values


def _collect_vault_values(
    vault_keys: list[str],
    vault_provided: dict,
) -> dict[str, str]:
    """Walk the vault template keys, prompting for each (input hidden).
    Skips keys in VAULT_SKIP_KEYS (auto-minted later)."""
    banner("Secrets (vault.sops.yml -- SOPS-encrypted)")
    print("(input is hidden; placeholder values count as missing)\n", file=sys.stderr)
    vault_values: dict[str, str] = {}
    for key in vault_keys:
        if key in VAULT_SKIP_KEYS:
            provided = str(vault_provided.get(key, "")).strip()
            if provided and provided not in PLACEHOLDER_VALUES:
                vault_values[key] = provided
            continue
        val = fill(vault_provided, key, "", key, secret=True)
        if val:
            vault_values[key] = val
    return vault_values


def _resolve_service_secrets(
    vault_values: dict[str, str],
    *,
    existing_vault: bool,
) -> None:
    """Walk every auto-mint group and mint any missing secret. Skipped
    entirely when the inventory vault already exists.

    Community ships only CE-deployable services. Operator-only secrets
    (Semaphore), the operator's billing portal (Catena portal + Stripe),
    and bench-only pen-test creds (ZAP) are NOT minted here."""
    # SSO service credentials (never shown). The oauth2-proxy cookie secret
    # has a fixed-length-after-decode contract; see _mint_oauth2_proxy_cookie_secret.
    _auto_mint_group(
        vault_values, existing_vault=existing_vault,
        minters={
            "vault_keycloak_db_password": _mint_strong_password,
            "vault_oauth2_proxy_cookie_secret": _mint_oauth2_proxy_cookie_secret,
            "vault_oauth2_proxy_client_secret": _mint_strong_password,
            "vault_dashboard_sync_client_secret": _mint_strong_password,
            "vault_nextcloud_oidc_client_secret": _mint_strong_password,
            "vault_element_oidc_client_secret": _mint_strong_password,
            # Self-hosted mailserver INTERNAL SSO secrets (Roundcube OIDC +
            # Dovecot token introspection): live entirely in our own
            # Keycloak realm, so auto-minted like the other app OIDC clients.
            "vault_mailserver_oidc_client_secret": _mint_strong_password,
            "vault_mailserver_introspect_client_secret": _mint_strong_password,
        },
        ok_message=(
            "SSO service secrets auto-generated (keycloak DB password, "
            "oauth2-proxy cookie + client secrets, dashboard-sync "
            "service-account secret, nextcloud + element OIDC client "
            "secrets, mailserver OIDC + introspection client secrets)."
        ),
    )
    # Healthchecks service credentials (self-hosted heartbeat instance).
    _auto_mint_group(
        vault_values, existing_vault=existing_vault,
        minters={
            "vault_healthchecks_secret_key": _mint_strong_password,
            "vault_healthchecks_superuser_password": _mint_strong_password,
            "vault_healthchecks_ping_key": _mint_url_safe,
            "vault_healthchecks_api_key_readonly": _mint_hc_api_key,
            "vault_healthchecks_api_key_readwrite": _mint_hc_api_key,
        },
        ok_message="Healthchecks secrets auto-generated.",
    )
    # Dokploy postgres password (stable across restores) and coturn
    # static-auth-secret (seed for ephemeral TURN credentials).
    _auto_mint_group(
        vault_values, existing_vault=existing_vault,
        minters={
            "vault_dokploy_postgres_password": _mint_strong_password,
            "vault_turn_static_auth_secret": _mint_strong_password,
        },
        ok_message="Dokploy postgres + coturn static-auth-secret auto-generated.",
    )
    # Nextcloud Talk + HPB bearer secrets. Idle when the talk-hpb service is
    # commented out in the compose.
    _auto_mint_group(
        vault_values, existing_vault=existing_vault,
        minters={
            "vault_nextcloud_talk_signaling_secret": _mint_strong_password,
            "vault_nextcloud_talk_internal_secret": _mint_strong_password,
        },
        ok_message="Nextcloud Talk + HPB bearer secrets auto-generated.",
    )
    # Rocket.Chat-bundled Jitsi component bearer secrets.
    _auto_mint_group(
        vault_values, existing_vault=existing_vault,
        minters={
            "vault_jitsi_prosody_password": _mint_strong_password,
            "vault_jitsi_jicofo_auth_password": _mint_strong_password,
            "vault_jitsi_jicofo_component_secret": _mint_strong_password,
            "vault_jitsi_jvb_auth_password": _mint_strong_password,
        },
        ok_message="Bundled Jitsi component secrets auto-generated.",
    )
    # Element-bundled Jitsi component secrets + jigasi (SIP <-> Jitsi) XMPP
    # password. Separate key family so the two chat stacks can coexist.
    _auto_mint_group(
        vault_values, existing_vault=existing_vault,
        minters={
            "vault_element_jitsi_jicofo_auth_password": _mint_strong_password,
            "vault_element_jitsi_jicofo_component_secret": _mint_strong_password,
            "vault_element_jitsi_jvb_auth_password": _mint_strong_password,
            "vault_element_jigasi_xmpp_password": _mint_strong_password,
        },
        ok_message="Element-bundled Jitsi + jigasi secrets auto-generated.",
    )
    # Beszel resource-monitor credentials. Minted unconditionally so flipping
    # BESZEL_ENABLED later needs no prompt round-trip. The universal token
    # uses the url-safe minter (Beszel rejects X-Token headers over 64 chars).
    _auto_mint_group(
        vault_values, existing_vault=existing_vault,
        minters={
            "vault_beszel_admin_password": _mint_strong_password,
            "vault_beszel_universal_token": _mint_url_safe,
        },
        ok_message="Beszel secrets auto-generated (used iff BESZEL_ENABLED=true).",
    )


def _print_summary(
    *,
    inventory: str,
    host_name: str,
    public_ip: str,
    initial_user: str,
    tailnet_ip_provided: str,
    env_values: dict[str, str],
    vault_keys: list[str],
    vault_values: dict[str, str],
) -> None:
    banner("Summary")
    print(f"  Inventory:      {inventory}", file=sys.stderr)
    print(f"  Host:           {host_name}", file=sys.stderr)
    print(f"  Public IPv4:    {public_ip}", file=sys.stderr)
    print(f"  Initial user:   {initial_user}", file=sys.stderr)
    print(f"  Tailnet IPv4:   {tailnet_ip_provided or '(captured by bootstrap.yml post_task)'}", file=sys.stderr)
    print(f"  CF zone:        {env_values.get('CLOUDFLARE_ZONE')}", file=sys.stderr)
    print(f"  Restic repo:    {env_values.get('BACKUP_RESTIC_REPO')}", file=sys.stderr)
    print(f"  Vault keys set: {len(vault_values)}/{len(vault_keys)}", file=sys.stderr)
    print(file=sys.stderr)


def _write_inventory_files(
    *,
    inv_dir: Path,
    inventory: str,
    env_template: str,
    env_values: dict[str, str],
    vault_values: dict[str, str],
    host_name: str,
    public_ip: str,
    initial_user: str,
    tailnet_ip_provided: str,
    self_pubkey: str,
) -> None:
    env_target = inv_dir / ".env"
    main_target = inv_dir / "group_vars" / "all" / "main.yml"
    vault_target = inv_dir / "group_vars" / "all" / "vault.sops.yml"
    hosts_target = inv_dir / "hosts.yml"
    banner(f"Writing inventory/{inventory}/")
    emit_env(env_template, env_values, env_target)
    ok(f"wrote {env_target}")
    emit_main_yml(main_target)
    ok(f"wrote {main_target}")
    emit_localhost_yml(inv_dir / "localhost.yml")
    ok(f"wrote {inv_dir / 'localhost.yml'}")
    # Write the recipient policy BEFORE encrypting so sops picks the right
    # rule on first encrypt.
    emit_self_sops_yaml(inv_dir, self_pubkey)
    ok(f"wrote {inv_dir / '.sops.yaml'}")
    emit_vault(vault_values, vault_target)
    ok(f"wrote {vault_target}")
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
        "--no-confirm",
        action="store_true",
        help="Skip the 'Proceed?' prompt and the one-shot Enter prompts "
             "after minting secrets. Intended for non-interactive installs.",
    )
    args = ap.parse_args(argv)

    inp = load_input(Path(args.input) if args.input else None)

    # Resolve the self-hoster's age key FIRST (mint on first install) so the
    # vault has a recipient to encrypt to. Sets SOPS_AGE_KEY in this process.
    self_pubkey = _resolve_self_age_key(no_confirm=args.no_confirm)

    # SOPS_AGE_KEY is now guaranteed set; confirm sops can use it before we
    # collect a vault's worth of cleartext into memory.
    try:
        sops_vault._ensure_age_key()
    except sops_vault.SopsError as exc:
        die(str(exc))

    provider = apply_smtp_provider_shortcut(inp)
    if provider:
        env = inp.get("env", {})
        sender_key = "RESEND_SENDER_EMAIL" if provider == "Resend" else "BREVO_SENDER_EMAIL"
        ok(f"{provider} SMTP shortcut: SMTP_* derived from {sender_key}={env.get(sender_key, '')}")

    banner("Target")
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

    vault_keys = parse_vault_template(VAULT_TEMPLATE)
    vault_provided = inp.get("vault", {})
    vault_values = _collect_vault_values(vault_keys, vault_provided)

    existing_vault = (inv_dir / "group_vars" / "all" / "vault.sops.yml").exists()
    _resolve_admin_password(
        vault_values, vault_provided,
        existing_vault=existing_vault, no_confirm=args.no_confirm,
    )
    _resolve_restic_password(
        vault_values,
        existing_vault=existing_vault, no_confirm=args.no_confirm,
    )
    _resolve_service_secrets(vault_values, existing_vault=existing_vault)

    # Deferred env keys (CF account auto-fetch needs the vault token).
    _resolve_cloudflare_account(env_values, vault_values, env_provided)

    # Validate before any destructive action.
    validation_inp = {
        "inventory": inventory,
        "host": {
            "name": host_name,
            "public_ip": public_ip,
            "initial_user": initial_user,
            "initial_password": host_data.get("initial_password") or "",
        },
        "env": env_values,
        "vault": vault_values,
    }
    problems = validate_install(validation_inp, env_keys, vault_keys)
    if problems:
        die(f"{problems} problem(s) -- fix and re-run.")

    _print_summary(
        inventory=inventory, host_name=host_name, public_ip=public_ip,
        initial_user=initial_user, tailnet_ip_provided=tailnet_ip_provided,
        env_values=env_values, vault_keys=vault_keys, vault_values=vault_values,
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
        vault_values=vault_values,
        host_name=host_name, public_ip=public_ip,
        initial_user=initial_user,
        tailnet_ip_provided=tailnet_ip_provided,
        self_pubkey=self_pubkey,
    )

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
