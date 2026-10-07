"""install-host.sh: the install, run on the server from the release's own tree.

What it refuses before touching anything, and the order of the chain, which
is where the lockout rule lives: the accounts leg closes nothing, and the
system leg proves its session before the first playbook hardens sshd.

Run: uv run pytest tests/unit/test_install_host.py
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ANSIBLE = Path(__file__).resolve().parents[2]
SCRIPT = ANSIBLE / "install-host.sh"
STARTER = ANSIBLE.parent / "installer" / "catena_installer" / "starter.sh"

FULL = ["--leg", "system", "--image", "ghcr.io/catenahq/catena-admin:v1.2.3@sha256:" + "0" * 64,
        "--name", "prod1", "--ops-user", "ops", "--ops-key", "/tmp/ops.pub",
        "--env-file", "/tmp/env"]


def _run(*args: str, script: Path = SCRIPT) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True)


@pytest.mark.parametrize("args, said", [
    (["--leg", "nonsense"], "--leg must be accounts, system or uninstall"),
    (["--leg", "system", "--bogus", "x"], "unknown argument: --bogus"),
    (FULL[:2], "--image is required"),
    ([a for a in FULL if a not in ("--ops-key", "/tmp/ops.pub")], "--ops-key is required"),
])
def test_a_wrong_call_is_refused_before_anything_runs(args, said):
    done = _run(*args)
    assert done.returncode == 2
    assert said in done.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="runs as root")
def test_it_runs_as_root_only():
    done = _run(*FULL)
    assert done.returncode == 2 and "run as root" in done.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="runs as root")
def test_the_starter_runs_as_root_only():
    done = _run("--", *FULL, script=STARTER)
    assert done.returncode == 2 and "run as root" in done.stderr


PROOF = 'python3 "$ansible_dir/helpers/session_proof.py"'


def _at(text: str, needle: str) -> int:
    assert needle in text, needle
    return text.index(needle)


def test_the_chain_runs_in_order_and_proves_the_session_first():
    text = SCRIPT.read_text()
    accounts = _at(text, "play accounts.yml")
    proof = _at(text, PROOF)
    bootstrap = _at(text, "play bootstrap.yml")
    keyset = _at(text, "play show-keyset.yml")
    converge = _at(text, "play converge.yml")
    pin = _at(text, "catena-stack-update-managed pin --image")
    validate = _at(text, "play validate.yml")
    assert accounts < proof < bootstrap < keyset < converge < pin < validate


def test_the_accounts_leg_stops_before_the_proof():
    """It runs through the provider's login: nothing after the accounts may
    run on that leg."""
    text = SCRIPT.read_text()
    leg = text[_at(text, 'if [ "$leg" = accounts ]'):_at(text, PROOF)]
    assert "exit 0" in leg


def test_a_scoped_converge_records_no_version():
    """--tags applies part of a release, which is not one to record."""
    text = SCRIPT.read_text()
    guard = text[_at(text, 'if [ -z "$tags" ]'):]
    assert guard.index("catena-stack-update-managed pin") < guard.index("fi")


def test_the_image_reaches_the_converge_for_the_panel_and_the_engines():
    text = SCRIPT.read_text()
    assert 'export CATENA_ADMIN_IMAGE="$image" CATENA_PAYLOAD_IMAGE="$image"' in text


def test_the_starter_holds_nothing_version_specific():
    """It must start the install of any release: it fetches and hands over
    to the fetched tree's own install-host.sh."""
    text = STARTER.read_text()
    assert "fetch_release.py" in text
    assert 'usr/local/share/catena-ce/ansible/install-host.sh' in text
    assert "ansible-playbook" not in text and "playbooks/" not in text
