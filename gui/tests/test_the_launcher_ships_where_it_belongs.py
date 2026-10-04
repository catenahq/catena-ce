"""The launcher stays out of the public panel image, and beside the CLI.

Why both rules hold is in gui/README.md.

Run: uv run pytest tests/test_the_launcher_ships_where_it_belongs.py
"""
from __future__ import annotations

from pathlib import Path

from catena_gui import registry

REPO = Path(__file__).resolve().parents[2]
GUI = REPO / "gui"


def test_the_launcher_is_not_under_ansible():
    """Under ansible/ it would ship into the public panel image, because the
    vendor script copies that whole tracked tree."""
    assert GUI.parent == REPO, f"the launcher moved to {GUI}"
    assert not (REPO / "ansible" / "gui").exists()
    assert not (REPO / "ansible" / "catena_gui").exists()


def test_it_has_its_own_project_rather_than_joining_the_installers():
    """Its own dependency set, and its own console script. Folding it into the
    installer's pyproject would put a UI dependency in the package every
    converge installs."""
    assert (GUI / "pyproject.toml").is_file()
    body = (GUI / "pyproject.toml").read_text(encoding="utf-8")
    assert "catena-gui" in body
    assert "catena_gui.__main__:main" in body


def test_the_cli_is_untouched():
    """A peer, not a replacement. The bench drives `catena-cli install`, and the
    operator verbs should not need a browser."""
    cli = (REPO / "ansible" / "catena_cli.py").read_text(encoding="utf-8")
    for verb in ("install", "converge", "uninstall"):
        assert verb in cli, f"the CLI lost {verb}"


def test_the_registry_is_read_from_the_sibling_tree_not_copied_here():
    """A copy under gui/ would be a second registry, and the one a client meets
    first."""
    assert not list((GUI / "catena_gui").glob("knobs.*"))
    source = registry.ansible_module("helpers.render_knobs").SOURCE
    assert source.is_relative_to(REPO / "ansible")
