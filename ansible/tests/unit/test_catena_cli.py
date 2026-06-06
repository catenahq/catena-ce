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
