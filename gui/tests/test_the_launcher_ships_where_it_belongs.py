"""The launcher stays out of the public panel image, and beside the CLI.

TWO PLACEMENT RULES, both of which a later edit could quietly break.

`scripts/vendor-catena-ce.sh` in catena-admin copies `git ls-files -- ansible`
into the image the panel ships as. Anything under `ansible/` therefore travels
into a public image. The launcher holds a client's cloud credentials in memory
and writes an install.yaml; it has no business there, and it is at the repo
root with its own pyproject for exactly that reason.

And it is a PEER of the CLI rather than a replacement. The bench drives
`catena install -i ... --no-confirm`, and converge, recover, rollback and
show-keyset are operator verbs that should not need a browser.

Run: uv run pytest tests/test_the_launcher_ships_where_it_belongs.py
"""
from __future__ import annotations

from pathlib import Path

import pytest

from catena_gui import registry, server

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
    """A peer, not a replacement. The bench drives `catena install`, and the
    operator verbs should not need a browser."""
    cli = (REPO / "ansible" / "catena_cli.py").read_text(encoding="utf-8")
    for verb in ("install", "converge", "recover", "rollback", "show-keyset"):
        assert verb in cli, f"the CLI lost {verb}"


def test_the_registry_is_read_from_the_sibling_tree_not_copied_here():
    """A copy under gui/ would be a second registry, and the one that drifted
    would be the one a client meets first."""
    assert not (GUI / "catena_gui" / "knobs.json").exists()
    assert registry.registry_path().is_relative_to(REPO / "ansible")


def test_recover_and_rollback_are_shown_disabled_rather_than_hidden():
    """`catena recover` re-prompts the whole recovery keyset, because nothing
    off the server holds a copy -- a different wizard with different inputs
    rather than a variant of install. Hiding the verbs would make the launcher
    look like the whole tool."""
    labels = [label for label, _ in server.DISABLED_TABS]
    assert "Recover" in labels and "Roll back" in labels
    for _label, why in server.DISABLED_TABS:
        assert "catena " in why, (
            "a disabled tab that does not name what to use instead is a dead "
            "end with a tooltip")


def test_a_missing_registry_raises_rather_than_rendering_blank_pages(monkeypatch,
                                                                     tmp_path):
    monkeypatch.setenv("CATENA_KNOBS", str(tmp_path / "absent.json"))
    with pytest.raises(FileNotFoundError):
        registry.registry_path()
