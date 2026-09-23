"""Turn a run's answers into the contract the installer already takes.

NO NEW MIDDLE LAYER. `install.yaml` plus `catena install -i ... --no-confirm`
is the declarative, non-interactive contract, and seed.py already live-probes
what it is given. The launcher is a THIRD producer of that file, beside a
person writing it and the test bench rendering it -- not a second way to
install.

That is what makes the acceptance test possible: a host the launcher built has
to be indistinguishable from one `catena install -i install.yaml` built,
because it IS one.

THE SECRETS RIDE THE FILE AND THE FILE IS TRANSIENT. seed reads them, writes
the ones the converge adopts into its own 0600 map, and the CLI deletes that.
This writer creates the install.yaml 0600 and its caller removes it, so the
window in which a client's cloud credentials exist on their disk is the length
of one install rather than the life of a run directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

# Answers the launcher keeps for itself. They describe the RUN rather than the
# install -- an acknowledgement, a page position -- and seed would file an
# unknown key as .env config, which is how a wizard's bookkeeping ends up in a
# client's inventory.
_LAUNCHER_ONLY = ("_keyset_acknowledged", "_step")


def install_yaml(*, inventory: str, answers: dict[str, str],
                 secrets: dict[str, str], host_name: str = "") -> str:
    """The flat install.yaml shape seed accepts.

    FLAT, not nested. seed splits a flat file by asking the registry which
    names are secrets rather than by reading a `vault_` prefix, which is the
    same question this launcher asked to decide what not to write to disk. One
    classification, so a credential cannot be filed as config by either.
    """
    doc: dict[str, object] = {"inventory": inventory}
    if host_name:
        doc["host_name"] = host_name
    for key, value in answers.items():
        if key in _LAUNCHER_ONLY or key.startswith("_"):
            continue
        if not str(value).strip():
            continue
        doc[key] = value
    for key, value in secrets.items():
        if str(value).strip():
            doc[key] = value
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)


def write_install_yaml(path: Path, body: str) -> None:
    """0600 from creation, never afterwards.

    A chmod after the write leaves a window in which the file holding a
    client's Cloudflare token and their S3 keys is world-readable, and on a
    shared machine that window is the whole exposure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)


def install_command(ansible_dir: Path, install_yaml_path: Path,
                    inventory: str) -> list[str]:
    """The argv the launcher runs.

    `--no-confirm` because the confirmation already happened: a client walked
    six pages and pressed the button. A second prompt on a process whose
    console they may have closed would stop the install and look like a hang.

    The inventory name rides as an explicit flag rather than being left to the
    file, so the directory the run writes into is the one the launcher named.
    """
    return [
        "uv", "run", "--project", str(ansible_dir),
        "catena", "install",
        "--inventory", inventory,
        "-i", str(install_yaml_path),
        "--no-confirm",
    ]
