#!/usr/bin/env python3
"""Seed inventory/<name>/ for Community Catena.

With `-i install.yaml` (bench / power user), generates a fresh inventory:
reads env/host/vault values from the file, prompts for anything missing.

Without one, inventory/<name>/.env must already exist -- copied from
inventory/example/.env.example and hand-filled, same as any other config
file -- and seed reads it directly instead of prompting field by field.
hosts.yml/localhost.yml auto-scaffold from skel/ regardless of which path
ran; an existing file's values always win (reconcile-not-overwrite):
  - inventory/<name>/.env                            (non-secret config)
  - inventory/<name>/hosts.yml                        (bootstrap + vps entries)
  - inventory/<name>/localhost.yml                    (preflight anchor)

The group_vars structure (playbooks/group_vars/all/main.yml) is shared: it
is pure `lookup('dotenv', ...)` boilerplate, identical for every inventory,
so it is not written per-inventory here -- only the .env VALUES it reads
differ between inventories.

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
the (non-secret) CLOUDFLARE_ZONE -- the account id is not a seed input at
all, the host engine (catena-cloudflared-sync) resolves it from the token at
activation.

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
# inventory/example/ carries ONLY .env.example -- the one file a self-hoster
# copies. hosts.yml/localhost.yml auto-scaffold from skel/ (below) and are
# never meant to be opened, let alone copied, so they don't sit in the same
# directory implying otherwise.
ENV_TEMPLATE = REPO_ROOT / "inventory" / "example" / ".env.example"
SKEL = REPO_ROOT / "skel"
LOCALHOST_YML_SKEL = SKEL / "localhost.yml"
HOSTS_YML_SKEL = SKEL / "hosts.yml.example"

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

    Declared by name rather than inferred from a `vault_` prefix: a prefix
    makes spelling load-bearing, silently filing an unprefixed credential as
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

# Shown right before the hidden OAuth prompts, so the user has the console
# steps in front of them while entering the creds. playbooks/preflight.yml
# carries the same steps, but only prints them on a failed check -- after the
# creds were already asked for.
TAILSCALE_SETUP_STEPS = """\
One-time setup in https://login.tailscale.com/admin :

  1. Access controls -> edit the JSON -> merge, then Save:

         "tagOwners": {{ "{tag}": ["autogroup:admin"] }}

  2. Settings -> Trust Credentials -> Generate OAuth client.
     - Description: catena-ansible
     - Scopes: check  Auth Keys -> Write
     - Tags:   check  {tag}
       (the tag must match step 1 exactly, or key minting rejects with a 400)
     Generate, then copy the client id AND the secret -- the secret is
     displayed once.

  3. Paste both at the prompts below.
"""

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

    if host.get("initial_password"):
        _check("host_initial_password provided (install_key.py won't prompt)", True)
    else:
        _check("host_initial_password blank (install_key.py will prompt)", True)

    # "Optional" env keys are inferred from an empty template default -- the
    # template author's signal that blank is acceptable.
    for key, default in env_keys:
        val = env.get(key, default)
        eff = _effective_options(default, ENV_OPTIONS.get(key))
        is_optional = not default
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

    return problems


def validate_install(inp: dict, env_keys: list, vault_keys: list) -> int:
    """Return the number of hard problems found. 0 = clean."""
    env = inp.get("env") or {}
    vault = inp.get("vault") or {}
    problems = validate_install_structural(inp, env_keys, vault_keys)

    # A missing keypair is not a problem -- ensure_ssh_key() offers to generate
    # it before anything is written -- so neither line is a failed check. The
    # label states which file was looked for and the detail states what was
    # found, rather than asserting the file exists and then marking that false.
    banner("Local prerequisites")
    for label, path in (("SSH private key", os.path.expanduser(env.get("SSH_PRIVATE_KEY", ""))),
                        ("SSH public key", os.path.expanduser(env.get("SSH_PUBLIC_KEY_FILE", "")))):
        if not path:
            # Already counted by the structural pass, which requires the key.
            _check(label, False, "no path set in .env")
        elif Path(path).is_file():
            _check(label, True, f"{path} (found)")
        else:
            _check(label, True,
                   f"{path} (not on this machine; seed offers to generate the pair)")

    banner("Credentials -- live probes")

    ts_id = vault.get("tailscale_oauth_client_id", "")
    ts_secret = vault.get("tailscale_oauth_client_secret", "")
    api_token = ""
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
        api_token = str((body or {}).get("access_token") or "")
    elif "tailscale_oauth_client_id" in vault_keys:
        _check("Tailscale OAuth token exchange", False, "skipped -- creds not set")
    else:
        _check("Tailscale OAuth token exchange", True,
               "not required -- self-hosted Headscale backend")
        # roles/tailscale asserts on one of these and fails the BOOTSTRAP
        # without it, before any host exists. Caught here instead, where
        # nothing has been touched yet and the message can say what to enter.
        if not (_is_filled(vault.get("headscale_api_key"))
                or _is_filled(vault.get("headscale_preauth_key"))):
            _check("Headscale pre-auth credential", False,
                   "neither headscale_api_key nor headscale_preauth_key is set; "
                   "bootstrap cannot join the tailnet and every later stage is "
                   "reached over it")
            problems += 1
        else:
            _check("Headscale pre-auth credential", True,
                   "headscale_api_key" if _is_filled(vault.get("headscale_api_key"))
                   else "headscale_preauth_key (static)")

    # No Cloudflare live-probe: the API token is entered in catena-admin >
    # Settings, never at install, so there is nothing to verify here. The
    # structural check above still requires CLOUDFLARE_ZONE (hostnames derive
    # from it); the account id + tunnel are resolved on-box from the token by
    # the host engine (catena-cloudflared-sync).
    _check("Cloudflare", True,
           "API token entered later in catena-admin > Settings (tunnel deferred)")

    # Last, so its remedy is the final thing on screen when it fails.
    #
    # This machine has to be ON the tailnet, not merely able to mint keys for
    # it. Everything after bootstrap reaches the VPS at its tailnet address, so
    # a controller that never joined gets a clean bootstrap and then an SSH
    # timeout to a 100.x host that is already hardened. Reuses the token just
    # exchanged, so the strong check costs one request and no extra scope.
    banner("Controller on the tailnet")
    from helpers import tailnet_check

    control_url = str(env.get("TAILNET_CONTROL_URL", "") or "").strip()
    tailnet = tailnet_check.check(token=api_token, control_url=control_url)
    for line in tailnet.lines:
        _check(line, tailnet.ok)
    if not tailnet.ok:
        problems += 1
        print(f"\n{tailnet.remedy}", file=sys.stderr)

    print(file=sys.stderr)
    if problems:
        warn(f"{problems} problem(s) -- install.yaml is NOT ready to deploy.")
        return problems
    ok("install.yaml is ready to deploy.")
    return 0


# --- input loading ----------------------------------------------------------
HOST_PREFIX = "host_"


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


def read_existing_env(path: Path) -> dict[str, str]:
    """Parse an already-filled-in inventory/<name>/.env (same KEY=value shape
    as the template) into a provided-values dict, so _collect_env_values()
    takes the already-in-provided fast path for every key instead of
    prompting for it."""
    pairs, _ = parse_env_template(path)
    return dict(pairs)


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
    "STORAGE_MODE": ["built_in", "attached"],
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


def emit_localhost_yml(target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LOCALHOST_YML_SKEL, target)


def emit_hosts_yml(target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HOSTS_YML_SKEL, target)


def emit_hosts_yml_entry(
    target: Path, host_name: str, env_values: dict[str, str],
) -> None:
    """-i install.yaml path only: writes a LITERAL (non-templated) entry for
    host_name, merging alongside any other hosts already in the file -- an
    install.yaml-driven caller (the test bench) can add a second host to
    the same inventory across separate seed.py runs. public_ip/initial_user/
    ssh_port/ops_user come from the already-collected env_values (the same
    HOST_PUBLIC_IP / HOST_INITIAL_USER / HOST_SSH_PORT / OPS_USER keys
    hosts.yml.example reads via dotenv for the read-existing-inventory
    path), so both paths agree on where these values live."""
    if target.exists():
        data = yaml.safe_load(target.read_text()) or {}
    else:
        data = {}
    children = data.setdefault("all", {}).setdefault("children", {})
    vps_hosts = children.setdefault("vps", {}).setdefault("hosts", {})
    bootstrap_hosts = children.setdefault("bootstrap", {}).setdefault("hosts", {})
    public_ip = env_values.get("HOST_PUBLIC_IP", "")
    ssh_port = env_values.get("HOST_SSH_PORT") or "22"
    # bootstrap_initial_user must be an inventory var (not only the Phase 0.5
    # vars_prompt in bootstrap.yml): vars_prompt outranks it and silently
    # takes its own default ("root") under --no-confirm's no TTY.
    bootstrap_hosts[f"{host_name}-bootstrap"] = {
        "ansible_host": public_ip,
        "ansible_port": ssh_port,
        "bootstrap_initial_user": env_values.get("HOST_INITIAL_USER") or "root",
    }
    vps_host_vars: dict = {
        # Placeholder; bootstrap.yml's post_task rewrites it once the VPS
        # joins the tailnet.
        "ansible_host": "0.0.0.0",
        "ansible_user": env_values.get("OPS_USER") or "ops",
        "ansible_port": ssh_port,
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


def _collect_env_values(
    env_keys: list[tuple[str, str]],
    env_provided: dict,
) -> dict[str, str]:
    """Walk the .env template keys, prompting for each.

    TAILNET_CONTROL_URL + HEADSCALE_USER need no special handling: both carry
    an empty template default, so a blank .env value is taken as an answer
    (Tailscale SaaS) rather than prompted for. The inventory declares the
    tailnet backend by whether those two fields are filled -- there is no
    separate question to ask."""
    banner("Configuration (.env)")
    print("(press Enter to accept the template default)\n", file=sys.stderr)
    env_values: dict[str, str] = {}
    for key, default in env_keys:
        allow_empty = not default
        env_values[key] = fill(
            env_provided, key, default, key,
            allow_empty=allow_empty,
            options=ENV_OPTIONS.get(key),
        )
    return env_values


def _oauth_tag(env_values: dict[str, str]) -> str:
    """First entry of TAILSCALE_TAGS -- the tag the OAuth client has to be
    scoped to. Same rule preflight applies (playbooks/preflight.yml)."""
    first = (env_values.get("TAILSCALE_TAGS") or "").split(",")[0].strip()
    return first or "tag:vps"


def _uses_headscale(env_values: dict[str, str]) -> bool:
    return _is_filled(env_values.get("TAILNET_CONTROL_URL"))


def _tailnet_backend(values: dict[str, str]) -> str:
    """Which control server the inventory declared. Deduced from the Headscale
    fields rather than asked -- blank means Tailscale SaaS -- so it is echoed
    back instead, since the user never confirmed it at a prompt."""
    if not _uses_headscale(values):
        return "Tailscale SaaS (TAILNET_CONTROL_URL blank)"
    user = (values.get("HEADSCALE_USER") or "").strip() or "HEADSCALE_USER NOT SET"
    return f"Headscale at {values['TAILNET_CONTROL_URL'].strip()} (user: {user})"


def _ipv4_endpoint(values: dict[str, str]) -> str:
    """The bootstrap target as one line: the provider IPv4 that the first SSH
    lands on, with the port and login that go with it. Echoed back so a stale
    HOST_PUBLIC_IP -- the template's own example address, or the box this
    inventory used to point at -- is caught before bootstrap touches it."""
    ip = (values.get("HOST_PUBLIC_IP") or "").strip() or "HOST_PUBLIC_IP NOT SET"
    port = (values.get("HOST_SSH_PORT") or "").strip() or "22"
    user = (values.get("HOST_INITIAL_USER") or "").strip() or "root"
    return f"{user}@{ip}:{port}"


def _collect_headscale_secret(vault_provided: dict) -> dict[str, str]:
    """The Headscale half of _collect_install_secrets.

    roles/tailscale asserts on one of these two being in scope and FAILS the
    bootstrap without it, so this cannot be deferred to catena-admin the way
    the Cloudflare and S3 credentials are: the panel it would be entered in is
    published on the tailnet the credential is what joins.

    api_key is preferred -- the converge mints a short-lived tagged pre-auth
    key per run from it, so no long-lived key sits on the VPS. A static
    preauth_key is the fallback for a Headscale whose API is not reachable
    from here."""
    banner("Headscale credential (not stored on this machine)")
    print("The converge needs ONE of these to join the VPS to the tailnet.\n"
          "  headscale_api_key      preferred -- a short-lived tagged pre-auth\n"
          "                         key is minted per run, nothing long-lived\n"
          "                         is left on the VPS. Needs HEADSCALE_USER.\n"
          "  headscale_preauth_key  fallback -- a static key made by hand with\n"
          "                         `headscale preauthkeys create --reusable`\n"
          "\n(input hidden; adopted on-box then discarded from the laptop)\n",
          file=sys.stderr)
    api_key = fill(vault_provided, "headscale_api_key", "", "headscale_api_key",
                   secret=True, allow_empty=True)
    if api_key:
        return {"headscale_api_key": api_key}
    static = fill(vault_provided, "headscale_preauth_key", "",
                  "headscale_preauth_key", secret=True, allow_empty=True)
    return {"headscale_preauth_key": static} if static else {}


def _collect_install_secrets(
    vault_provided: dict, env_values: dict[str, str],
) -> dict[str, str]:
    """Prompt (hidden) for the install-critical vendor creds only: whichever
    credential the chosen tailnet backend needs to JOIN, preceded by the
    console steps that produce it. The Cloudflare API token is NOT collected
    here (Settings-only); everything else is minted on-box. These go to the
    transient --secrets-out file, never a persisted vault.

    Both backends are collected here for the same reason. bootstrap joins the
    VPS to the tailnet, and every stage after it reaches the host at a tailnet
    address -- so a credential that only arrives later, through a panel that is
    only reachable over that tailnet, can never arrive at all."""
    if _uses_headscale(env_values):
        return _collect_headscale_secret(vault_provided)
    banner("Install-critical vendor credentials (not stored on this machine)")
    if sys.stdin.isatty():
        print(TAILSCALE_SETUP_STEPS.format(tag=_oauth_tag(env_values)),
              file=sys.stderr)
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
    env_values: dict[str, str],
    secret_values: dict[str, str],
    expected_creds: int,
) -> None:
    banner("Summary")
    print(f"  Inventory:      {inventory}", file=sys.stderr)
    print(f"  IPv4 endpoint:  {_ipv4_endpoint(env_values)}", file=sys.stderr)
    print(f"  Tailnet:        {_tailnet_backend(env_values)}", file=sys.stderr)
    print(f"  CF zone:        {env_values.get('CLOUDFLARE_ZONE')}", file=sys.stderr)
    print("  CF API token:   entered later in catena-admin > Settings "
          "(never at install)", file=sys.stderr)
    print(f"  Vendor creds:   {len(secret_values)}/{expected_creds} "
          "collected (transient; adopted on-box, not stored here)", file=sys.stderr)
    print(file=sys.stderr)


def _write_inventory_files(
    *,
    inv_dir: Path,
    inventory: str,
    env_template: str,
    env_values: dict[str, str],
    host_name: str | None,
) -> None:
    env_target = inv_dir / ".env"
    banner(f"Writing inventory/{inventory}/ (non-secret files only)")
    emit_env(env_template, env_values, env_target)
    ok(f"wrote {env_target}")
    emit_localhost_yml(inv_dir / "localhost.yml")
    ok(f"wrote {inv_dir / 'localhost.yml'}")
    # No vault.yml: secrets never persist on the laptop (0b). Vendor creds go to
    # the transient --secrets-out file; everything else is minted on-box.
    hosts_target = inv_dir / "hosts.yml"
    if host_name is not None:
        # -i install.yaml: a caller-chosen host name, merged into the file.
        emit_hosts_yml_entry(hosts_target, host_name, env_values)
    else:
        emit_hosts_yml(hosts_target)
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

    env_keys, env_template = parse_env_template(ENV_TEMPLATE)
    # install.yaml (bench / power user) supplies env values directly and
    # generates the inventory from scratch. Without one, .env must already
    # exist -- copied from inventory/example/.env.example and hand-filled --
    # so it answers every field instead of prompting for it one at a time.
    if args.input:
        if inv_dir.exists():
            warn(f"inventory '{inventory}' exists -- host will be merged into existing files.")
        env_provided = inp.get("env", {})
        ok(f"loaded {args.input} -- {len(env_provided)} config value(s)")
    else:
        env_path = inv_dir / ".env"
        if not env_path.is_file():
            die(
                f"{env_path} not found. Copy inventory/example/.env.example "
                f"to {env_path}, fill it in, then re-run."
            )
        env_provided = read_existing_env(env_path)
        ok(f"loaded {env_path} -- {len(env_provided)} config value(s)")
    # Echoed before any prompting, so the box about to be bootstrapped and the
    # control server deduced from the Headscale fields are both visible while
    # there is still nothing to undo. Repeated in the summary above the
    # proceed prompt.
    print(f"  IPv4 endpoint: {_ipv4_endpoint(env_provided)}", file=sys.stderr)
    print(f"  Tailnet:       {_tailnet_backend(env_provided)}", file=sys.stderr)
    env_values = _collect_env_values(env_keys, env_provided)

    # host.initial_password is a genuine secret (the VPS provider's initial
    # root password) that never belongs in .env. host.name only matters on
    # the -i path: the hosts.yml entry seed writes for install.yaml-driven
    # generation (the bench adds a distinctly-named host per run/slot to the
    # same inventory). Public IP and initial SSH user are HOST_PUBLIC_IP /
    # HOST_INITIAL_USER in .env either way, read straight into hosts.yml by
    # the dotenv lookup on the read-existing-inventory path.
    host_data = inp.get("host", {})
    host_name = (host_data.get("name") or f"{inventory}1") if args.input else None

    vault_provided = inp.get("vault", {})
    secret_values = _collect_install_secrets(vault_provided, env_values)
    # A fully-specified install.yaml (power user / test bench) can supply the
    # whole keyset; pass any extra vault_* creds through to the adopt file.
    _absorb_provided_secrets(secret_values, vault_provided)
    # Optional install.yaml admin-password pin; otherwise the box mints it.
    _resolve_admin_override(secret_values, vault_provided)
    # Every other secret -- internal service secrets AND the user-held
    # admin/restic DR keyset -- is minted ON-BOX by the converge loader
    # (helpers/onbox_config.py), never here. The Cloudflare API token is
    # resolved on-box too: entered in catena-admin > Settings, which also
    # derives and persists the account id from it.

    # Validate before any destructive action. The install externals ride the
    # `vault` slot so the structural checks reach them.
    validation_inp = {
        "inventory": inventory,
        "host": {"initial_password": host_data.get("initial_password") or ""},
        "env": env_values,
        "vault": secret_values,
    }
    # A Headscale install collects no OAuth creds, so requiring them here would
    # block a valid inventory on credentials that backend has no API for. It
    # needs exactly one of its own pair instead, which validate_install checks
    # directly -- "one of two" does not fit the all-required key list.
    if _uses_headscale(env_values):
        required_vault: list[str] = []
        expected_creds = 1
    else:
        required_vault = list(INSTALL_EXTERNAL_KEYS)
        expected_creds = len(required_vault)
    problems = validate_install(validation_inp, env_keys, required_vault)
    if problems:
        die(f"{problems} problem(s) -- fix and re-run.")

    _print_summary(
        inventory=inventory, env_values=env_values, secret_values=secret_values,
        expected_creds=expected_creds,
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
        host_name=host_name,
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
