"""Unit tests for the `catena` CLI wrapper: command wiring + key bridging."""
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
    monkeypatch.setattr(cli, "ensure_age_key_env", lambda *a, **k: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_key", lambda inv: False)
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
    monkeypatch.setattr(cli, "ensure_age_key_env", lambda *a, **k: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    monkeypatch.setattr(cli, "_ensure_dokploy_api_key", lambda inv: False)
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


def test_recover_second_site_pass_when_vault_lacks_key(cli, monkeypatch):
    """If the reused vault somehow lacks the Dokploy key (key was rotated out
    / fresh repo), recover still mints + runs the second site pass -- same
    bridge as install."""
    from helpers import bootstrap_output

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "_preflight_checks", lambda: None)
    monkeypatch.setattr(cli, "_require_inventory", lambda inv: None)
    monkeypatch.setattr(cli, "ensure_age_key_env", lambda *a, **k: None)
    monkeypatch.setattr(cli, "ensure_collections", lambda: None)
    # First site pass -> key minted -> second pass requested.
    monkeypatch.setattr(cli, "_ensure_dokploy_api_key", lambda inv: True)
    monkeypatch.setattr(bootstrap_output, "apply_to_inventory", lambda p: [])

    ns = cli.build_parser().parse_args(["recover", "--inventory", "test"])
    assert ns.func(ns) == 0

    stages = [_stage_of(c) for c in calls if c and c[0] == "ansible-playbook"]
    assert stages == [
        "preflight", "bootstrap", "restore", "site", "site", "validate",
    ]


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
    monkeypatch.setattr(cli, "ensure_age_key_env", lambda *a, **k: None)
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
    missing = cli.check_prereqs(("ansible-playbook", "sops"))
    assert set(missing) == {"ansible-playbook", "sops"}


def test_check_prereqs_all_present(cli, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    assert cli.check_prereqs(("sops",)) == []


def test_ensure_age_key_env_loads_from_file(cli, tmp_path, monkeypatch):
    monkeypatch.delenv("SOPS_AGE_KEY", raising=False)
    key_file = tmp_path / "keys.txt"
    secret = "AGE-SECRET-KEY-1LOADEDFROMFILE000000000000000000000000000000000000"
    key_file.write_text(f"# public key: age1x\n{secret}\n")
    cli.ensure_age_key_env(key_file=key_file)
    import os
    assert os.environ.get("SOPS_AGE_KEY") == secret


def test_ensure_age_key_env_keeps_existing(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("SOPS_AGE_KEY", "AGE-SECRET-KEY-1ALREADYSET00000000000000000000000000000000000000")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("AGE-SECRET-KEY-1OTHER0000000000000000000000000000000000000000000\n")
    cli.ensure_age_key_env(key_file=key_file)
    import os
    assert os.environ["SOPS_AGE_KEY"].endswith("ALREADYSET00000000000000000000000000000000000000")


def test_ensure_age_key_env_noop_when_no_file(cli, tmp_path, monkeypatch):
    monkeypatch.delenv("SOPS_AGE_KEY", raising=False)
    cli.ensure_age_key_env(key_file=tmp_path / "absent.txt")
    import os
    assert not os.environ.get("SOPS_AGE_KEY")


def test_required_binaries_cover_vault_and_ansible(cli):
    for b in ("ansible-playbook", "sops", "age-keygen"):
        assert b in cli.REQUIRED_BINARIES


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


def _seed_hosts_yml(inv_dir: Path, ansible_host: str) -> None:
    import yaml

    inv_dir.mkdir(parents=True, exist_ok=True)
    (inv_dir / "hosts.yml").write_text(
        yaml.safe_dump(
            {"all": {"children": {"vps": {"hosts": {
                "host1": {"ansible_host": ansible_host, "ansible_user": "ops"},
            }}}}},
            sort_keys=False,
        )
    )


def test_host_tailnet_ip_reads_vps_host(cli, tmp_path, monkeypatch):
    inv = tmp_path / "inventory" / "prod"
    _seed_hosts_yml(inv, "100.77.16.46")
    monkeypatch.setattr(cli, "inventory_path", lambda name: tmp_path / "inventory" / name)
    assert cli._host_tailnet_ip("prod") == "100.77.16.46"


def test_host_tailnet_ip_skips_placeholder(cli, tmp_path, monkeypatch):
    inv = tmp_path / "inventory" / "prod"
    _seed_hosts_yml(inv, "0.0.0.0")
    monkeypatch.setattr(cli, "inventory_path", lambda name: tmp_path / "inventory" / name)
    assert cli._host_tailnet_ip("prod") == ""


def test_host_tailnet_ip_missing_file(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "inventory_path", lambda name: tmp_path / "inventory" / name)
    assert cli._host_tailnet_ip("prod") == ""


def test_ensure_dokploy_api_key_skips_when_present(cli, tmp_path, monkeypatch):
    from helpers import sops_vault

    inv = tmp_path / "inventory" / "prod"
    (inv / "group_vars" / "all").mkdir(parents=True)
    (inv / "group_vars" / "all" / "vault.sops.yml").write_text("encrypted")
    monkeypatch.setattr(cli, "inventory_path", lambda name: tmp_path / "inventory" / name)
    monkeypatch.setattr(sops_vault, "read_value", lambda *a, **k: "a-real-dokploy-api-key")
    # No second pass needed; must NOT shell out to the bootstrap helper.
    called = []
    monkeypatch.setattr(cli, "_run", lambda cmd: called.append(cmd))
    assert cli._ensure_dokploy_api_key("prod") is False
    assert called == []


def test_ensure_dokploy_api_key_mints_when_absent(cli, tmp_path, monkeypatch):
    from helpers import sops_vault

    inv = tmp_path / "inventory" / "prod"
    (inv / "group_vars" / "all").mkdir(parents=True)
    (inv / "group_vars" / "all" / "vault.sops.yml").write_text("encrypted")
    _seed_hosts_yml(inv, "100.77.16.46")
    monkeypatch.setattr(cli, "inventory_path", lambda name: tmp_path / "inventory" / name)
    monkeypatch.setattr(sops_vault, "read_value", lambda *a, **k: "")
    called = []
    monkeypatch.setattr(cli, "_run", lambda cmd: called.append(cmd))
    assert cli._ensure_dokploy_api_key("prod") is True
    assert len(called) == 1
    cmd = called[0]
    assert cmd[1].endswith("helpers/bootstrap_dokploy_admin.py")
    assert "--tailnet-ip" in cmd
    assert cmd[cmd.index("--tailnet-ip") + 1] == "100.77.16.46"
    assert cmd[cmd.index("--vault") + 1].endswith("vault.sops.yml")


def test_ensure_dokploy_api_key_dies_without_tailnet_ip(cli, tmp_path, monkeypatch):
    from helpers import sops_vault

    inv = tmp_path / "inventory" / "prod"
    (inv / "group_vars" / "all").mkdir(parents=True)
    (inv / "group_vars" / "all" / "vault.sops.yml").write_text("encrypted")
    _seed_hosts_yml(inv, "0.0.0.0")  # placeholder -> no usable tailnet IP
    monkeypatch.setattr(cli, "inventory_path", lambda name: tmp_path / "inventory" / name)
    monkeypatch.setattr(sops_vault, "read_value", lambda *a, **k: "")
    with pytest.raises(SystemExit):
        cli._ensure_dokploy_api_key("prod")
