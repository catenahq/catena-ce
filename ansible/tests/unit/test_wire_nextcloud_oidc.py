"""The Nextcloud OIDC wiring runs its provider command only against the
user_oidc release line the template names.

user_oidc comes from the app store: app:enable installs its newest release
for the running Nextcloud, and each core upgrade moves it on. The template
names the release the provider command's flags are written for
(NEXTCLOUD_USER_OIDC_VERSION on the app container); the script accepts an
installed release of that major version, at that release or newer.

Run: uv run pytest tests/unit/test_wire_nextcloud_oidc.py
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wire-nextcloud-oidc.sh"

ENV = {
    "NEXTCLOUD_OIDC_CLIENT_ID": "nextcloud",
    "NEXTCLOUD_OIDC_CLIENT_SECRET": "oidc-secret",
    "NEXTCLOUD_OIDC_ISSUER_URL": "https://id.example.com/realms/vps",
    "NEXTCLOUD_USER_OIDC_VERSION": "8.11.0",
}


def _run(tmp_path: Path, env: dict[str, str],
         installed: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the script against a fake docker: `printenv` answers from `env`,
    user_oidc's installed_version is `installed`, and every occ call is
    logged."""
    envdir = tmp_path / "env"
    envdir.mkdir()
    for name, value in env.items():
        (envdir / name).write_text(value)
    occ_log = tmp_path / "occ"
    occ_log.touch()
    (tmp_path / "catena-container").write_text("#!/bin/bash\necho nc_app.1.x\n")
    (tmp_path / "docker").write_text(f"""#!/bin/bash
shift
if [ "$1" = "--user" ]; then
    printf '%s\\n' "${{*:6}}" >> {occ_log}
    if [ "$6" = "config:app:get" ]; then
        echo {installed}
    fi
    exit 0
fi
case "$4" in
    printenv*) name=${{4#printenv \\"}}; name=${{name%\\"}}
               [ -f "{envdir}/$name" ] || exit 1
               cat "{envdir}/$name" ;;
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


def _provider_calls(occ: list[str]) -> list[str]:
    return [line for line in occ if line.startswith("user_oidc:provider keycloak")]


@pytest.mark.parametrize("installed", ["8.11.0", "8.12.3"])
def test_the_named_release_or_a_newer_one_of_its_major_is_wired(
        tmp_path: Path, installed: str) -> None:
    out, occ = _run(tmp_path, ENV, installed)
    assert out.returncode == 0, out.stderr
    assert occ[0] == "app:enable user_oidc"
    assert len(_provider_calls(occ)) == 1, occ


@pytest.mark.parametrize("installed", ["9.0.0", "8.10.0"])
def test_another_major_or_an_older_release_is_refused_before_the_provider_command(
        tmp_path: Path, installed: str) -> None:
    out, occ = _run(tmp_path, ENV, installed)
    assert out.returncode == 3
    assert f"user_oidc {installed} is installed" in out.stderr
    assert "written for user_oidc 8.11.0" in out.stderr
    assert _provider_calls(occ) == []


def test_a_stack_that_names_no_release_is_refused_before_any_occ_call(
        tmp_path: Path) -> None:
    env = {k: v for k, v in ENV.items() if k != "NEXTCLOUD_USER_OIDC_VERSION"}
    out, occ = _run(tmp_path, env, "8.11.0")
    assert out.returncode == 2
    assert "names no NEXTCLOUD_USER_OIDC_VERSION" in out.stderr
    assert occ == []
