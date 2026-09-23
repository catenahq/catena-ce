#!/usr/bin/env python3
"""catena -- Community Catena installer / CLI.

A thin wrapper over the Ansible base so self-hosters never touch raw
ansible-playbook. Subcommands:

  install    seed config + secrets (reuses seed.py), then run
             preflight -> bootstrap -> converge -> validate
  converge   re-run converge.yml (apply config changes / new app tags)
  validate   run validate.yml (on-host + tailnet + external checks)
  backup     trigger an on-demand snapshot (the manual CE backup)
  restore    run restore.yml (in-place whole-host restore)
  recover    whole-host DR onto a FRESH replacement box: preflight ->
             bootstrap -> restore -> converge -> validate (reuses the
             existing inventory's group_vars + .env; no seed)
  rollback   roll a STILL-RUNNING host back to a prior snapshot: preflight ->
             restore -> converge -> validate (in place, no bootstrap)
  rotate-tunnel
             regenerate the host's Cloudflare tunnel without a full
             converge (manual maintenance); authenticates as the
             Cloudflare API token already in the host's store
  rotate-tailscale
             re-authenticate the node to the tailnet (force re-auth;
             manual maintenance)
  show-keyset
             re-display the admin / restic / console passwords and the
             first-login URLs (install shows them once; this asks again)
  uninstall  hand control back to the OS: unmask + re-enable Debian's
             apt-daily-upgrade.timer (the unattended-upgrades handback)
             and print teardown guidance

The inventory is a PLAINTEXT group_vars tree; the on-box config store
(/etc/catena/config.json) is the runtime source of truth and rides the
restic backup.

This module IS the CLI. `[project.scripts]` in pyproject.toml exposes it as
the `catena` console script, so there is one file to read and one way to run
it, from `ansible/`:

    uv run catena install --inventory prod

Verb first, inventory as a flag. A verb that runs a single playbook carries
that playbook's name. Bare `catena` prompts for both.
"""
from __future__ import annotations

import argparse
import configparser
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# This file lives at the root of the self-contained ansible/ tree.
ANSIBLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ANSIBLE_DIR))


def _collections_dir() -> str:
    """Where ansible loads collections from, read out of ansible.cfg.

    Derived rather than duplicated: a hardcoded copy drifted from the config
    once already and left the galaxy install writing to a directory ansible
    never read. Falls back to ansible's own default if the key is absent.
    """
    cfg = configparser.ConfigParser()
    try:
        cfg.read(ANSIBLE_DIR / "ansible.cfg")
    except configparser.Error:
        return ".collections"
    value = cfg.get("defaults", "collections_path", fallback=".collections")
    return value.split(os.pathsep)[0].strip() or ".collections"


COLLECTIONS_DIR = _collections_dir()

# The ordered converge chain a fresh install runs. preflight is a SEPARATE
# invocation BEFORE bootstrap so a stray --limit can never skip it.
#
# `lockdown` is its own leg, after the converge and before validate. It is the
# one step that can make a host unreachable, so it runs alone and last, where a
# failure is a failure of lockdown rather than of a converge that did fifteen
# other things correctly -- and validate then measures the posture it produced.
INSTALL_CHAIN = ("preflight", "bootstrap", "converge", "lockdown", "validate")

# Whole-host disaster recovery onto a FRESH replacement box: same as install
# but with `restore` inserted after bootstrap (push the last snapshot onto the
# new host before the converge), and no seed step (the existing inventory's
# group_vars + .env are reused). Mirrors the operator disaster_recovery flow.
RECOVER_CHAIN = ("preflight", "bootstrap", "restore", "converge", "lockdown",
                 "validate")

# Roll a STILL-RUNNING host back to a prior restic snapshot: restore in place
# then re-converge + validate. No bootstrap (the box is alive and reachable at
# its administrative address). The `converge` leg is load-bearing for password
# coherence (pg_password_reconcile re-aligns the store -> swarm secret ->
# pg_authid if a secret was rotated between snapshot and rollback) -- never drop
# it. `lockdown` runs for the same reason it does on the other two: a restore
# replaces /etc, and the firewall state it brings back is the snapshot's.
ROLLBACK_CHAIN = ("preflight", "restore", "converge", "lockdown", "validate")

# Host binaries the wrapper shells out to. ansible-playbook/ansible run the
# base; the plaintext vault needs no separate secret-tooling binary.
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


def _tags_extra(args: argparse.Namespace) -> list[str] | None:
    """Turn a `--tags a,b` CLI value into the ansible-playbook passthrough,
    or None when unset. Lets `converge`/`validate` run a tag-scoped subset
    (e.g. `catena converge --tags keycloak,oauth2_proxy` to re-apply only
    the auth roles after rotating a secret), Pure -- unit-testable."""
    tags = (getattr(args, "tags", "") or "").strip()
    return ["--tags", tags] if tags else None


def _snapshot_extra(args: argparse.Namespace) -> list[str] | None:
    """Turn a `--snapshot <id>` CLI value into the restore.yml extra-var
    passthrough (`-e restore_snapshot=<id>`), or None when unset (restore.yml
    then defaults to `latest`). Lets a self-hoster restore from a SPECIFIC
    restic snapshot, not just the most recent. Pure -- unit-testable."""
    snap = (getattr(args, "snapshot", "") or "").strip()
    return ["-e", f"restore_snapshot={snap}"] if snap else None


def _prompt_provider_password(inv_dir: Path, initial_user: str) -> str:
    """Ask for the VPS provider's password for the initial login, up front.

    bootstrap.yml declares this as a play-scoped vars_prompt, so left to
    itself it stops the deploy chain to ask -- after preflight has already
    run and the operator has walked away. Asking here, before the first
    playbook, is the whole point; ansible-playbook skips a vars_prompt whose
    name is already an extra-var, so the mid-run question never appears.

    Blank is a real answer: Phase 0.5 then skips the key install and assumes
    the box is already keyed, which is what the vars_prompt default means."""
    env_path = inv_dir / ".env"
    target = ""
    if env_path.is_file():
        import seed

        target = seed.read_existing_env(env_path).get("HOST_PUBLIC_IP", "").strip()
    where = f"{initial_user}@{target}" if target else (initial_user or "the initial user")
    print(_c("1;34", f"\n== Provider password for {where}"), file=sys.stderr)
    print("The password the VPS provider issued for that login, used once to "
          "install\nyour SSH key. Leave blank if the key is already installed.",
          file=sys.stderr)
    return getpass.getpass("Provider password (blank to skip): ")


def _bootstrap_extra_vars(
    inv_dir: Path, input_path: str | None, *, prompt_password: bool = False,
) -> tuple[list[str], Path | None]:
    """bootstrap.yml collects bootstrap_initial_user + bootstrap_root_password
    via a play-scoped vars_prompt, which outranks any inventory hostvar --
    with no TTY it silently takes its own default ("root"), never the real
    provider user. Extra-vars (-e) are the only thing that outranks
    vars_prompt, so both are injected there.
    bootstrap_initial_user: install.yaml's host.initial_user when given --
    `catena recover` reuses the OLD inventory's .env, whose HOST_INITIAL_USER
    reflects the box that died, not necessarily the fresh replacement (a
    different provider/image can mean a different login) -- else the
    inventory's own .env (HOST_INITIAL_USER; on disk by now regardless of
    install.yaml, since seed.py already wrote or confirmed it for a normal
    install). bootstrap_root_password comes from install.yaml when given,
    else from the up-front prompt (`prompt_password`, interactive callers
    only); blank is meaningful either way -- Phase 0.5 then skips key
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
            _prompt_provider_password(inv_dir, initial_user)
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
    """Install the Galaxy collections (community.general, community.docker, ...)
    on first run if they are not already present. Honors
    ANSIBLE_COLLECTIONS_PATH: when set (first path wins), install there instead
    of the in-tree dir -- so the CLI can run from a READ-ONLY checkout (e.g.
    driven from a CI runner against a :ro catena-ce mount) by pointing
    at a writable location, which ansible then also reads.

    The in-tree default MUST match ansible.cfg's collections_path. It did not
    for a release: the install landed in collections/ while ansible read
    .collections/, so a fresh checkout installed a tree nothing loaded and fell
    through to whatever collections the controller happened to have."""
    req = ANSIBLE_DIR / "requirements.yml"
    if not req.is_file():
        return
    override = os.environ.get("ANSIBLE_COLLECTIONS_PATH", "").strip()
    coll = Path(override.split(os.pathsep)[0]).expanduser() if override else ANSIBLE_DIR / COLLECTIONS_DIR
    if coll.is_dir():
        return
    banner("Installing Ansible collections (first run)")
    _run([
        "ansible-galaxy", "collection", "install",
        "-r", str(req), "-p", str(coll),
    ])


def _preflight_checks(binaries: tuple[str, ...] = REQUIRED_BINARIES) -> None:
    missing = check_prereqs(binaries)
    if missing:
        die(
            "missing required tools on PATH: " + ", ".join(missing) + "\n"
            "  - ansible-core: run this via `uv run catena ...`, or "
            "`pipx install ansible-core`"
        )


def _require_inventory(inv_dir: Path) -> None:
    if not inv_dir.is_dir():
        die(
            f"inventory not found at {inv_dir}. Copy inventory/example/ "
            f"there, fill in .env and hosts.yml, then run "
            f"`catena install --inventory {inv_dir.name}` first."
        )


def _run_deploy_chain(
    inv_dir: Path,
    chain: tuple[str, ...],
    *,
    bootstrap_extra: list[str] | None,
    restore_extra: list[str] | None = None,
    global_extra: list[str] | None = None,
) -> None:
    """Run an ordered deploy chain (install or recover) stage by stage,
    threading the bootstrap creds onto the bootstrap stage, (recover only) the
    restore snapshot onto the restore stage, and `global_extra` onto EVERY
    stage. `global_extra` is the transient secret-adopt file (`-e @file`) that
    carries the install-critical vendor creds / DR keyset into each play so the
    on-box loader adopts them -- no persisted laptop vault (0b). Plus the one
    inter-stage bridge a deploy needs:

      - after `bootstrap`: fold the steady-state tailnet IP that bootstrap
        emitted into .bootstrap-output.yml back into hosts.yml, so the later
        stages (each a separate ansible invocation) reach the host instead of
        the 0.0.0.0 placeholder.

    The Portainer API key that the auth stack (Keycloak, oauth2-proxy) is
    gated on is minted in-band by roles/portainer during `site` (it mints
    from the initial admin, reloads the vault into play scope via
    include_vars, and self-heals a missing/rejected key on every converge),
    so a single `site` pass deploys everything -- no second pass.

    Shared by install + recover so the bridge logic lives in one place."""
    for stage in chain:
        banner(f"Stage: {stage}")
        if stage == "bootstrap":
            extra = list(bootstrap_extra or [])
        elif stage == "restore":
            extra = list(restore_extra or [])
        else:
            extra = []
        if global_extra:
            extra = extra + global_extra
        _run(playbook_cmd(inv_dir, stage, extra or None))
        if stage == "bootstrap":
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


def _show_dr_keyset(inv_dir: Path) -> None:
    """After a fresh install, surface the on-box-minted DR keyset (admin +
    restic passwords) ONCE for the user's password manager. Non-fatal: a
    failure here must never fail an otherwise-successful install (the keyset
    is always retrievable later from catena-admin > Recovery)."""
    banner("Your disaster-recovery keyset -- shown once, save it now")
    cmd = playbook_cmd(inv_dir, "show-keyset")
    print(_c("1;30", "  $ " + " ".join(cmd)), file=sys.stderr)
    if subprocess.run(cmd).returncode != 0:
        print(_c("1;33", "! could not display the DR keyset automatically; "
                 "retrieve it later from catena-admin > Recovery."),
              file=sys.stderr)


def cmd_install(args: argparse.Namespace) -> int:
    _preflight_checks()

    # The transient adopt file seed writes the vendor creds into (never a
    # persisted inventory vault). Threaded onto every deploy stage as `-e @file`
    # so the on-box loader adopts them, then deleted.
    secrets_tmp = _mktemp_secrets("catena-install-secrets-")

    banner("Step 1/2 -- collect config + vendor creds (seed)")
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
        # The last question of the run. Everything the deploy chain needs is
        # answered before the first playbook starts, so nothing stops to ask
        # once it is under way.
        bootstrap_extra, bootstrap_vars_tmp = _bootstrap_extra_vars(
            inv_dir, args.input, prompt_password=not args.no_confirm)

        ensure_collections()

        banner("Step 2/2 -- deploy (preflight -> bootstrap -> site -> validate)")
        _run_deploy_chain(inv_dir, INSTALL_CHAIN,
                          bootstrap_extra=bootstrap_extra, global_extra=adopt_extra)
        _show_dr_keyset(inv_dir)
    finally:
        if bootstrap_vars_tmp is not None:
            bootstrap_vars_tmp.unlink(missing_ok=True)
        secrets_tmp.unlink(missing_ok=True)
    banner("Install complete.")
    return 0


# The user-held DR keyset re-entered at recover time: the box is wiped, so
# nothing is read from a persisted laptop vault (0b).
#
# (key, label, hidden?, needs_tailnet?). The last field is the one that is not
# about secrecy: a host whose access method is public SSH joins no tailnet, so
# asking for the credential that joins one is asking for a value this recover
# would store and nothing would read. The answer comes from the inventory rather
# than the store, because on a recover there is no store yet -- that is what is
# being rebuilt.
DR_ADOPT_SECRETS = (
    ("backup_restic_password", "Restic backup password", True, False),
    ("backup_s3_access_key", "S3 access key", True, False),
    ("backup_s3_secret_key", "S3 secret key", True, False),
    ("cloudflare_api_token", "Cloudflare API token", True, False),
    ("tailscale_oauth_client_id", "Tailscale OAuth client id", False, True),
    ("tailscale_oauth_client_secret", "Tailscale OAuth client secret", True, True),
    ("headscale_api_key", "Headscale API key", True, True),
    ("headscale_preauth_key", "Headscale pre-authentication key", True, True),
)


def _recover_access_method(inv_dir: Path, provided_env: dict) -> str:
    """Which access method the host being rebuilt declared.

    From the answers file when one was given, else from the inventory `.env`
    that seeded the store in the first place. Defaults to `tailnet`, which is
    what a host installed before the key existed is on -- and the safer guess:
    it asks for a credential that may go unused rather than skipping one the
    bootstrap cannot continue without.
    """
    declared = str(provided_env.get("ACCESS_METHOD", "") or "").strip()
    if not declared:
        env_path = inv_dir / ".env"
        if env_path.is_file():
            import seed  # shares ANSIBLE_DIR on sys.path
            declared = str(
                seed.read_existing_env(env_path).get("ACCESS_METHOD", "") or ""
            ).strip()
    return declared or "tailnet"


def _collect_dr_adopt_file(
    input_path: str | None, inv_dir: Path,
) -> tuple[list[str], Path | None]:
    """Collect the DR keyset for a fresh-box recover into a 0600 temp adopt
    file (`-e @file`), threaded onto every recover stage so `restore` can
    decrypt the backup and the on-box loader adopts the creds. Values come from
    --input install.yaml when present, else an interactive prompt; the restic
    repo URL rides as `backup_restic_repo`. Returns ([], None) when nothing was
    collected. Mirrors _bootstrap_extra_vars: never on argv, cleaned by caller."""
    provided_vault: dict = {}
    provided_env: dict = {}
    if input_path:
        import seed  # shares ANSIBLE_DIR on sys.path
        inp = seed.load_input(Path(input_path))
        provided_vault = inp.get("vault") or {}
        provided_env = inp.get("env") or {}

    on_tailnet = _recover_access_method(inv_dir, provided_env) == "tailnet"
    values: dict[str, str] = {}
    for key, label, hidden, needs_tailnet in DR_ADOPT_SECRETS:
        if needs_tailnet and not on_tailnet:
            continue
        val = str(provided_vault.get(key, "") or "").strip()
        if not val and sys.stdin.isatty():
            val = (getpass.getpass(f"{label}: ") if hidden
                   else input(f"{label}: ")).strip()
        if val:
            values[key] = val
    repo = str(provided_env.get("BACKUP_RESTIC_REPO", "") or "").strip()
    if not repo and sys.stdin.isatty():
        repo = input("Restic repo URL (s3:<endpoint>/<bucket>): ").strip()
    if repo:
        values["backup_restic_repo"] = repo

    if not values:
        return [], None
    import yaml
    tmp = _mktemp_secrets("catena-recover-secrets-")
    tmp.write_text(yaml.safe_dump(values, default_flow_style=False))
    tmp.chmod(0o600)
    return ["-e", f"@{tmp}"], tmp


def cmd_recover(args: argparse.Namespace) -> int:
    """Whole-host disaster recovery onto a FRESH replacement box: preflight ->
    bootstrap -> restore -> site -> validate. The provider creds come from
    --input install.yaml (0600 temp file, never on argv). The DR keyset (restic
    password + S3 keys + repo URL + Cloudflare/Tailscale creds) is re-entered
    here -- prompted or from --input -- since nothing persists on the laptop;
    it is threaded onto every stage so restore can decrypt the backup.
    --snapshot picks the restic snapshot to push (default: latest)."""
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()

    banner("Recover -- preflight -> bootstrap -> restore -> site -> validate")
    # Same rule as install: the replacement box's provider password is asked
    # for here, alongside the DR keyset, not by bootstrap.yml mid-chain.
    bootstrap_extra, bootstrap_vars_tmp = _bootstrap_extra_vars(
        inv_dir, args.input, prompt_password=True)
    restore_extra = _snapshot_extra(args)
    dr_extra, dr_tmp = _collect_dr_adopt_file(args.input, inv_dir)
    try:
        _run_deploy_chain(
            inv_dir, RECOVER_CHAIN,
            bootstrap_extra=bootstrap_extra, restore_extra=restore_extra,
            global_extra=dr_extra or None,
        )
    finally:
        if bootstrap_vars_tmp is not None:
            bootstrap_vars_tmp.unlink(missing_ok=True)
        if dr_tmp is not None:
            dr_tmp.unlink(missing_ok=True)
    banner("Recovery complete.")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    """Roll a still-running host back to a prior restic snapshot: preflight ->
    restore -> site -> validate, in place (no bootstrap). Reuses the existing
    inventory's vault. --snapshot picks the restic snapshot to roll back to
    (default: latest). The `site` leg is load-bearing for password coherence
    (pg_password_reconcile) -- never dropped."""
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()

    banner("Rollback -- preflight -> restore -> site -> validate")
    _run_deploy_chain(
        inv_dir, ROLLBACK_CHAIN,
        bootstrap_extra=None, restore_extra=_snapshot_extra(args),
    )
    banner("Rollback complete.")
    return 0


def cmd_converge(args: argparse.Namespace) -> int:
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()
    _run(playbook_cmd(inv_dir, "converge", _tags_extra(args)))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()
    _run(playbook_cmd(inv_dir, "validate", _tags_extra(args)))
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Trigger an on-demand backup snapshot: run catena-backup.service
    synchronously via the backup role's trigger_now (the same mechanism the
    admin shell "Backup now" button drives). CE ships a single, manual
    backup -- this is its CLI entry point. No preflight (mints no keys)."""
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()
    banner("Backup -- trigger an on-demand snapshot")
    _run(playbook_cmd(inv_dir, "backup"))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()
    _run(playbook_cmd(inv_dir, "restore", _snapshot_extra(args)))
    return 0


def cmd_rotate_tunnel(args: argparse.Namespace) -> int:
    """Regenerate this host's Cloudflare tunnel without a full converge
    (manual maintenance).

    Takes no token. The playbook authenticates as the cloudflare_api_token
    already in the host's store, because the half that mints the replacement
    tunnel is a host engine that reads that store directly -- a token handed in
    here would reach the delete and not the re-create. Rotating the API token
    is a separate act, done in catena-admin > Settings, and it has to land
    before this runs."""
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()
    banner("Rotate -- regenerate the Cloudflare tunnel")
    _run(playbook_cmd(inv_dir, "rotate-tunnel"))
    return 0


def cmd_rotate_tailscale(args: argparse.Namespace) -> int:
    """Re-authenticate the node to the tailnet, forcing a fresh OAuth key
    (manual maintenance: node lost tailnet reachability or you are rotating
    tags). The tailscale role's tailscale_force_reauth is set by the
    playbook itself, so this just runs it."""
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()
    banner("Rotate -- re-authenticate Tailscale")
    _run(playbook_cmd(inv_dir, "rotate-tailscale"))
    return 0


def cmd_show_keyset(args: argparse.Namespace) -> int:
    """Re-display the DR keyset and the first-login URLs.

    The install shows these once, at the very end. An install that fails
    anywhere before that point -- or a closed terminal -- leaves a working
    server nobody can sign in to, which is a worse state than a failed
    install. The values are all still on the box, so asking for them again
    costs nothing and re-runs nothing.

    The journal verification key is the one exception: it is deleted from the
    server the first time it is shown, by design, so a later call has nothing
    to print for it.
    """
    _preflight_checks()
    inv_dir = resolve_inventory(args)
    _require_inventory(inv_dir)
    ensure_collections()
    _show_dr_keyset(inv_dir)
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
    ("install", "Set up a new host (seed + deploy)"),
    ("converge", "Re-apply configuration or app changes"),
    ("validate", "On-host + tailnet + external health checks"),
    ("backup", "Take an on-demand backup snapshot"),
    ("restore", "In-place whole-host restore"),
    ("recover", "Rebuild onto a fresh replacement box"),
    ("rollback", "Roll a running host back to a snapshot"),
    ("rotate-tunnel", "Regenerate the Cloudflare tunnel"),
    ("rotate-tailscale", "Re-authenticate the node to the tailnet"),
    ("show-keyset", "Show the passwords and first-login URLs again"),
    ("uninstall", "Hand unattended-upgrades back to the OS"),
)
KNOWN_COMMANDS = tuple(name for name, _ in MENU_COMMANDS)


def _choose_command() -> str:
    """Print the numbered menu and return the chosen subcommand name."""
    print(_c("1;34", "catena -- choose an operation:"), file=sys.stderr)
    for i, (name, desc) in enumerate(MENU_COMMANDS, 1):
        print(f"  {i:>2}) {name:<18} {desc}", file=sys.stderr)
    choice = input("\nNumber [1=install]: ").strip() or "1"
    try:
        return MENU_COMMANDS[int(choice) - 1][0]
    except (ValueError, IndexError):
        die(f"not a valid choice: {choice!r}")


def interactive_menu() -> list[str]:
    """Bare `catena` (no args): prompt for inventory, then the operation, and
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
        f"comes first: `catena {rest or '<verb>'} --inventory {argv[0]}`. "
        f"Commands: {', '.join(KNOWN_COMMANDS)}.", code=2)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="catena",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Not required: bare `catena` drops into interactive_menu() instead of
    # erroring, so a self-hoster can discover the subcommands.
    sub = ap.add_subparsers(dest="command", required=False)

    p_install = sub.add_parser("install", help="seed + preflight/bootstrap/site/validate")
    _add_inventory_args(p_install, required=False)
    p_install.add_argument("-i", "--input", help="install.yaml for non-interactive values")
    p_install.add_argument("--no-confirm", action="store_true",
                           help="skip seed confirmation + one-shot secret prompts")
    p_install.set_defaults(func=cmd_install)

    p_conv = sub.add_parser("converge", help="re-run converge.yml")
    _add_inventory_args(p_conv, required=True)
    p_conv.add_argument("--tags", default="",
                        help="comma-separated ansible tags to scope the "
                             "converge (e.g. keycloak,oauth2_proxy)")
    p_conv.set_defaults(func=cmd_converge)

    p_val = sub.add_parser("validate", help="run validate.yml")
    _add_inventory_args(p_val, required=True)
    p_val.add_argument("--tags", default="",
                       help="comma-separated ansible tags to scope validation")
    p_val.set_defaults(func=cmd_validate)

    p_bak = sub.add_parser(
        "backup", help="trigger an on-demand snapshot (manual CE backup)",
    )
    _add_inventory_args(p_bak, required=True)
    p_bak.set_defaults(func=cmd_backup)

    p_res = sub.add_parser("restore", help="run restore.yml (in-place restore)")
    _add_inventory_args(p_res, required=True)
    p_res.add_argument("--snapshot", default="",
                       help="restic snapshot id to restore (default: latest)")
    p_res.set_defaults(func=cmd_restore)

    p_rec = sub.add_parser(
        "recover",
        help="whole-host DR onto a fresh replacement box "
             "(bootstrap -> restore -> site -> validate)",
    )
    _add_inventory_args(p_rec, required=True)
    p_rec.add_argument("-i", "--input",
                       help="install.yaml supplying the replacement box's "
                            "provider user + password for bootstrap")
    p_rec.add_argument("--snapshot", default="",
                       help="restic snapshot id to restore (default: latest)")
    p_rec.set_defaults(func=cmd_recover)

    p_rb = sub.add_parser(
        "rollback",
        help="roll a running host back to a prior snapshot "
             "(restore -> site -> validate, in place)",
    )
    _add_inventory_args(p_rb, required=True)
    p_rb.add_argument("--snapshot", default="",
                      help="restic snapshot id to roll back to (default: latest)")
    p_rb.set_defaults(func=cmd_rollback)

    p_rtun = sub.add_parser(
        "rotate-tunnel",
        help="regenerate the Cloudflare tunnel (manual; uses the stored token)",
    )
    _add_inventory_args(p_rtun, required=True)
    p_rtun.set_defaults(func=cmd_rotate_tunnel)

    p_rts = sub.add_parser(
        "rotate-tailscale",
        help="re-authenticate the node to the tailnet (manual force re-auth)",
    )
    _add_inventory_args(p_rts, required=True)
    p_rts.set_defaults(func=cmd_rotate_tailscale)

    p_keys = sub.add_parser(
        "show-keyset",
        help="re-display the DR keyset + first-login URLs (reads the box; "
             "runs no converge)",
    )
    _add_inventory_args(p_keys, required=True)
    p_keys.set_defaults(func=cmd_show_keyset)

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
    # `--install` is an alias for the `install` subcommand (so `catena
    # --install` == `catena install`); rewrite it to the positional form and
    # let install's own parser handle any trailing flags.
    if argv and argv[0] == "--install":
        argv = ["install", *argv[1:]]
    # Bare `catena` (no args): interactive menu, when a TTY is present.
    if not argv:
        if not sys.stdin.isatty():
            die("no subcommand given (and no TTY for the menu). "
                "Try `catena install --inventory <name>` or `catena --help`.",
                code=2)
        argv = interactive_menu()
    else:
        _reject_bare_inventory(argv)
    args = build_parser().parse_args(argv)
    return args.func(args)


def _entry(argv: list[str] | None = None) -> int:
    """Entry point for the `catena` console script; keeps the Ctrl-C handling
    in one place."""
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("\nCancelled (interrupted).", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(_entry())
