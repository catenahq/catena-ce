"""Every script finds an application's container through one lookup.

catena-container resolves <app> <service> by the swarm service name every task
container carries, `com.docker.swarm.service.name=<app>_<service>`.

Run: uv run pytest tests/unit/test_catena_container_lookup.py
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
LOOKUP = SCRIPTS / "catena-container.sh"
CALLERS = sorted(path for path in SCRIPTS.glob("*.sh") if path != LOOKUP)


def _run(tmp_path: Path, docker_body: str, *args: str) -> subprocess.CompletedProcess:
    fake = tmp_path / "docker"
    fake.write_text(f"#!/bin/bash\nprintf '%s\\n' \"$@\" > {tmp_path}/argv\n{docker_body}\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    return subprocess.run(["bash", str(LOOKUP), *args], env=env,
                          capture_output=True, text=True, check=False)


def test_it_selects_the_desired_task_over_an_orphan(tmp_path: Path) -> None:
    out = _run(tmp_path, """if [ "$1" = ps ]; then
  printf 'nc_app.1.orphan\\nnc_app.1.live\\n'
else
  printf 'nc_app.1.live\\n'
fi""", "catena-nextcloud", "app")
    assert (out.returncode, out.stdout) == (0, "nc_app.1.live\n")
    argv = (tmp_path / "argv").read_text().splitlines()
    assert "catena-nextcloud_app" in argv
    assert "desired-state=running" in argv
    assert "--no-trunc" in argv


def test_failed_service_query_is_not_absent(tmp_path: Path) -> None:
    out = _run(tmp_path, 'if [ "$1" = ps ]; then printf "orphan\\n"; else exit 1; fi',
               "--service", "catena-postgres")
    assert out.returncode == 2
    assert "docker service ps failed" in out.stderr


def test_only_orphan_is_absent(tmp_path: Path) -> None:
    out = _run(tmp_path, 'if [ "$1" = ps ]; then printf "orphan\\n"; else printf "live\\n"; fi',
               "--service", "catena-postgres")
    assert (out.returncode, out.stdout.strip()) == (0, "")


def test_not_running_prints_nothing(tmp_path: Path) -> None:
    out = _run(tmp_path, "exit 0", "catena-nextcloud", "app")
    assert (out.returncode, out.stdout.strip()) == (0, "")


def test_required_says_how_to_deploy_on_stderr(tmp_path: Path) -> None:
    out = _run(tmp_path, "exit 0", "--required", "catena-nextcloud", "app")
    assert out.returncode == 1
    assert out.stdout == ""
    assert "The catena-nextcloud application is not running on this host." in out.stderr
    assert "Deploy it first from the panel's Apps page" in out.stderr


@pytest.mark.parametrize("args", [(), ("--required",)])
def test_a_failed_docker_ps_is_not_an_absent_app(tmp_path: Path, args) -> None:
    out = _run(tmp_path, "exit 1", *args, "catena-nextcloud", "app")
    assert out.returncode == 2
    assert "docker ps failed" in out.stderr


@pytest.mark.parametrize("args", [(), ("catena-nextcloud",), ("--required", "catena-nextcloud")])
def test_it_needs_an_app_and_a_service(tmp_path: Path, args) -> None:
    out = _run(tmp_path, "exit 0", *args)
    assert out.returncode == 64
    assert "usage" in out.stderr


@pytest.mark.parametrize("script", CALLERS, ids=lambda p: p.name)
def test_no_caller_carries_its_own_copy_of_the_lookup(script: Path) -> None:
    text = script.read_text()
    assert "com.docker.swarm.service.name=" not in text, (
        f"{script.name} carries its own copy of the container lookup")
