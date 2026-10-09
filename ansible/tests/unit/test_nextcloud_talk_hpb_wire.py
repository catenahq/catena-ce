"""The Talk wiring registers the hosts the Nextcloud template gives talk-hpb.

The template sets SIGNALING_HOSTNAME and TURN_HOSTNAME once in the stack
environment and hands them to both the app container and talk-hpb (its
TALK_HOST and TURN_DOMAIN). The wire script reads them from the app container,
so the signaling URL Talk dials is the host talk-hpb answers as, whatever the
Nextcloud host is called.

Run: uv run pytest tests/unit/test_nextcloud_talk_hpb_wire.py
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "nextcloud-talk-hpb-wire.sh"

ENV = {
    "NEXTCLOUD_HOSTNAME": "files.example.com",
    "SIGNALING_HOSTNAME": "signaling.example.com",
    "SIGNALING_SECRET": "sig-secret",
    "TURN_HOSTNAME": "turn.example.com",
    "TURN_STATIC_AUTH_SECRET": "turn-secret",
}


def _run(tmp_path: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the script against a fake docker: `printenv` answers from `env`, the
    signaling probe succeeds, coturn runs, and every occ call is logged."""
    envdir = tmp_path / "env"
    envdir.mkdir()
    for name, value in env.items():
        (envdir / name).write_text(value)
    occ_log = tmp_path / "occ"
    occ_log.touch()
    (tmp_path / "catena-container").write_text("#!/bin/bash\necho nc_app.1.x\n")
    (tmp_path / "docker").write_text(f"""#!/bin/bash
case "$1" in
    service) echo coturn; exit 0 ;;
esac
shift
if [ "$1" = "--user" ]; then
    printf '%s\\n' "${{*:6}}" >> {occ_log}
    exit 0
fi
case "$4" in
    printenv*) name=${{4#printenv \\"}}; name=${{name%\\"}}
               [ -f "{envdir}/$name" ] || exit 1
               cat "{envdir}/$name" ;;
    curl*) exit 0 ;;
esac
""")
    for fake in ("catena-container", "docker"):
        (tmp_path / fake).chmod(0o755)
    script = tmp_path / "wire.sh"
    script.write_text(SCRIPT.read_text().replace(
        "/usr/local/bin/catena-container", str(tmp_path / "catena-container")))
    out = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, check=False,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})
    return out, occ_log.read_text().splitlines()


def test_signaling_is_registered_at_the_host_the_template_sets(tmp_path: Path) -> None:
    out, occ = _run(tmp_path, ENV)
    assert out.returncode == 0, out.stderr
    assert "talk:signaling:add https://signaling.example.com sig-secret" in occ
    assert not [line for line in occ if "files.example.com" in line], occ


def test_turn_and_stun_are_registered_at_the_template_turn_host(tmp_path: Path) -> None:
    out, occ = _run(tmp_path, ENV)
    assert out.returncode == 0, out.stderr
    assert "talk:turn:add turn.example.com:5349 udp,tcp --secret=turn-secret" in occ
    assert "talk:stun:add turn.example.com:3478" in occ


@pytest.mark.parametrize("name", ["SIGNALING_HOSTNAME", "TURN_HOSTNAME"])
def test_a_missing_host_is_refused_before_any_occ_call(tmp_path: Path, name: str) -> None:
    out, occ = _run(tmp_path, {k: v for k, v in ENV.items() if k != name})
    assert out.returncode == 2
    assert f"  - {name}" in out.stderr
    assert occ == []
