"""One office wiring script serves both editors: each panel button passes its
editor, and anything else is refused before the host is touched.

Run: uv run pytest tests/unit/test_nextcloud_office_wiring.py
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

ANSIBLE = Path(__file__).resolve().parents[2]
SCRIPT = ANSIBLE / "scripts" / "wire-nextcloud-office.sh"
CATALOG = (ANSIBLE / "reconcile" / "roles" / "catena-admin" / "templates"
           / "actions.yml.j2")
INSTALL = (ANSIBLE / "reconcile" / "roles" / "infrastructure" / "tasks"
           / "nextcloud_office.yml")
COMMAND = "/usr/local/bin/catena-wire-nextcloud-office"


def _buttons() -> dict[str, str]:
    catalog = yaml.safe_load(CATALOG.read_text().split("\n", 1)[1])
    return {a["name"]: a["shell"] for a in catalog["actions"]}


@pytest.mark.parametrize("editor", ["collabora", "onlyoffice"])
def test_each_button_passes_its_editor(editor: str) -> None:
    assert _buttons()[f"wire-nextcloud-{editor}"] == f"{COMMAND} {editor}"


def test_the_converge_installs_the_command_the_buttons_run() -> None:
    copy = yaml.safe_load(INSTALL.read_text())[0]["ansible.builtin.copy"]
    assert copy["dest"] == COMMAND
    assert copy["src"].endswith("/scripts/wire-nextcloud-office.sh")


FAKE_DOCKER = """#!/bin/bash
echo "$*" >> "$FAKE_LOG"
case "$1" in
    inspect) printf '%s\\n' "$FAKE_OFFICE_HOST" ;;
    exec)
        case "$*" in
            *collabora:9980*) [ "$FAKE_EDITOR" = collabora ] && echo '<wopi-discovery>' || exit 1 ;;
            *documentserver/healthcheck*) [ "$FAKE_EDITOR" = onlyoffice ] && echo true || exit 1 ;;
            *printenv*) echo a-jwt-secret ;;
            *app:list*) echo '{}' ;;
        esac ;;
esac
"""

FAKE_CATENA_CONTAINER = """#!/bin/bash
case "${@: -1}" in
    app) echo nextcloud-ct ;;
    *) echo office-ct ;;
esac
"""


def _wire(tmp_path: Path, editor: str, office_host: str) -> tuple[subprocess.CompletedProcess, str]:
    """Run the script against a fake docker and a fake catena-container, with
    `editor` serving and `office_host` as its container's vps.route.host."""
    for name, body in (("docker", FAKE_DOCKER), ("catena-container", FAKE_CATENA_CONTAINER)):
        path = tmp_path / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "docker.log"
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "CATENA_CONTAINER": str(tmp_path / "catena-container"),
           "FAKE_LOG": str(log), "FAKE_EDITOR": editor, "FAKE_OFFICE_HOST": office_host}
    out = subprocess.run(["bash", str(SCRIPT), editor], capture_output=True,
                         text=True, check=False, env=env)
    return out, log.read_text() if log.exists() else ""


def test_collabora_is_wired_to_the_route_of_its_container(tmp_path: Path) -> None:
    out, calls = _wire(tmp_path, "collabora", "edit.cloud.example.com")
    assert out.returncode == 0, out.stderr
    assert "inspect -f {{index .Config.Labels \"vps.route.host\"}} office-ct" in calls
    assert "config:app:set richdocuments wopi_url --value=https://edit.cloud.example.com" in calls
    assert "config:app:set richdocuments public_wopi_url --value=https://edit.cloud.example.com" in calls


def test_onlyoffice_is_wired_to_the_route_of_its_container(tmp_path: Path) -> None:
    out, calls = _wire(tmp_path, "onlyoffice", "edit.cloud.example.com")
    assert out.returncode == 0, out.stderr
    assert "config:app:set onlyoffice DocumentServerUrl --value=https://edit.cloud.example.com/" in calls


@pytest.mark.parametrize("editor", ["collabora", "onlyoffice"])
def test_an_office_container_without_a_route_label_is_refused_unchanged(
        tmp_path: Path, editor: str) -> None:
    out, calls = _wire(tmp_path, editor, "")
    assert out.returncode != 0
    assert "vps.route.host" in out.stderr
    assert "config:app:set" not in calls
    assert "app:install" not in calls


@pytest.mark.parametrize("args", [[], ["libreoffice"]])
def test_an_unknown_editor_is_refused_before_any_lookup(args: list[str]) -> None:
    out = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                         text=True, check=False)
    assert out.returncode == 2
    assert "usage:" in out.stderr
    assert out.stdout == ""
