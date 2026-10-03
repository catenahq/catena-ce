"""Every Nextcloud wire script finds the app container through one lookup.

Run: uv run pytest tests/unit/test_nextcloud_container_lookup.py
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
LOOKUP = SCRIPTS / "nextcloud-container.sh"
WIRE_SCRIPTS = sorted(SCRIPTS.glob("wire-nextcloud-*.sh")) + [
    SCRIPTS / "nextcloud-talk-hpb-wire.sh"]


def _run(tmp_path: Path, docker_body: str, *args: str) -> subprocess.CompletedProcess:
    fake = tmp_path / "docker"
    fake.write_text(f"#!/bin/bash\n{docker_body}\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    return subprocess.run(["bash", str(LOOKUP), *args], env=env,
                          capture_output=True, text=True, check=False)


def test_it_prints_the_first_app_container(tmp_path: Path) -> None:
    out = _run(tmp_path, "printf 'nc_app.1.a\\nnc_app.2.b\\n'")
    assert (out.returncode, out.stdout) == (0, "nc_app.1.a\n")


def test_not_deployed_prints_nothing(tmp_path: Path) -> None:
    out = _run(tmp_path, "exit 0")
    assert (out.returncode, out.stdout.strip()) == (0, "")


def test_required_says_how_to_deploy_on_stderr(tmp_path: Path) -> None:
    out = _run(tmp_path, "exit 0", "--required")
    assert out.returncode == 1
    assert out.stdout == ""
    assert "Nextcloud is not running on this host." in out.stderr
    assert "nextcloud-s3" in out.stderr


@pytest.mark.parametrize("args", [(), ("--required",)])
def test_a_failed_docker_ps_is_not_an_absent_stack(tmp_path: Path, args) -> None:
    out = _run(tmp_path, "exit 1", *args)
    assert out.returncode == 2
    assert "docker ps failed" in out.stderr


def test_there_are_wire_scripts_to_check() -> None:
    assert len(WIRE_SCRIPTS) == 7, WIRE_SCRIPTS


@pytest.mark.parametrize("script", WIRE_SCRIPTS, ids=lambda p: p.name)
def test_each_wire_script_uses_the_shared_lookup(script: Path) -> None:
    text = script.read_text()
    assert "/usr/local/bin/catena-nextcloud-container" in text
    assert "label=vps.app=catena-nextcloud" not in text, (
        f"{script.name} carries its own copy of the container lookup")
