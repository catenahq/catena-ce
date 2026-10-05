#!/usr/bin/env python3
"""catena-cli -- Community Catena installer / CLI.

A thin wrapper over the Ansible base so self-hosters never touch raw
ansible-playbook. It reaches the server over SSH; everything else -- backups,
the tunnel, the tailnet, the passwords -- runs from catena-admin on the host.
Subcommands:

  init       create an inventory: its .env, every key at its default and
             explained, to fill in before `install`
  install    seed config (reuses seed.py), then run bootstrap, show the
             passwords the server minted, run converge -> validate, and
             show the passwords again. Run again on an installed server, it
             asks nothing and re-applies the whole configuration, the
             operator-run roles the panel's own converge cannot run
             included: the way in when the panel is down. --address reaches
             the server at another address for that run (its tailnet address
             once the panel's Lockdown has closed public SSH); --tags scopes
             the converge leg. Every run ends with the validation. A server
             that presents another host key than the one this machine
             trusts stops the run, unless --reinstalled (or yes when asked)
             says it was reinstalled.
  uninstall  hand control back to the OS: unmask + re-enable Debian's
             apt-daily-upgrade.timer (the unattended-upgrades handback)
             and print teardown guidance

The inventory is a PLAINTEXT group_vars tree; the on-box config store
(/etc/catena/config.json) is the runtime source of truth and rides the
restic backup.

This module IS the CLI. `[project.scripts]` in pyproject.toml exposes it as
the `catena-cli` console script, run from the repository root or `ansible/`:

    uv run catena-cli install --inventory prod

Verb first, inventory as a flag; ansible/README.md says how verbs are named.
Bare `catena-cli` prompts for both.
"""
from __future__ import annotations

import argparse
import configparser
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# This file lives at the root of the self-contained ansible/ tree.
ANSIBLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ANSIBLE_DIR))


def _collections_dir() -> str:
    """Where ansible loads collections from, read out of ansible.cfg so the
    galaxy install writes where ansible reads. Falls back to ansible's own
    default if the key is absent.
    """
    cfg = configparser.ConfigParser()
    try:
        cfg.read(ANSIBLE_DIR / "ansible.cfg")
    except configparser.Error:
        return ".collections"
    value = cfg.get("defaults", "collections_path", fallback=".collections")
    return value.split(os.pathsep)[0].strip() or ".collections"


COLLECTIONS_DIR = _collections_dir()

# The ordered chain every install runs, each leg its own ansible-playbook
# invocation. Every leg reaches the host at its SSH address: --address for
# this run, else HOST_SSH_ADDRESS, else HOST_PUBLIC_IP.
INSTALL_CHAIN = ("bootstrap", "converge", "validate")

# Stages that emit the host's administrative address into
# .bootstrap-output.yml: bootstrap records the install address.
_ADDRESS_STAGES = ("bootstrap",)

# The two lines `install` frames the passwords block with. Nothing else prints
# them, so the graphical installer finds the block in the output by them.
KEYSET_BEGIN = "-----BEGIN CATENA PASSWORDS-----"
KEYSET_END = "-----END CATENA PASSWORDS-----"

# Host binaries the wrapper shells out to.
REQUIRED_BINARIES = ("ansible-playbook", "ansible")


def _c(code: str, msg: str) -> str:
    return f"\033[{code}m{msg}\033[0m"


def banner(msg: str) -> None:
    print(_c("1;34", f"\n== {msg}"), file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    print(_c("1;31", f"x {msg}"), file=sys.stderr)
    raise SystemExit(code)


def check_prereqs(binaries: tuple[str, ...] = REQUIRED_BINARIES) -> list[str]:
    """Return the list of required binaries missing from PATH."""
    return [b for b in binaries if shutil.which(b) is None]


def inventory_path(inventory: str) -> Path:
    return ANSIBLE_DIR / "inventory" / inventory


def resolve_inventory(args: argparse.Namespace) -> Path:
    """The inventory DIRECTORY a command runs against: an explicit
    --inventory-path (an inventory OUTSIDE this checkout -- e.g. an operator's
    ops-side inventory driven by external automation), else the named directory under
    ANSIBLE_DIR/inventory. Pure -- unit-testable."""
    path = getattr(args, "inventory_path", None)
    if path:
        return Path(path).expanduser()
    return inventory_path(args.inventory)


def playbook_cmd(inventory: str | Path, playbook: str, extra: list[str] | None = None) -> list[str]:
    """Build the ansible-playbook argv for one stage. `inventory` is either an
    inventory NAME (resolved under ANSIBLE_DIR/inventory) or an already-resolved
    inventory-directory Path. Pure (no side effects) so it is unit-testable."""
    inv = inventory if isinstance(inventory, Path) else inventory_path(inventory)
    cmd = [
        "ansible-playbook",
        "-i", str(inv),
        str(ANSIBLE_DIR / "playbooks" / f"{playbook}.yml"),
    ]
    if extra:
        cmd.extend(extra)
    return cmd


def _add_inventory_args(parser: argparse.ArgumentParser, *, required: bool) -> None:
    """Add the mutually-exclusive --inventory / --inventory-path pair. A NAME
    resolves under ansible/inventory/; a PATH points at an inventory directory
    OUTSIDE the checkout (an operator driving an ops-side inventory from
    external automation, say). `required` is False only for `install`, which prompts."""
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--inventory",
                       help="inventory name (directory under ansible/inventory/)")
    group.add_argument("--inventory-path",
                       help="path to an inventory directory outside the checkout "
                            "(alternative to --inventory)")


def _address(value: str) -> str:
    """An IP address or host name, as argparse's type for --address."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an address")
    return value


def _ssh_address(env: dict[str, str], address: str | None = None) -> str:
    """Where the install dials the server: --address for this run, else
    HOST_SSH_ADDRESS, else the public IP. Pure -- unit-testable."""
    return (address or (env.get("HOST_SSH_ADDRESS") or "").strip()
            or (env.get("HOST_PUBLIC_IP") or "").strip())


def _stage_extra(stage: str, args: argparse.Namespace) -> list[str]:
    """One leg's passthrough: the address to reach the host at when --address
    names one, on every leg, and the `--tags a,b` scope on the converge leg
    alone, where it re-applies only the roles a change touched (e.g.
    `--tags keycloak,oauth2_proxy` after rotating a secret). Bootstrap and the
    validation always run whole. Pure -- unit-testable."""
    extra: list[str] = []
    address = getattr(args, "address", None)
    if address:
        extra += ["-e", f"ansible_host={address}"]
    tags = (getattr(args, "tags", "") or "").strip()
    if tags and stage == "converge":
        extra += ["--tags", tags]
    return extra


def _vps_hosts(inv_dir: Path) -> list[str]:
    """The hosts hosts.yml puts in the `vps` group."""
    import yaml

    hosts_yml = inv_dir / "hosts.yml"
    if not hosts_yml.is_file():
        return []
    data = yaml.safe_load(hosts_yml.read_text()) or {}
    vps = (((data.get("all") or {}).get("children") or {}).get("vps") or {})
    return list((vps.get("hosts") or {}).keys())


def _key_already_opens(env: dict[str, str], initial_user: str,
                       address: str | None = None) -> str:
    """The account the operator key already logs in to -- the provider's
    initial login, or ops on a server a previous run hardened -- or ""."""
    host = _ssh_address(env, address)
    if not host:
        return ""
    from helpers import install_key

    key = os.path.expanduser(env.get("SSH_PRIVATE_KEY") or "")
    port = int((env.get("HOST_SSH_PORT") or "22").strip() or "22")
    for user in dict.fromkeys((initial_user or "root", env.get("OPS_USER") or "ops")):
        if install_key._key_already_works(host, user, key, port):
            return user
    return ""


def _prompt_provider_password(inv_dir: Path, initial_user: str,
                              address: str | None = None) -> str:
    """Ask for the VPS provider's password for the initial login, up front --
    only when the key does not open the server already.

    Many providers install the public key given when the server is ordered, and
    then there is no password to type. Otherwise bootstrap.yml needs it to add
    the key, and declares it as a play-scoped vars_prompt that, left to itself,
    stops the deploy chain to ask -- after the collections install and the
    operator has walked away. Asking here, before the first playbook, is the
    whole point; ansible-playbook skips a vars_prompt whose name is already an
    extra-var, so the mid-run question never appears."""
    env_path = inv_dir / ".env"
    env: dict[str, str] = {}
    if env_path.is_file():
        import seed

        env = seed.read_existing_env(env_path)
    target = _ssh_address(env, address)
    where = f"{initial_user}@{target}" if target else (initial_user or "the initial user")
    opened = _key_already_opens(env, initial_user, address)
    if opened:
        print(_c("1;32", f"\n+ the SSH key already opens {opened}@{target}: no "
                 "provider password needed"), file=sys.stderr)
        return ""
    print(_c("1;34", f"\n== Provider password for {where}"), file=sys.stderr)
    print("The SSH key does not open that login yet. The password the VPS "
          "provider\nissued for it is used once, to install the key. Leave it "
          "blank if the\nserver is not up yet and the key will be there.",
          file=sys.stderr)
    return getpass.getpass("Provider password (blank to skip): ")


def _settle_host_key(inv_dir: Path, args: argparse.Namespace) -> None:
    """Stop before anything reaches a server that presents another host key
    than the one this machine trusts for it, unless the operator confirms a
    reinstall (`--reinstalled`, or yes when asked): then forget the old key,
    and bootstrap trusts the new one."""
    env_path = inv_dir / ".env"
    env: dict[str, str] = {}
    if env_path.is_file():
        import seed

        env = seed.read_existing_env(env_path)
    host = _ssh_address(env, args.address)
    if not host:
        return
    from helpers import host_key

    port = int((env.get("HOST_SSH_PORT") or "22").strip() or "22")
    trusted, offered = host_key.changed(host, port)
    if not trusted:
        return
    name = host_key.known_name(host, port)
    if not args.reinstalled:
        print(_c("1;33", f"\n! {name} presents another host key than the one "
                 "this machine trusts for it"), file=sys.stderr)
        print("  trusted: " + ", ".join(trusted) + "\n  offered: " + ", ".join(offered)
              + "\nA reinstalled server presents a new key. If this one was not "
              "reinstalled,\nanother machine may be answering at this address.",
              file=sys.stderr)
        if args.no_confirm or not sys.stdin.isatty():
            die(f"stopped before reaching {name}. Run again with --reinstalled "
                "if the server was reinstalled.")
        if input("Was the server reinstalled? [y/N]: ").strip().lower() not in ("y", "yes"):
            die(f"stopped before reaching {name}.")
    host_key.forget(host, port)
    print(_c("1;32", f"+ forgot the old host key of {name}; bootstrap trusts the "
             "new one"), file=sys.stderr)


def _bootstrap_extra_vars(
    inv_dir: Path, input_path: str | None, *, prompt_password: bool = False,
    address: str | None = None,
) -> tuple[list[str], Path | None]:
    """bootstrap.yml collects bootstrap_initial_user + bootstrap_root_password
    via a play-scoped vars_prompt, which outranks any inventory hostvar --
    with no TTY it silently takes its own default ("root"), never the real
    provider user. Extra-vars (-e) are the only thing that outranks
    vars_prompt, so both are injected there.
    bootstrap_initial_user: install.yaml's host.initial_user when given,
    else the inventory's own .env (HOST_INITIAL_USER; on disk by now
    regardless of install.yaml, since seed.py already wrote or confirmed it). bootstrap_root_password comes from install.yaml when given,
    else from the up-front prompt (`prompt_password`, interactive callers
    only); blank is meaningful either way -- bootstrap then skips key
    install, the host assumed already keyed.

    Both names are emitted whenever either source answered, so the play-scoped
    prompt is suppressed rather than asked again mid-chain.

    The provider password is written to a 0600 temp file passed as `-e @file`
    (NOT `-e key=value`), so it never lands on argv, in `ps`, or in the
    printed command / bench logs. Returns (extra_args, tmp_path_to_clean)."""
    import seed  # shares ANSIBLE_DIR on sys.path
    import yaml

    host = (seed.load_input(Path(input_path)).get("host") or {}) if input_path else {}
    overrides: dict[str, str] = {}
    initial_user = host.get("initial_user") or ""
    if not initial_user:
        env_path = inv_dir / ".env"
        if env_path.is_file():
            initial_user = seed.read_existing_env(env_path).get("HOST_INITIAL_USER", "")
    if initial_user:
        overrides["bootstrap_initial_user"] = initial_user
    if input_path:
        overrides["bootstrap_root_password"] = host.get("initial_password") or ""
    else:
        # Emitted even when the answer is blank: leaving the name out is what
        # lets bootstrap.yml's vars_prompt ask again mid-chain, and a caller
        # that declined to prompt (--no-confirm, no TTY) means "do not ask",
        # not "ask later".
        overrides["bootstrap_root_password"] = (
            _prompt_provider_password(inv_dir, initial_user, address)
            if prompt_password and sys.stdin.isatty() else ""
        )

    fd, name = tempfile.mkstemp(prefix="catena-bootstrap-vars-", suffix=".yml")
    os.close(fd)
    tmp = Path(name)
    tmp.write_text(yaml.safe_dump(overrides, default_flow_style=False))
    tmp.chmod(0o600)
    return ["-e", f"@{tmp}"], tmp


def _run(cmd: list[str]) -> None:
    print(_c("1;30", "  $ " + " ".join(cmd)), file=sys.stderr)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        die(f"command failed (exit {result.returncode}): {' '.join(cmd)}",
            code=result.returncode)


def ensure_collections() -> None:
    """Install exactly the Galaxy collections requirements.yml pins.

    A stamp beside them records the requirements.yml they were installed
    from; a tree with no stamp or another one is brought to the pins. The
    in-tree dir (ansible.cfg's collections_path, COLLECTIONS_DIR) is this
    checkout's own, so it is rebuilt, which also drops a collection the pins
    do not name. ANSIBLE_COLLECTIONS_PATH, when set (first path wins), is
    installed into instead -- so the CLI can run from a READ-ONLY checkout
    (e.g. a CI runner against a :ro catena-ce mount) -- and, unless it is the
    in-tree dir, is the caller's: reinstalled over rather than removed."""
    req = ANSIBLE_DIR / "requirements.yml"
    if not req.is_file():
        return
    override = os.environ.get("ANSIBLE_COLLECTIONS_PATH", "").strip()
    in_tree = ANSIBLE_DIR / COLLECTIONS_DIR
    coll = Path(override.split(os.pathsep)[0]).expanduser() if override else in_tree
    stamp = coll / ".requirements.sha256"
    wanted = hashlib.sha256(req.read_bytes()).hexdigest()
    if stamp.is_file() and stamp.read_text().strip() == wanted:
        return
    banner("Installing the Ansible collections requirements.yml pins")
    if coll.resolve() == in_tree.resolve():
        shutil.rmtree(coll, ignore_errors=True)
    coll.mkdir(parents=True, exist_ok=True)
    _run([
        "ansible-galaxy", "collection", "install",
        "-r", str(req), "-p", str(coll), "--force",
    ])
    stamp.write_text(wanted + "\n")


def _preflight_checks(binaries: tuple[str, ...] = REQUIRED_BINARIES) -> None:
    missing = check_prereqs(binaries)
    if missing:
        die(
            "missing required tools on PATH: " + ", ".join(missing) + "\n"
            "  - ansible-core: run this via `uv run catena-cli ...`, or "
            "`pipx install ansible-core`"
        )


def _require_inventory(inv_dir: Path) -> None:
    if not inv_dir.is_dir():
        die(
            f"inventory not found at {inv_dir}. Create it with "
            f"`catena-cli init --inventory {inv_dir.name}`, fill in its .env, "
            f"then run `catena-cli install --inventory {inv_dir.name}` first."
        )


def _run_deploy_chain(
    inv_dir: Path,
    chain: tuple[str, ...],
    *,
    bootstrap_extra: list[str] | None,
    global_extra: list[str] | None = None,
    args: argparse.Namespace | None = None,
) -> None:
    """Run an ordered deploy chain stage by stage, threading the bootstrap
    creds onto the bootstrap stage, `global_extra` onto EVERY stage, and each
    stage's own `_stage_extra` (the --address and --tags of `args`).
    `global_extra` is the transient secret-adopt file (`-e @file`) that
    carries an admin password override, when install.yaml pins one, into each
    play so the on-box loader adopts it. Plus the
    one inter-stage bridge a deploy needs:

      - after `bootstrap`: fold the install address it emitted into
        .bootstrap-output.yml back into hosts.yml, so the later stages (each a
        separate ansible invocation) reach the host instead of the 0.0.0.0
        placeholder. Not with --address: that address is for this run only,
        and every leg carries it already.

    The Portainer API key that the auth stack (Keycloak, oauth2-proxy) is
    gated on is minted in-band by roles/portainer during the converge (it
    mints from the initial admin, sets the key as a fact and writes it to the
    on-box store, and self-heals a missing/rejected key on every converge),
    so a single converge pass deploys everything."""
    for stage in chain:
        banner(f"Stage: {stage}")
        extra = list(bootstrap_extra or []) if stage == "bootstrap" else []
        if global_extra:
            extra = extra + global_extra
        if args is not None:
            extra = extra + _stage_extra(stage, args)
        _run(playbook_cmd(inv_dir, stage, extra or None))
        if stage in _ADDRESS_STAGES and not getattr(args, "address", None):
            from helpers import bootstrap_output
            applied = bootstrap_output.apply_to_inventory(inv_dir)
            for line in applied:
                print(_c("1;32", f"  + bootstrap-output applied {line}"),
                      file=sys.stderr)


def _mktemp_secrets(prefix: str) -> Path:
    """Create an empty 0600 temp file for a transient secret-adopt map."""
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".yml")
    os.close(fd)
    tmp = Path(name)
    tmp.chmod(0o600)
    return tmp


def _print_keyset(keysets: list[dict], as_json: bool) -> None:
    """Print each host's passwords between KEYSET_BEGIN and KEYSET_END: its
    block as text, or with `as_json` its values as one JSON object per line,
    which the graphical installer reads."""
    body = [json.dumps({k: v for k, v in keyset.items() if k != "banner"})
            if as_json else keyset["banner"].rstrip("\n") for keyset in keysets]
    print("\n".join((KEYSET_BEGIN, *body, KEYSET_END)), file=sys.stderr,
          flush=True)


def _show_dr_keyset(inv_dir: Path, extra: list[str] | None = None, *,
                    as_json: bool = False) -> list[dict]:
    """Mint the passwords the install shows (admin + console) if the server
    has none, print them ONCE between KEYSET_BEGIN and KEYSET_END, and return
    them so the install can print them again when it ends.

    Runs right after bootstrap, so the passwords are on screen for the whole
    converge. `extra` is the install's adopt file, which carries an admin
    password install.yaml pins. Each host's block and values arrive as one 0600
    JSON file in a 0700 directory, deleted before this returns.

    Non-fatal: the converge mints the same passwords when this could not, and
    running the install again shows them."""
    banner("Your passwords -- shown once, save them now")
    out_dir = Path(tempfile.mkdtemp(prefix="catena-keyset-"))
    try:
        cmd = playbook_cmd(inv_dir, "show-keyset",
                           ["-e", f"keyset_out={out_dir}", *(extra or [])])
        print(_c("1;30", "  $ " + " ".join(cmd)), file=sys.stderr)
        shown = subprocess.run(cmd).returncode == 0
        keysets = [json.loads(p.read_text())
                   for p in sorted(out_dir.iterdir())] if shown else []
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    if not keysets:
        print(_c("1;33", "! could not show the passwords; the install carries "
                 "on, and `catena-cli install` run again shows them."),
              file=sys.stderr)
        return []
    _print_keyset(keysets, as_json)
    return keysets


def cmd_init(args: argparse.Namespace) -> int:
    import seed

    if args.inventory_path:
        inv_dir = Path(args.inventory_path).expanduser()
        problem = "it already exists" if inv_dir.exists() else ""
        flag = f"--inventory-path {inv_dir}"
    else:
        inv_dir = inventory_path(args.inventory)
        code = seed.inventory_name_problem(args.inventory, inv_dir.parent)
        problem = seed.INVENTORY_NAME_PROBLEMS[code] if code else ""
        flag = f"--inventory {args.inventory}"
    if problem:
        die(f"cannot create the inventory {inv_dir}: {problem}")
    env = seed.write_env(inv_dir, {})
    print(_c("1;32", f"+ wrote {env}"), file=sys.stderr)
    print(f"Fill it in, then run: catena-cli install {flag}", file=sys.stderr)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    _preflight_checks()

    # The transient adopt file seed writes an admin override into. Threaded
    # onto every deploy stage as `-e @file` so the on-box loader adopts it,
    # then deleted.
    secrets_tmp = _mktemp_secrets("catena-install-secrets-")

    banner("Step 1/2 -- collect config (seed)")
    seed_cmd = [sys.executable, str(ANSIBLE_DIR / "seed.py"),
                "--secrets-out", str(secrets_tmp)]
    if args.inventory_path:
        inv_dir = Path(args.inventory_path).expanduser()
        seed_cmd += ["--inventory-path", str(inv_dir)]
    else:
        inventory = args.inventory or input("Inventory name [prod]: ").strip() or "prod"
        inv_dir = inventory_path(inventory)
        seed_cmd += ["--inventory", inventory]
    if args.input:
        seed_cmd += ["-i", args.input]
    if args.no_confirm:
        seed_cmd += ["--no-confirm"]
    result = subprocess.run(seed_cmd)
    if result.returncode != 0:
        secrets_tmp.unlink(missing_ok=True)
        die(f"seed failed (exit {result.returncode}); nothing deployed.",
            code=result.returncode)

    adopt_extra = ["-e", f"@{secrets_tmp}"]
    bootstrap_vars_tmp = None
    try:
        # One address for every leg needs one host to give it to.
        if args.address and len(_vps_hosts(inv_dir)) > 1:
            die(f"--address names one server, and {inv_dir / 'hosts.yml'} lists "
                f"{len(_vps_hosts(inv_dir))} in its vps group. Run the install "
                "from an inventory that holds that server alone.")
        # Before the key probe and the provider password, both of which go to
        # whatever answers at the address.
        _settle_host_key(inv_dir, args)
        # The last question of the run. Everything the deploy chain needs is
        # answered before the first playbook starts, so nothing stops to ask
        # once it is under way.
        bootstrap_extra, bootstrap_vars_tmp = _bootstrap_extra_vars(
            inv_dir, args.input, prompt_password=not args.no_confirm,
            address=args.address)

        ensure_collections()

        banner("Step 2/2 -- deploy (" + " -> ".join(INSTALL_CHAIN) + ")")
        # The passwords come right after bootstrap, the first leg: the server
        # holds its store by then, and the converge after it is the long part.
        _run_deploy_chain(inv_dir, INSTALL_CHAIN[:1], bootstrap_extra=bootstrap_extra,
                          global_extra=adopt_extra, args=args)
        keysets = _show_dr_keyset(inv_dir, adopt_extra + _stage_extra("show-keyset", args),
                                  as_json=args.keyset_json)
        _run_deploy_chain(inv_dir, INSTALL_CHAIN[1:], bootstrap_extra=bootstrap_extra,
                          global_extra=adopt_extra, args=args)
        if keysets:
            banner("Your passwords, as shown after bootstrap")
            _print_keyset(keysets, args.keyset_json)
    finally:
        if bootstrap_vars_tmp is not None:
            bootstrap_vars_tmp.unlink(missing_ok=True)
        secrets_tmp.unlink(missing_ok=True)
    banner("Install complete.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()
    banner("Uninstall -- hand unattended-upgrades back to Debian")
    _run(playbook_cmd(inv_dir, "uninstall"))
    return 0


# Subcommands offered by the interactive menu, in run order. Also the set of
# argv[0] tokens `_normalize_argv` recognizes as "this is a subcommand, not
# an inventory name".
MENU_COMMANDS = (
    ("init", "Create an inventory to fill in"),
    ("install", "Set up a host, or re-apply its configuration"),
    ("uninstall", "Hand unattended-upgrades back to the OS"),
)
KNOWN_COMMANDS = tuple(name for name, _ in MENU_COMMANDS)


def _choose_command() -> str:
    """Print the numbered menu and return the chosen subcommand name."""
    print(_c("1;34", "catena-cli -- choose an operation:"), file=sys.stderr)
    for i, (name, desc) in enumerate(MENU_COMMANDS, 1):
        print(f"  {i:>2}) {name:<18} {desc}", file=sys.stderr)
    default = str(KNOWN_COMMANDS.index("install") + 1)
    choice = input(f"\nNumber [{default}=install]: ").strip() or default
    try:
        return MENU_COMMANDS[int(choice) - 1][0]
    except (ValueError, IndexError):
        die(f"not a valid choice: {choice!r}")


def interactive_menu() -> list[str]:
    """Bare `catena-cli` (no args): prompt for inventory, then the operation, and
    return the argv for the chosen subcommand. Pure of side effects beyond
    stdin/stdout, so the caller just feeds the result back through the
    parser."""
    inv = input("Inventory name [prod]: ").strip() or "prod"
    return [_choose_command(), "--inventory", inv]


def _reject_bare_inventory(argv: list[str]) -> None:
    """Refuse a leading token that is neither a verb nor a flag.

    One shape, said once. A first token like `prod` is an inventory name in
    the position a verb belongs, which argparse would otherwise report as an
    invalid choice without saying what the right shape is."""
    if not argv or argv[0] in KNOWN_COMMANDS or argv[0].startswith("-"):
        return
    rest = " ".join(argv[1:])
    die(f"{argv[0]!r} is not a command. The inventory is a flag, and the verb "
        f"comes first: `catena-cli {rest or '<verb>'} --inventory {argv[0]}`. "
        f"Commands: {', '.join(KNOWN_COMMANDS)}.", code=2)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="catena-cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Not required: bare `catena-cli` drops into interactive_menu() instead of
    # erroring, so a self-hoster can discover the subcommands.
    sub = ap.add_subparsers(dest="command", required=False)

    p_init = sub.add_parser(
        "init", help="create an inventory's .env, every key at its default")
    _add_inventory_args(p_init, required=True)
    p_init.set_defaults(func=cmd_init)

    p_install = sub.add_parser(
        "install",
        help="seed, then " + ", ".join(INSTALL_CHAIN))
    _add_inventory_args(p_install, required=False)
    p_install.add_argument("-i", "--input", help="install.yaml for non-interactive values")
    p_install.add_argument("--no-confirm", action="store_true",
                           help="skip seed confirmation + one-shot secret prompts")
    p_install.add_argument("--keyset-json", action="store_true",
                           help="print the passwords as one JSON object per "
                                "host, for the graphical installer")
    p_install.add_argument("--address", type=_address,
                           help="reach the host at this address for this run, "
                                "such as its tailnet address once the panel's "
                                "Lockdown has closed public SSH")
    p_install.add_argument("--tags", default="",
                           help="comma-separated ansible tags that scope the "
                                "converge leg (e.g. keycloak,oauth2_proxy)")
    p_install.add_argument("--reinstalled", action="store_true",
                           help="the server was reinstalled: trust the new "
                                "host key it presents")
    p_install.set_defaults(func=cmd_install)

    p_uni = sub.add_parser(
        "uninstall",
        help="hand unattended-upgrades back to the OS + print teardown guidance",
    )
    _add_inventory_args(p_uni, required=True)
    p_uni.set_defaults(func=cmd_uninstall)

    return ap


def main(argv: list[str] | None = None) -> int:
    # Run from the ansible/ dir so ansible.cfg + relative inventory/playbook
    # paths resolve regardless of where the user invoked us.
    os.chdir(ANSIBLE_DIR)
    argv = list(sys.argv[1:] if argv is None else argv)
    # `--install` is an alias for the `install` subcommand (so `catena-cli
    # --install` == `catena-cli install`); rewrite it to the positional form and
    # let install's own parser handle any trailing flags.
    if argv and argv[0] == "--install":
        argv = ["install", *argv[1:]]
    # Bare `catena-cli` (no args): interactive menu, when a TTY is present.
    if not argv:
        if not sys.stdin.isatty():
            die("no subcommand given (and no TTY for the menu). "
                "Try `catena-cli install --inventory <name>` or `catena-cli --help`.",
                code=2)
        argv = interactive_menu()
    else:
        _reject_bare_inventory(argv)
    args = build_parser().parse_args(argv)
    return args.func(args)


def _entry(argv: list[str] | None = None) -> int:
    """Entry point for the `catena-cli` console script; keeps the Ctrl-C handling
    in one place."""
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("\nCancelled (interrupted).", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(_entry())
