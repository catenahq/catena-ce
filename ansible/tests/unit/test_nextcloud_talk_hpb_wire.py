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


def _run(
    tmp_path: Path,
    env: dict[str, str],
    *,
    talk_present: bool = True,
    store_down: bool = False,
    signaling: bool = True,
    install_answer_lost: bool = False,
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the script against a fake docker: `printenv` answers from `env`, the
    signaling probe succeeds unless `signaling` is False, coturn runs, every occ
    call is logged, and Talk is installed unless `talk_present` is False
    (`app:install` then installs it, fails when `store_down`, or installs it and
    exits 1 when `install_answer_lost`)."""
    envdir = tmp_path / "env"
    envdir.mkdir()
    for name, value in env.items():
        (envdir / name).write_text(value)
    occ_log = tmp_path / "occ"
    occ_log.touch()
    talk = tmp_path / "talk-installed"
    if talk_present:
        talk.touch()
    (tmp_path / "catena-container").write_text("#!/bin/bash\necho nc_app.1.x\n")
    (tmp_path / "docker").write_text(f"""#!/bin/bash
case "$1" in
    service) echo coturn; exit 0 ;;
esac
shift
if [ "$1" = "--user" ]; then
    printf '%s\\n' "${{*:6}}" >> {occ_log}
    case "${{*:6}}" in
        "app:getpath spreed") [ -f {talk} ]; exit $? ;;
        "app:install spreed")
            [ -f {tmp_path}/store-down ] && {{ echo "Could not download app spreed" >&2; exit 1; }}
            touch {talk}
            [ -f {tmp_path}/answer-lost ] && exit 1
            exit 0 ;;
    esac
    exit 0
fi
case "$4" in
    printenv*) name=${{4#printenv \\"}}; name=${{name%\\"}}
               [ -f "{envdir}/$name" ] || exit 1
               cat "{envdir}/$name" ;;
    curl*) [ -f {tmp_path}/no-signaling ]; exit $(( $? == 0 )) ;;
esac
""")
    if store_down:
        (tmp_path / "store-down").touch()
    if install_answer_lost:
        (tmp_path / "answer-lost").touch()
    if not signaling:
        (tmp_path / "no-signaling").touch()
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
    assert "Stacks > catena-nextcloud" in out.stderr
    assert occ == []


def test_absent_talk_is_installed_and_enabled_before_it_is_wired(tmp_path: Path) -> None:
    out, occ = _run(tmp_path, ENV, talk_present=False)
    assert out.returncode == 0, out.stderr
    steps = ["app:getpath spreed", "app:install spreed", "app:enable spreed"]
    positions = [occ.index(step) for step in steps]
    assert positions == sorted(positions)
    assert positions[-1] < next(i for i, line in enumerate(occ) if line.startswith("talk:turn:add"))


def test_present_talk_is_enabled_without_an_install(tmp_path: Path) -> None:
    out, occ = _run(tmp_path, ENV)
    assert out.returncode == 0, out.stderr
    assert "app:enable spreed" in occ
    assert "app:install spreed" not in occ


def test_an_unreachable_app_store_stops_the_run_before_any_wiring(tmp_path: Path) -> None:
    out, occ = _run(tmp_path, ENV, talk_present=False, store_down=True)
    assert out.returncode == 5
    assert "apps.nextcloud.com" in out.stderr
    assert "Could not download app spreed" in out.stderr
    assert "Nothing has been changed" in out.stderr
    assert not [line for line in occ if line.startswith(("talk:", "config:", "app:enable"))], occ


def test_without_signaling_talk_is_ensured_then_the_run_ends_cleanly(tmp_path: Path) -> None:
    out, occ = _run(tmp_path, ENV, talk_present=False, signaling=False)
    assert out.returncode == 0, out.stderr
    assert occ == ["app:getpath spreed", "app:install spreed", "app:enable spreed"]


def test_an_install_answer_lost_after_talk_arrived_still_wires(tmp_path: Path) -> None:
    """`app:install` exits 1 but Talk is there on the second look: the run goes on."""
    out, occ = _run(tmp_path, ENV, talk_present=False, install_answer_lost=True)
    assert out.returncode == 0, out.stderr
    assert occ.count("app:getpath spreed") == 2
    enable = occ.index("app:enable spreed")
    assert enable < next(i for i, line in enumerate(occ) if line.startswith("talk:turn:add"))
    assert any(line.startswith("talk:signaling:add") for line in occ[enable:])
