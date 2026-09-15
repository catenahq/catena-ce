"""Every host script must search the payload's lib before its own directory.

Import resolution among these scripts is by DIRECTORY, not by package: each one
inserts its own dirname on sys.path and then imports its neighbours by bare
name. That is why they sit flat in one directory rather than in a package, and
it is also why moving one of them into the catena-admin payload is dangerous in
a way that does not announce itself.

A half-moved cluster still WORKS. The moved module lands in
/usr/local/lib/catena; the ones that have not moved yet are still in
/usr/local/bin beside the orchestrator; and a script that searched its own
directory first would keep importing the stale siblings. Green, running the
previous release's logic -- the worst outcome available, because nothing fails.

Putting the payload's lib first makes its copy win whenever there is one. On a
host where the payload ships no lib the entry resolves nothing and the search
falls through to the script's own directory, leaving a single search path.

Run: uv run pytest tests/unit/test_host_script_import_path.py
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
PAYLOAD_LIB = "/usr/local/lib/catena"


def _inserters() -> list[Path]:
    """Scripts that manipulate sys.path at all."""
    out = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if "sys.path" in path.read_text():
            out.append(path)
    return out


def test_there_are_scripts_to_check():
    """Guards the guard: an empty set would pass every assertion below.

    The catena-admin payload carries most of this cluster. What this repo ships
    is the public-ports reconciler, which is genuinely converge-owned: its
    correct value changes when the HOST's declared ports change, not when the
    product does."""
    names = [p.name for p in _inserters()]
    assert names, "no host script pins sys.path any more"
    assert "catena-public-ports.py" in names, names


@pytest.mark.parametrize("script", _inserters(), ids=lambda p: p.name)
def test_the_payload_lib_is_searched_first(script: Path):
    body = script.read_text()
    assert PAYLOAD_LIB in body, (
        f"{script.name} pins sys.path without the payload's lib, so a module "
        "moved into the payload is invisible to it"
    )
    # The old single-entry form: own directory only, at index 0. A script left
    # on it resolves the stale sibling in /usr/local/bin ahead of the moved one.
    assert "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))" not in body, (
        f"{script.name} is back on the single-entry form"
    )


@pytest.mark.parametrize("script", _inserters(), ids=lambda p: p.name)
def test_the_script_still_parses(script: Path):
    ast.parse(script.read_text())


def test_the_resolved_order_puts_the_payload_first(tmp_path):
    """Behavioural, not textual. The ordering is the whole property, and a
    comment saying so is not the thing that holds it.

    Run from a FILE, because the block reads __file__ -- which is the point:
    the script's own directory is one of the two entries."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        + _extract_path_block(SCRIPTS / "catena-public-ports.py")
        + "\nprint('\\n'.join(sys.path[:2]))\n"
    )
    lib = tmp_path / "payload-lib"
    out = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True, text=True, check=True,
        env=dict(os.environ, CATENA_PAYLOAD_LIB=str(lib)),
    ).stdout.splitlines()
    assert out[0] == str(lib), (
        f"sys.path[0] = {out[0]!r}; the payload's lib must win over the "
        "script's own directory or a moved module never takes effect"
    )
    assert out[1] == str(tmp_path), (
        f"sys.path[1] = {out[1]!r}, want the script's own directory -- the "
        "modules that have not moved yet still have to resolve"
    )


def _extract_path_block(script: Path) -> str:
    """The `for _d in (...)` loop, lifted verbatim.

    Running the real lines rather than a copy of them: a probe that restated
    the loop would prove the probe orders correctly and nothing about the
    script."""
    lines = script.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("for _d in ("))
    end = start
    while not lines[end].strip().startswith("sys.path.insert"):
        end += 1
    return "\n".join(lines[start:end + 1])
