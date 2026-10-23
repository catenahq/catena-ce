"""The installer stays out of the public panel image, and carries only the
tree files it uses.

Why both rules hold is in installer/README.md.

Run: uv run pytest installer/tests/test_the_installer_ships_where_it_belongs.py
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from catena_installer import install, tree

REPO = Path(__file__).resolve().parents[2]
ANSIBLE = REPO / "ansible"


def _force_include() -> dict[str, str]:
    doc = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return doc["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


def test_the_installer_is_not_under_ansible():
    """Under ansible/ it would ship into the public panel image, because the
    vendor step copies that tracked tree."""
    assert (REPO / "installer" / "catena_installer").is_dir()
    assert not (ANSIBLE / "installer").exists()
    assert not (ANSIBLE / "catena_installer").exists()


def test_a_wheel_carries_every_tree_file_the_installer_reads_or_sends():
    """In a checkout tree.TREE is ansible/; in a wheel it is the copies these
    entries make. A file the installer uses and the wheel lacks is an install
    that works from a checkout and fails from PyPI."""
    carried = {src: dst for src, dst in _force_include().items()}
    for src, dst in carried.items():
        assert src.startswith("ansible/"), src
        assert dst == "catena_installer/tree/" + src.removeprefix("ansible/"), (src, dst)
    needed = ["seed.py", "skel/hosts.yml.example", "helpers/knobs.yml",
              "helpers/render_knobs.py", "helpers/onbox_config.py", *tree.FETCHER]
    for rel in needed:
        assert any(rel == s.removeprefix("ansible/")
                   or rel.startswith(s.removeprefix("ansible/") + "/")
                   for s in carried), rel
        assert (ANSIBLE / rel).is_file(), rel


def test_the_starter_ships_inside_the_package():
    assert tree.STARTER == Path(install.__file__).parent / "starter.sh"
    assert tree.STARTER.is_file()


def test_the_registry_is_read_from_the_tree_not_copied_here():
    """A copy under installer/ would be a second registry, and the one a
    client meets first."""
    assert not list((REPO / "installer" / "catena_installer").glob("knobs.*"))
    source = tree.module("helpers.render_knobs").SOURCE
    assert source.is_relative_to(tree.TREE)


def test_a_checkout_keeps_its_inventories_beside_the_tree(monkeypatch):
    monkeypatch.delenv("CATENA_INVENTORY_ROOT", raising=False)
    assert tree.inventory_root() == ANSIBLE / "inventory"
    monkeypatch.setenv("CATENA_INVENTORY_ROOT", "/srv/inventories")
    assert tree.inventory_root() == Path("/srv/inventories")
