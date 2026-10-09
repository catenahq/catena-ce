"""The Jitsi wiring sets the host the Rocket.Chat template gives jitsi-web.

The template builds the rocketchat service's JITSI_HOSTNAME and jitsi-web's
PUBLIC_URL from the same stack variable. The wire script reads JITSI_HOSTNAME
from the rocketchat container and sets it as Rocket.Chat's Jitsi_Domain, so a
call opens on the host jitsi-web answers as, whatever the Rocket.Chat host is
called.

Run: uv run pytest tests/unit/test_rocketchat_jitsi_wire.py
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rocketchat-jitsi-wire.sh"

ENV = {
    "ROOT_URL": "https://chat.example.com",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASS": "admin-pass",
    "JITSI_HOSTNAME": "meet.example.com",
}


def _run(tmp_path: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    """Run the script against a fake docker: `printenv` answers from `env`,
    coturn runs, the login succeeds, and every setting call is logged."""
    envdir = tmp_path / "env"
    envdir.mkdir()
    for name, value in env.items():
        (envdir / name).write_text(value)
    calls = tmp_path / "settings"
    calls.touch()
    (tmp_path / "catena-container").write_text("#!/bin/bash\necho rc_rocketchat.1.x\n")
    (tmp_path / "docker").write_text(f"""#!/bin/bash
case "$1" in
    service) echo coturn; exit 0 ;;
esac
shift
case "$4" in
    printenv*) name=${{4#printenv \\"}}; name=${{name%\\"}}
               [ -f "{envdir}/$name" ] || exit 1
               cat "{envdir}/$name" ;;
    *api/v1/login*) echo '{{"status":"success","data":{{"authToken":"tok","userId":"uid"}}}}' ;;
    *api/v1/settings/*) printf '%s' "$4" | tr '\\n' ' ' >> {calls}; echo >> {calls} ;;
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
    settings = {}
    for line in calls.read_text().splitlines():
        key = re.search(r"/api/v1/settings/(\w+)", line).group(1)
        settings[key] = re.search(r"-d '([^']*)'", line).group(1)
    return out, settings


def test_jitsi_domain_is_the_host_the_template_sets(tmp_path: Path) -> None:
    out, settings = _run(tmp_path, ENV)
    assert out.returncode == 0, out.stderr
    assert settings["Jitsi_Domain"] == '{"value":"meet.example.com"}'
    assert settings["Jitsi_Enabled"] == '{"value":true}'


def test_a_missing_jitsi_host_is_refused_before_any_setting(tmp_path: Path) -> None:
    out, settings = _run(tmp_path, {k: v for k, v in ENV.items() if k != "JITSI_HOSTNAME"})
    assert out.returncode == 2
    assert "  - JITSI_HOSTNAME" in out.stderr
    assert "Stacks > catena-rocketchat" in out.stderr
    assert settings == {}


@pytest.mark.parametrize("name", ["ROOT_URL", "ADMIN_USERNAME", "ADMIN_PASS"])
def test_a_missing_sign_in_value_is_refused(tmp_path: Path, name: str) -> None:
    out, settings = _run(tmp_path, {k: v for k, v in ENV.items() if k != name})
    assert out.returncode == 2
    assert f"  - {name}" in out.stderr
    assert settings == {}
