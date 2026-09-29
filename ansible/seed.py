#!/usr/bin/env python3
"""Seed inventory/<name>/ for Community Catena.

With `-i install.yaml` (bench / power user), generates a fresh inventory:
reads env/host/vault values from the file, prompts for anything missing.

Without one, inventory/<name>/.env must already exist -- copied from
inventory/example/.env.example and filled in, same as any other config
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

No secret file is written into the inventory (no persisted laptop vault), and
no vendor credential is collected at all. The installer reaches and installs
the server and nothing else: the domain and its Cloudflare token, the private
network and its credentials, and the backups are entered in catena-admin >
Settings once the server runs, reached through an SSH forward as the panel
account. An install.yaml that names one of them is refused, with the panel
named, rather than having the value dropped in silence.

The one secret an install.yaml may carry is an admin password override, which
the first converge creates the panel and Portainer admins with. seed writes it
to the TRANSIENT 0600 file given by `--secrets-out`; the installer
(`catena-cli`) threads that file onto the converge as `-e @file`, so the on-box
loader ADOPTS it into /etc/catena/config.json, and deletes it.

Everything else is minted ON-BOX by the converge loader
(helpers/onbox_config.py): the internal service secrets, plus the user-held
passwords the installer surfaces ONCE for the user's password manager.

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
from helpers import render_knobs  # noqa: E402

# --- the knobs the installer asks about -------------------------------------
#
# Declared in helpers/knobs.yml, read here from the rendered registry, and
# rendered into ENV_TEMPLATE by the same renderer. So the prompts, the template
# and the on-box store cannot disagree about which keys exist, what they
# default to, or which values a key accepts.
#
# The template is still read as TEXT, for emit_env: an inventory `.env` keeps
# the explanation beside the value, and what a client reads in their own file
# has to be what they read in the one they copied.
#
# Order is the template's, not the registry's, so a client answering prompts
# and a client editing the file walk the same sequence.
_KNOBS = render_knobs.rendered()
ENV_KEYS: list[tuple[str, str]] = [
    (knob["key"], knob["env"]["default"]) for knob in render_knobs.env_knobs(_KNOBS)
]
# Enumerated values, shown inline in the prompt. Booleans are auto-detected
# from the default, so only a non-boolean enumeration is declared.
ENV_OPTIONS: dict[str, list[str]] = {
    knob["key"]: knob["env"]["options"]
    for knob in render_knobs.env_knobs(_KNOBS)
    if "options" in knob["env"]
}
# Keys with no default that still need an answer, such as the server's
# address: the registry gives them an example instead of a default.
ENV_EXAMPLED: frozenset[str] = frozenset(
    knob["key"] for knob in render_knobs.env_knobs(_KNOBS)
    if knob["env"].get("example")
)

PLACEHOLDER_VALUES = {"REPLACE", "REPLACE-LONG-RANDOM-STRING"}

# The template's illustrative domains. RFC 2606 reserves these, so none of them
# can ever be a real answer -- which is what makes refusing them safe.
#
# They are placeholders that do not LOOK like placeholders, and that is the
# whole problem: "REPLACE" stops a seed, `you@example.com` sails through it.
# The template's ADMIN_EMAIL is identity -- the address the box thinks it
# belongs to -- and a host that converged on the example did not fail, it
# finished, belonging to nobody.
_PLACEHOLDER_DOMAINS = ("example.com", "example.net", "example.org")


def _is_placeholder(value: str) -> bool:
    """Whether this value is the template illustrating a field, not answering it."""
    s = str(value).strip()
    if s in PLACEHOLDER_VALUES:
        return True
    low = s.lower()
    return any(low == d or low.endswith("@" + d) for d in _PLACEHOLDER_DOMAINS)


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

# The one secret an install.yaml may carry: an admin password override. Every
# other secret is minted ON-BOX (helpers/onbox_config.py) or entered in
# catena-admin > Settings.
ADMIN_PASSWORD_KEY = "admin_password"


def server_held(keys) -> list[str]:
    """The names among `keys` the server holds rather than the installer,
    sorted.

    A registry knob the `.env` does not keep is the server's to hold -- the
    domain, the tailnet, the backups, the subdomains -- and every secret but
    the admin override is minted on the box or entered in the panel. A key
    neither the registry nor the store declares is not this check's concern.
    """
    from helpers import onbox_config

    installer_keys = {key for key, _ in ENV_KEYS}
    server_keys = {
        entry["key"] for entry in [*_KNOBS["secrets"], *_KNOBS["config"]]
        if entry["key"] not in installer_keys
    } | set(onbox_config.secret_names())
    server_keys.discard(ADMIN_PASSWORD_KEY)
    return sorted(set(keys) & server_keys)


def not_install_inputs(inp: dict) -> list[str]:
    """The install.yaml keys the installer does not take, sorted. Naming one
    is an install that expects it to take effect, so it is refused rather
    than dropped."""
    return server_held(set((inp.get("env") or {})) | set((inp.get("vault") or {})))


def warn_server_held_lines(env_path: Path) -> None:
    """Name the filled `.env` lines no playbook reads: values catena-admin
    holds on the server."""
    filled = {k for k, v in read_existing_env(env_path).items() if _is_filled(v)}
    held = server_held(filled)
    if held:
        warn(f"{env_path} sets {', '.join(held)}, which the server holds: "
             "nothing reads these lines. Change them in catena-admin > "
             "Settings, and delete them from the .env.")


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
def _check(label: str, ok_: bool, detail: str = "") -> bool:
    mark = "\033[1;32m+\033[0m" if ok_ else "\033[1;31mx\033[0m"
    suffix = f" -- {detail}" if detail else ""
    print(f"  {mark} {label}{suffix}", file=sys.stderr)
    return ok_


def validate_install_structural(inp: dict, env_keys: list) -> int:
    """Field-level install.yaml checks. No network, no filesystem. Returns
    the problem count; prints per-field pass/fail lines to stderr."""
    host = inp.get("host") or {}
    env = inp.get("env") or {}
    inventory = inp.get("inventory")
    problems = 0

    banner("install.yaml -- structural checks")
    if not _check("inventory set", bool(inventory), str(inventory or "(missing)")):
        problems += 1

    if host.get("initial_password"):
        _check("host_initial_password provided (installs the key if it is missing)", True)
    else:
        _check("host_initial_password blank (the key must already open the server)", True)

    # "Optional" env keys are inferred from an empty template default -- the
    # template author's signal that blank is acceptable -- unless the key
    # carries an example, which marks a value with no default that is needed.
    for key, default in env_keys:
        val = env.get(key, default)
        eff = _effective_options(default, ENV_OPTIONS.get(key))
        is_optional = not default and key not in ENV_EXAMPLED
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

    return problems


def validate_install(inp: dict, env_keys: list) -> int:
    """Return the number of hard problems found. 0 = clean."""
    env = inp.get("env") or {}
    problems = validate_install_structural(inp, env_keys)

    # A missing keypair is not a problem -- ensure_ssh_key() offers to generate
    # it before anything is written -- so neither line is a failed check. The
    # label states which file was looked for and the detail states what was
    # found, rather than asserting the file exists and then marking that false.
    banner("Local prerequisites")
    private = os.path.expanduser(env.get("SSH_PRIVATE_KEY", ""))
    for label, path in (("SSH private key", private),
                        ("SSH public key", private and private + ".pub")):
        if not path:
            # Already counted by the structural pass, which requires the key.
            _check(label, False, "no path set in .env")
        elif Path(path).is_file():
            _check(label, True, f"{path} (found)")
        else:
            _check(label, True,
                   f"{path} (not on this machine; seed offers to generate the pair)")

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

    onbox_config declares which names are secrets, so ask it rather than
    reading a prefix: a `vault_` convention makes a spelling load-bearing, and
    an operator who writes the key without it gets their credential silently
    filed as non-secret .env config."""
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


def parse_env_pairs(text: str) -> list[tuple[str, str]]:
    """The KEY=value lines of a `.env`, in file order, quotes stripped."""
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        pairs.append((key.strip(), value))
    return pairs


def read_existing_env(path: Path) -> dict[str, str]:
    """Parse an already-filled-in inventory/<name>/.env (same KEY=value shape
    as the template) into a provided-values dict, so _collect_env_values()
    takes the already-in-provided fast path for every key instead of
    prompting for it."""
    return dict(parse_env_pairs(path.read_text()))


# --- prompting --------------------------------------------------------------
def _is_filled(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    s = str(value).strip()
    return bool(s) and not _is_placeholder(s)


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
        # Enter accepts the default, and for an illustrative one that means
        # accepting `example.com` as the domain this server will serve. The
        # default is still SHOWN, because it is the right shape to copy; it
        # just cannot be the answer.
        if _is_placeholder(val):
            warn("the default is an example -- please enter a real value")
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
        if s and not _is_placeholder(s):
            if eff and s not in eff:
                warn(f"{key}={s!r} is not a valid value (expected one of "
                     f"{', '.join(eff)}); falling through to prompt.")
            else:
                return s
        elif allow_empty and not s:
            return ""
    if not sys.stdin.isatty():
        # An ILLUSTRATIVE default is not an answer, and there is nobody to ask.
        # Returning it here is how a non-interactive seed that simply omitted
        # the key produced a host that believed it served example.com: no
        # prompt, no warning, and a converge that finishes.
        if default and not _is_placeholder(default):
            return default
        if allow_empty:
            return ""
        die(f"{key} is required but not set in install.yaml "
            f"(running non-interactively, cannot prompt).")
    return prompt(label or key, default=default, secret=secret,
                  allow_empty=allow_empty, options=options)


# --- file emission ----------------------------------------------------------
def emit_env(template_text: str, values: dict[str, str], target: Path, *,
             keep_existing: bool = True) -> None:
    """Write an inventory `.env` from the template, the explanation beside each
    value. With `keep_existing` a value already in the file wins over the
    input, so a re-run of `catena-cli install` never rewrites what a client
    edited by hand. The graphical installer passes False: what it saves IS the
    client's edit.

    A key already in the file that the template does not carry is kept, after
    the template's keys, in either mode: the template decides the layout, not
    which of a client's lines survive."""
    target.parent.mkdir(parents=True, exist_ok=True)
    on_disk: dict[str, str] = {}
    if target.exists():
        on_disk = dict(parse_env_pairs(target.read_text()))
    existing = on_disk if keep_existing else {}

    def line(key: str, value: str) -> str:
        if any(c in value for c in " \t#\"'$"):
            value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return f"{key}={value}"

    out: list[str] = []
    template_keys: set[str] = set()
    for raw in template_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(raw)
            continue
        key = stripped.split("=", 1)[0].strip()
        template_keys.add(key)
        if key in existing:
            value = existing[key]
            if key in values and values[key] != value:
                warn(
                    f"{key}: existing .env keeps {value!r}; "
                    f"input value {values[key]!r} ignored"
                )
        else:
            value = values.get(key, "")
        out.append(line(key, value))
    extra = [line(k, v) for k, v in on_disk.items() if k not in template_keys]
    if extra:
        out += ["", "# Not in the template, kept as found."] + extra
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
        # Placeholder; bootstrap.yml emits the install address and
        # catena_cli applies it.
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
def ensure_ssh_key(privkey_path: str) -> None:
    privkey = Path(os.path.expanduser(privkey_path))
    pubkey = Path(str(privkey) + ".pub")
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
def _resolve_admin_override(vault_values: dict[str, str], vault_provided: dict) -> None:
    """Honor an OPTIONAL install.yaml admin-password pin. With no override the
    admin password is minted ON-BOX by the converge loader and surfaced once by
    the installer (seed never mints it). A too-short pin is a hard error."""
    if ADMIN_PASSWORD_KEY in vault_values:
        return
    provided = str(vault_provided.get(ADMIN_PASSWORD_KEY, "")).strip()
    if not provided or provided in PLACEHOLDER_VALUES:
        return
    if len(provided) < ADMIN_PASSWORD_MIN_LEN:
        die(
            f"admin_password in install.yaml is only {len(provided)} "
            f"chars; need at least {ADMIN_PASSWORD_MIN_LEN}. Leave it out to "
            "have the box mint one and show it once."
        )
    vault_values[ADMIN_PASSWORD_KEY] = provided
    ok("Admin password pinned from install.yaml.")


def write_secrets_out(path: Path, secrets: dict[str, str]) -> None:
    """Write the transient adopt map (the admin override, when one was given)
    to a 0600 file. The installer threads it onto the converge as `-e @file`
    for the on-box loader to ADOPT, then deletes it -- so no secret ever
    persists on the laptop. Blank/placeholder values are dropped."""
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
    """Walk the .env template keys, prompting for each. A key with an empty
    template default takes a blank as an answer rather than asking again."""
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


def _ipv4_endpoint(values: dict[str, str]) -> str:
    """The bootstrap target as one line: the provider IPv4 that the first SSH
    lands on, with the port and login that go with it. Echoed back so a stale
    HOST_PUBLIC_IP -- the template's own example address, or the box this
    inventory used to point at -- is caught before bootstrap touches it."""
    ip = (values.get("HOST_PUBLIC_IP") or "").strip() or "HOST_PUBLIC_IP NOT SET"
    port = (values.get("HOST_SSH_PORT") or "").strip() or "22"
    user = (values.get("HOST_INITIAL_USER") or "").strip() or "root"
    return f"{user}@{ip}:{port}"


def _print_summary(*, inventory: str, env_values: dict[str, str]) -> None:
    banner("Summary")
    print(f"  Inventory:      {inventory}", file=sys.stderr)
    print(f"  IPv4 endpoint:  {_ipv4_endpoint(env_values)}", file=sys.stderr)
    print("  Afterwards:     the domain, the private network and backups are "
          "entered in catena-admin > Settings", file=sys.stderr)
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
    # No vault.yml: secrets never persist on the laptop. An admin override goes
    # to the transient --secrets-out file; everything else is minted on-box.
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
        help="Path to write the TRANSIENT 0600 adopt map (an admin password "
             "override, when install.yaml pins one) that the installer threads "
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
    refused = not_install_inputs(inp)
    if refused:
        die(f"install.yaml names {', '.join(refused)}, which the installer does "
            "not take. The domain, the private network, backups and the rest "
            "are set on the server once it runs, in catena-admin > Settings.")

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

    env_keys, env_template = ENV_KEYS, ENV_TEMPLATE.read_text()
    # install.yaml (bench / power user) supplies env values directly and
    # generates the inventory from scratch. Without one, .env must already
    # exist -- copied from inventory/example/.env.example and filled in --
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
    if (inv_dir / ".env").is_file():
        warn_server_held_lines(inv_dir / ".env")
    # Echoed before any prompting, so the box about to be bootstrapped is
    # visible while there is still nothing to undo. Repeated in the summary
    # above the proceed prompt.
    print(f"  IPv4 endpoint: {_ipv4_endpoint(env_provided)}", file=sys.stderr)
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

    # Optional install.yaml admin-password pin; otherwise the box mints it,
    # with every other secret (helpers/onbox_config.py).
    secret_values: dict[str, str] = {}
    _resolve_admin_override(secret_values, inp.get("vault", {}))

    # Validate before any destructive action.
    validation_inp = {
        "inventory": inventory,
        "host": {"initial_password": host_data.get("initial_password") or ""},
        "env": env_values,
        "vault": secret_values,
    }
    problems = validate_install(validation_inp, env_keys)
    if problems:
        die(f"{problems} problem(s) -- fix and re-run.")

    _print_summary(inventory=inventory, env_values=env_values)
    if not args.no_confirm:
        answer = input("Proceed with seed (write inventory files)? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            die("Aborted.", code=130)

    banner("Prereqs")
    ensure_ssh_key(env_values["SSH_PRIVATE_KEY"])

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
