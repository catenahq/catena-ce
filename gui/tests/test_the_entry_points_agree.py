"""The two entry points exist, resolve to code, and agree with each other.

`catena-cli` (ansible/) and `catena-gui` (gui/) are what a client runs, from
the repository root through the root pyproject or from each project. A name is
held in several places -- two project declarations, the root project that
gathers them, the launcher that runs the CLI, and every page that tells a
client what to type -- and each of those is a place it can drift. This gate
reads all of them.

The locks are held to the pyprojects by CI (`uv lock --check` in the root,
ansible/ and gui/), since resolving them needs the package index.

Run: uv run pytest tests/test_the_entry_points_agree.py
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

from catena_gui import registry, render

REPO = Path(__file__).resolve().parents[2]
ANSIBLE = REPO / "ansible"
GUI = REPO / "gui"

# What each project declares, and nothing else.
CLI = ("catena-cli", "catena_cli:_entry")
LAUNCHER = ("catena-gui", "catena_gui.__main__:main")

# The pages that tell a client or an operator what to run.
DOCS = (REPO / "README.md", ANSIBLE / "README.md", GUI / "README.md",
        ANSIBLE / "inventory" / "README.md")


def _pyproject(path: Path) -> dict:
    return tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))


def _defines(source: Path, function: str) -> bool:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return any(isinstance(node, ast.FunctionDef) and node.name == function
               for node in tree.body)


def test_each_project_declares_its_one_entry_point():
    assert _pyproject(ANSIBLE)["project"]["scripts"] == {CLI[0]: CLI[1]}
    assert _pyproject(GUI)["project"]["scripts"] == {LAUNCHER[0]: LAUNCHER[1]}


def test_each_entry_point_resolves_to_a_function_in_the_code():
    """A script whose target moved or was renamed installs cleanly and fails
    the first time a client runs it."""
    module, function = CLI[1].split(":")
    assert module in _pyproject(ANSIBLE)["tool"]["setuptools"]["py-modules"]
    assert _defines(ANSIBLE / f"{module}.py", function), CLI[1]

    module, function = LAUNCHER[1].split(":")
    package = module.split(".")[0]
    assert package in _pyproject(GUI)["tool"]["setuptools"]["packages"]
    assert _defines(GUI / Path(*module.split(".")).with_suffix(".py"), function), \
        LAUNCHER[1]


def test_the_root_project_gathers_both():
    """`uv run catena-cli` and `uv run catena-gui` from the root are the root
    project installing both, editable, from their own directories."""
    root = _pyproject(REPO)
    wanted = {_pyproject(ANSIBLE)["project"]["name"]: "ansible",
              _pyproject(GUI)["project"]["name"]: "gui"}
    assert set(root["project"]["dependencies"]) == set(wanted)
    sources = root["tool"]["uv"]["sources"]
    for name, path in wanted.items():
        assert sources[name] == {"path": path, "editable": True}, name
    assert root["tool"]["uv"]["package"] is False


def test_the_launcher_runs_the_cli_by_its_declared_name():
    assert registry.cli_script() == CLI[0]
    argv = render.install_command(ANSIBLE, Path("/tmp/install.yaml"), "clientco")
    assert argv[:5] == ["uv", "run", "--project", str(ANSIBLE), CLI[0]]


def test_the_cli_accepts_what_the_launcher_passes():
    """The launcher's arguments are the CLI's contract: a flag the CLI renamed
    would make every graphical install exit on a usage error."""
    if str(ANSIBLE) not in sys.path:
        sys.path.insert(0, str(ANSIBLE))
    import catena_cli

    argv = render.install_command(ANSIBLE, Path("/tmp/install.yaml"), "clientco")
    args = catena_cli.build_parser().parse_args(argv[5:])
    assert args.func is catena_cli.cmd_install
    assert (args.inventory, args.input, args.no_confirm) == (
        "clientco", "/tmp/install.yaml", True)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_every_documented_command_is_a_declared_entry_point(doc):
    """What a page tells a client to type is what exists. `uv run catena ...`
    after a rename is a command that fails on the first line of an install."""
    declared = {CLI[0], LAUNCHER[0]}
    named = set(re.findall(r"uv run (catena[\w-]*)", doc.read_text(encoding="utf-8")))
    assert named <= declared, f"{doc.name} names {sorted(named - declared)}"
