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


@pytest.mark.parametrize("args", [[], ["libreoffice"]])
def test_an_unknown_editor_is_refused_before_any_lookup(args: list[str]) -> None:
    out = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                         text=True, check=False)
    assert out.returncode == 2
    assert "usage:" in out.stderr
    assert out.stdout == ""
