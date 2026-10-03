"""Turn a run's answers into the contract the installer already takes.

NO NEW MIDDLE LAYER. `install.yaml` plus `catena-cli install -i ... --no-confirm`
is the declarative, non-interactive contract, and seed.py already live-probes
what it is given. The launcher is a THIRD producer of that file, beside a
person writing it and the test bench rendering it -- not a second way to
install.

That is what makes the acceptance test possible: a host the launcher built has
to be indistinguishable from one `catena-cli install -i install.yaml` built,
because it IS one.

THE PASSWORD RIDES THE FILE AND THE FILE IS TRANSIENT. The provider's
password, when the server needs it, is read by seed and handed to the key
install. This writer creates the install.yaml 0600 in a fresh 0700 directory
outside the inventory and removes both when the install ends, so the window in
which it exists on a client's disk is the length of one install, and never
inside the inventory a client keeps.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import yaml

from . import registry


def install_yaml(*, inventory: str, answers: dict[str, str],
                 secrets: dict[str, str], host_name: str = "") -> str:
    """The flat install.yaml shape seed accepts.

    FLAT, not nested. seed files a `host_`-prefixed key under the host -- the
    provider's password is `host_initial_password` -- and splits the rest by
    asking the registry which names are secrets, the same question this
    launcher asked to decide what not to write to disk. One classification, so
    a credential cannot be filed as config by either.
    """
    doc: dict[str, object] = {"inventory": inventory}
    if host_name:
        doc["host_name"] = host_name
    for key, value in answers.items():
        if not str(value).strip():
            continue
        doc[key] = value
    for key, value in secrets.items():
        if str(value).strip():
            doc[key] = value
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)


def write_install_yaml(path: Path, body: str) -> None:
    """0600 from creation, never afterwards.

    A chmod after the write leaves a window in which the file holding the
    provider's password is world-readable, and on a shared machine that window
    is the whole exposure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)


@contextmanager
def transient_install_yaml(body: str) -> Iterator[Path]:
    """The install.yaml, in a fresh 0700 directory, removed with it on exit."""
    workdir = Path(tempfile.mkdtemp(prefix="catena-gui-"))
    target = workdir / "install.yaml"
    try:
        write_install_yaml(target, body)
        yield target
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def install_command(ansible_dir: Path, install_yaml_path: Path,
                    inventory: str, *, keyset_json: bool = False) -> list[str]:
    """The argv the launcher runs: the installer CLI, by the script name its own
    pyproject gives it, in its own project.

    `--no-confirm` because the confirmation already happened: a client checked
    the sections and pressed Install. A second prompt on a process whose
    console they may have closed would stop the install and look like a hang.

    The inventory name rides as an explicit flag rather than being left to the
    file, so the directory the run writes into is the one the launcher named.
    `keyset_json` has the CLI print the passwords as values the page shows one
    by one.
    """
    return [
        "uv", "run", "--project", str(ansible_dir),
        registry.cli_script(), "install",
        "--inventory", inventory,
        "-i", str(install_yaml_path),
        "--no-confirm",
        *(["--keyset-json"] if keyset_json else []),
    ]
