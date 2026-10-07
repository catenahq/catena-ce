"""The installer's one entry point exists, resolves to code, and is what every
page tells a client or an operator to type.

`catena-installer` is the root project's console script: `uvx
catena-installer` from PyPI, `uv run catena-installer` in a checkout. The
name is held in the declaration, in the code it points at and in every page
that names a command, and each of those is a place it can drift.

The locks are held to the pyprojects by CI (`uv lock --check`), since
resolving them needs the package index.

Run: uv run pytest installer/tests/test_the_entry_points_agree.py
"""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

from catena_installer import __main__ as main_mod

REPO = Path(__file__).resolve().parents[2]
ENTRY = ("catena-installer", "catena_installer.__main__:main")

# The pages that tell a client or an operator what to run.
DOCS = (REPO / "README.md", REPO / "ansible" / "README.md",
        REPO / "installer" / "README.md", REPO / "ansible" / "inventory" / "README.md")


def _root() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_root_project_is_the_installer_and_declares_one_entry_point():
    root = _root()
    assert root["project"]["name"] == ENTRY[0]
    assert root["project"]["scripts"] == {ENTRY[0]: ENTRY[1]}


def test_the_entry_point_resolves_to_a_function_in_the_code():
    """A script whose target does not resolve installs cleanly and fails the
    first time a client runs it."""
    module, function = ENTRY[1].split(":")
    assert _root()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "installer/catena_installer"]
    source = REPO / "installer" / Path(*module.split(".")).with_suffix(".py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.FunctionDef) and n.name == function for n in tree.body)


def test_no_arguments_opens_the_page_and_a_verb_runs_in_the_console():
    parser = main_mod.build_parser()
    assert parser.parse_args([]).command is None
    args = parser.parse_args(["install", "--inventory", "clientco", "--release", "v1.2.3"])
    assert (args.func, args.inventory, args.release) == (
        main_mod.cmd_install, "clientco", "v1.2.3")
    for verb, func in (("connect", main_mod.cmd_connect), ("init", main_mod.cmd_init),
                       ("uninstall", main_mod.cmd_uninstall)):
        assert parser.parse_args([verb, "--inventory", "c"]).func is func


def test_a_release_and_an_image_are_one_choice():
    with pytest.raises(SystemExit):
        main_mod.build_parser().parse_args(
            ["install", "--inventory", "c", "--release", "v1", "--image", "x"])


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_documented_command_is_the_entry_point(doc):
    """What a page tells a client to type is what exists. `uv run catena ...`
    after a rename is a command that fails on the first line of an install."""
    named = set(re.findall(r"(?:uv run|uvx) (catena[\w-]*)", doc.read_text(encoding="utf-8")))
    assert named <= {ENTRY[0]}, f"{doc.name} names {sorted(named - {ENTRY[0]})}"
