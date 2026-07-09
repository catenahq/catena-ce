"""Unit tests for the `catena` CLI wrapper: command wiring."""
from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ANSIBLE_DIR = Path(__file__).resolve().parents[2]
CATENA_PATH = ANSIBLE_DIR / "catena"


@pytest.fixture(scope="module")
def cli():
    # `catena` has no .py extension; load it explicitly via SourceFileLoader.
    loader = SourceFileLoader("catena_cli", str(CATENA_PATH))
    spec = importlib.util.spec_from_loader("catena_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_install_chain_order(cli):
    """Fresh install runs preflight before bootstrap, then site, then validate."""
    assert cli.INSTALL_CHAIN == ("preflight", "bootstrap", "site", "validate")


def test_recover_chain_order(cli):
    """DR onto a fresh box runs preflight -> bootstrap -> restore -> site ->
    validate: the install chain with `restore` inserted after bootstrap."""
    assert cli.RECOVER_CHAIN == (
        "preflight", "bootstrap", "restore", "site", "validate",
    )


def test_recover_parser_wires_snapshot_and_input(cli):
    ns = cli.build_parser().parse_args(
        ["recover", "--inventory", "test", "--snapshot", "snap42",
         "-i", "install.yaml"]
    )
    assert ns.func is cli.cmd_recover
    assert ns.snapshot == "snap42"
    assert ns.input == "install.yaml"


def _stage_of(cmd):
    """The playbook stem of an ansible-playbook argv (the .yml arg)."""
    pb = next(a for a in cmd if a.endswith(".yml"))
    return pb.rsplit("/", 1)[-1].removesuffix(".yml")


def test_recover_runs_full_chain_with_snapshot(cli, monkeypatch):
    """cmd_recover drives the whole DR chain in order, threads the snapshot
    onto the restore stage, and (reused vault holds the key) makes a single
    site pass -- no seed."""
    from helpers import bootstrap_output

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setattr(bootstrap_output, "apply_to_inventory", lambda p: [])

    ns = cli.build_parser().parse_args(
        ["recover", "--inventory", "test", "--snapshot", "snap42"]
    )
    assert ns.func(ns) == 0

    pb_calls = [c for c in calls if c and c[0] == "ansible-playbook"]
    assert [_stage_of(c) for c in pb_calls] == [
        "preflight", "bootstrap", "restore", "site", "validate",
    ]
    restore_cmd = next(c for c in pb_calls if _stage_of(c) == "restore")
    assert "restore_snapshot=snap42" in " ".join(restore_cmd)


def test_rollback_chain_order(cli):
    """In-place rollback runs preflight -> restore -> site -> validate, no
    bootstrap (the host is alive)."""
    assert cli.ROLLBACK_CHAIN == ("preflight", "restore", "site", "validate")


def test_rollback_parser_wires_snapshot(cli):
    ns = cli.build_parser().parse_args(
        ["rollback", "--inventory", "test", "--snapshot", "snap7"]
    )
    assert ns.func is cli.cmd_rollback
    assert ns.snapshot == "snap7"


def test_rollback_runs_chain_with_snapshot_no_bootstrap(cli, monkeypatch):
    """cmd_rollback drives restore -> site -> validate (preflight first), threads
    the snapshot onto restore, and never runs bootstrap."""
    from helpers import bootstrap_output

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setattr(bootstrap_output, "apply_to_inventory", lambda p: [])

    ns = cli.build_parser().parse_args(
        ["rollback", "--inventory", "test", "--snapshot", "snap7"]
    )
    assert ns.func(ns) == 0

    pb_calls = [c for c in calls if c and c[0] == "ansible-playbook"]
    stages = [_stage_of(c) for c in pb_calls]
    assert stages == ["preflight", "restore", "site", "validate"]
    assert "bootstrap" not in stages
    restore_cmd = next(c for c in pb_calls if _stage_of(c) == "restore")
    assert "restore_snapshot=snap7" in " ".join(restore_cmd)


def test_recover_runs_single_site_pass(cli, monkeypatch):
    """Post-Portainer-migration there is no CLI-driven second site pass: the
    Portainer API key the auth stack needs is minted in-band by
    roles/portainer during `site`, so the chain runs `site` exactly once."""
    from helpers import bootstrap_output

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setattr(bootstrap_output, "apply_to_inventory", lambda p: [])

    ns = cli.build_parser().parse_args(["recover", "--inventory", "test"])
    assert ns.func(ns) == 0

    stages = [_stage_of(c) for c in calls if c and c[0] == "ansible-playbook"]
    assert stages == ["preflight", "bootstrap", "restore", "site", "validate"]
    assert stages.count("site") == 1


def test_playbook_cmd_shape(cli):
    cmd = cli.playbook_cmd("prod", "site")
    assert cmd[0] == "ansible-playbook"
    assert "-i" in cmd
    inv = cmd[cmd.index("-i") + 1]
    assert inv.endswith("inventory/prod")
    assert cmd[-1].endswith("playbooks/site.yml")


def test_playbook_cmd_extra_args(cli):
    cmd = cli.playbook_cmd("prod", "bootstrap", ["--limit", "prod1-bootstrap"])
    assert cmd[-2:] == ["--limit", "prod1-bootstrap"]


def test_converge_accepts_tags_passthrough(cli):
    """`catena converge --tags a,b` scopes the converge to those roles
    (e.g. re-apply only keycloak,oauth2_proxy after rotating a secret)."""
    ns = cli.build_parser().parse_args(
        ["converge", "--inventory", "test", "--tags", "keycloak,oauth2_proxy"]
    )
    assert cli._tags_extra(ns) == ["--tags", "keycloak,oauth2_proxy"]
    cmd = cli.playbook_cmd(ns.inventory, "site", cli._tags_extra(ns))
    assert cmd[-2:] == ["--tags", "keycloak,oauth2_proxy"]
    assert cmd[-3].endswith("playbooks/site.yml")


def test_tags_extra_is_none_when_unset(cli):
    """No --tags -> no passthrough (a full converge / validate). validate
    accepts the same flag."""
    conv = cli.build_parser().parse_args(["converge", "--inventory", "test"])
    assert cli._tags_extra(conv) is None
    val = cli.build_parser().parse_args(
        ["validate", "--inventory", "test", "--tags", "keycloak"]
    )
    assert cli._tags_extra(val) == ["--tags", "keycloak"]


def test_backup_parser_wires_backup_now(cli):
    """`catena backup` runs the backup_now playbook (the manual CE snapshot)."""
    ns = cli.build_parser().parse_args(["backup", "--inventory", "test"])
    assert ns.func is cli.cmd_backup
    cmd = cli.playbook_cmd(ns.inventory, "backup_now")
    assert cmd[-1].endswith("playbooks/backup_now.yml")


def test_backup_runs_backup_now_playbook(cli, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    ns = cli.build_parser().parse_args(["backup", "--inventory", "test"])
    assert ns.func(ns) == 0
    assert len(calls) == 1
    assert calls[0][-1].endswith("playbooks/backup_now.yml")


def test_restore_accepts_snapshot_passthrough(cli):
    """`catena restore --snapshot <id>` restores a specific restic snapshot
    via restore.yml's restore_snapshot extra-var."""
    ns = cli.build_parser().parse_args(
        ["restore", "--inventory", "test", "--snapshot", "abc123"]
    )
    assert cli._snapshot_extra(ns) == ["-e", "restore_snapshot=abc123"]
    cmd = cli.playbook_cmd(ns.inventory, "restore", cli._snapshot_extra(ns))
    assert cmd[-2:] == ["-e", "restore_snapshot=abc123"]
    assert cmd[-3].endswith("playbooks/restore.yml")


def test_snapshot_extra_is_none_when_unset(cli):
    """No --snapshot -> restore.yml defaults to latest."""
    ns = cli.build_parser().parse_args(["restore", "--inventory", "test"])
    assert cli._snapshot_extra(ns) is None


def test_check_prereqs_reports_missing(cli, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    missing = cli.check_prereqs(("ansible-playbook", "ansible"))
    assert set(missing) == {"ansible-playbook", "ansible"}


def test_check_prereqs_all_present(cli, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    assert cli.check_prereqs(("ansible",)) == []


def test_required_binaries_drop_sops_and_age(cli):
    """SOPS+age was dropped (0b): the vault is plaintext, so neither sops nor
    age-keygen is a prereq, and there is no seed-only binary set anymore."""
    assert cli.REQUIRED_BINARIES == ("ansible-playbook", "ansible")
    assert "sops" not in cli.REQUIRED_BINARIES
    assert "age-keygen" not in cli.REQUIRED_BINARIES
    assert not hasattr(cli, "_SEED_BINARIES")
    assert not hasattr(cli, "ensure_age_key_env")


def test_preflight_passes_with_core_binaries(cli, monkeypatch):
    """Every command runs against a plaintext inventory; only ansible is
    required."""
    present = set(cli.REQUIRED_BINARIES)
    monkeypatch.setattr(
        cli.shutil, "which",
        lambda name: ("/usr/bin/" + name) if name in present else None,
    )
    cli._preflight_checks()  # must NOT raise


def test_ensure_collections_uses_writable_override_path(cli, monkeypatch, tmp_path):
    """On a read-only checkout, ANSIBLE_COLLECTIONS_PATH redirects the galaxy
    install to a writable dir (ansible reads the same var), so the CLI can run
    without writing into the :ro checkout's collections/ dir."""
    target = tmp_path / "colls"  # absent -> triggers install
    monkeypatch.setenv("ANSIBLE_COLLECTIONS_PATH", str(target))
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    cli.ensure_collections()
    assert len(calls) == 1, calls
    cmd = calls[0]
    assert cmd[:3] == ["ansible-galaxy", "collection", "install"]
    assert cmd[cmd.index("-p") + 1] == str(target)


def test_ensure_collections_skips_when_override_dir_exists(cli, monkeypatch, tmp_path):
    existing = tmp_path / "colls"
    existing.mkdir()
    monkeypatch.setenv("ANSIBLE_COLLECTIONS_PATH", str(existing))
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    cli.ensure_collections()
    assert calls == []


def test_bootstrap_extra_vars_writes_secret_to_file_not_argv(cli, tmp_path):
    import yaml

    iy = tmp_path / "install.yaml"
    iy.write_text(
        "inventory: test\n"
        "host_initial_user: debian\n"
        "host_initial_password: s3cr3t-provider-pw\n"
    )
    extra, tmp = cli._bootstrap_extra_vars(str(iy))
    try:
        # The provider password is referenced as -e @file, never inline on
        # argv (would otherwise leak via `ps` and the printed command).
        assert extra[0] == "-e"
        assert extra[1].startswith("@")
        assert "s3cr3t-provider-pw" not in " ".join(extra)
        data = yaml.safe_load(tmp.read_text())
        assert data["bootstrap_initial_user"] == "debian"
        assert data["bootstrap_root_password"] == "s3cr3t-provider-pw"
        # 0600 so the provider password is not world-readable on disk.
        assert (tmp.stat().st_mode & 0o777) == 0o600
    finally:
        tmp.unlink(missing_ok=True)


def test_bootstrap_extra_vars_noop_without_install_yaml(cli):
    extra, tmp = cli._bootstrap_extra_vars(None)
    assert extra == []
    assert tmp is None


def test_rotate_tunnel_parser_wires_token(cli):
    ns = cli.build_parser().parse_args(
        ["rotate-tunnel", "--inventory", "test", "--cf-api-token", "cf-tok"]
    )
    assert ns.func is cli.cmd_rotate_tunnel
    assert ns.cf_api_token == "cf-tok"


def test_rotate_tailscale_parser_wires_playbook(cli):
    ns = cli.build_parser().parse_args(["rotate-tailscale", "--inventory", "test"])
    assert ns.func is cli.cmd_rotate_tailscale
    cmd = cli.playbook_cmd(ns.inventory, "rotate-tailscale")
    assert cmd[-1].endswith("playbooks/rotate-tailscale.yml")


def test_secret_extra_var_writes_secret_to_file_not_argv(cli):
    import yaml

    extra, tmp = cli._secret_extra_var("cf_api_token", "super-secret-token")
    try:
        # Referenced as -e @file; the secret never lands inline on argv.
        assert extra[0] == "-e"
        assert extra[1].startswith("@")
        assert "super-secret-token" not in " ".join(extra)
        data = yaml.safe_load(tmp.read_text())
        assert data["cf_api_token"] == "super-secret-token"
        assert (tmp.stat().st_mode & 0o777) == 0o600
    finally:
        tmp.unlink(missing_ok=True)


def test_secret_extra_var_noop_on_empty(cli):
    extra, tmp = cli._secret_extra_var("cf_api_token", "")
    assert extra == []
    assert tmp is None


def test_rotate_tunnel_runs_playbook_with_token_file(cli, monkeypatch):
    """cmd_rotate_tunnel runs the regenerate-cf-tunnel playbook, passing the
    token via -e @file (never inline), and cleans the temp file after."""
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)

    ns = cli.build_parser().parse_args(
        ["rotate-tunnel", "--inventory", "test", "--cf-api-token", "cf-tok"]
    )
    assert ns.func(ns) == 0
    assert len(calls) == 1
    joined = " ".join(calls[0])
    assert calls[0][-2] == "-e"
    assert calls[0][-1].startswith("@")
    assert "cf-tok" not in joined
    assert _stage_of(calls[0]) == "regenerate-cf-tunnel"


def test_rotate_tunnel_reads_token_from_env(cli, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setenv("CATENA_CF_API_TOKEN", "env-token")

    ns = cli.build_parser().parse_args(["rotate-tunnel", "--inventory", "test"])
    assert ns.func(ns) == 0
    assert len(calls) == 1
    assert _stage_of(calls[0]) == "regenerate-cf-tunnel"


def test_rotate_tunnel_dies_without_token(cli, monkeypatch):
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.delenv("CATENA_CF_API_TOKEN", raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "")

    ns = cli.build_parser().parse_args(["rotate-tunnel", "--inventory", "test"])
    with pytest.raises(SystemExit):
        ns.func(ns)


def test_rotate_tailscale_runs_playbook(cli, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)

    ns = cli.build_parser().parse_args(["rotate-tailscale", "--inventory", "test"])
    assert ns.func(ns) == 0
    assert len(calls) == 1
    assert _stage_of(calls[0]) == "rotate-tailscale"


# ---- --inventory-path: drive an inventory OUTSIDE the checkout ----
# (an operator pointing the CLI at an ops-side inventory from Semaphore).

def test_resolve_inventory_name_resolves_under_checkout(cli):
    ns = cli.build_parser().parse_args(["converge", "--inventory", "prod"])
    inv = cli.resolve_inventory(ns)
    assert inv == cli.inventory_path("prod")
    assert str(inv).endswith("inventory/prod")


def test_resolve_inventory_path_is_used_verbatim(cli, tmp_path):
    ext = tmp_path / "ops" / "inventory" / "clientA"
    ns = cli.build_parser().parse_args(["converge", "--inventory-path", str(ext)])
    assert cli.resolve_inventory(ns) == ext


def test_playbook_cmd_accepts_a_path_directly(cli, tmp_path):
    from pathlib import Path

    ext = Path(tmp_path) / "clientA"
    cmd = cli.playbook_cmd(ext, "site")
    assert cmd[cmd.index("-i") + 1] == str(ext)
    assert cmd[-1].endswith("playbooks/site.yml")


def test_inventory_path_threads_to_ansible_playbook_i_flag(cli, monkeypatch, tmp_path):
    """A --inventory-path run passes that exact dir to `ansible-playbook -i` on
    every stage -- so the operator's ops-side inventory is what ansible reads."""
    from helpers import bootstrap_output

    ext = tmp_path / "ops-inv" / "clientA"
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setattr(bootstrap_output, "apply_to_inventory", lambda p: [])

    ns = cli.build_parser().parse_args(["rollback", "--inventory-path", str(ext)])
    assert ns.func(ns) == 0
    pb_calls = [c for c in calls if c and c[0] == "ansible-playbook"]
    assert pb_calls
    for c in pb_calls:
        assert c[c.index("-i") + 1] == str(ext)


def test_inventory_and_path_are_mutually_exclusive(cli):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["converge", "--inventory", "prod", "--inventory-path", "/tmp/x"]
        )


def test_inventory_required_for_non_install_commands(cli):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["converge"])


def test_install_accepts_inventory_path_and_skips_the_name(cli):
    ns = cli.build_parser().parse_args(["install", "--inventory-path", "/tmp/x"])
    assert ns.func is cli.cmd_install
    assert ns.inventory_path == "/tmp/x"
    assert ns.inventory is None
