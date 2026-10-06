"""The knob registry, as the launcher reads it: ansible/helpers/knobs.yml,
loaded and validated by the sibling tree's render_knobs.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Where the ansible tree sits relative to this file: gui/catena_gui/ -> gui/ ->
# the repo root. A checkout is the only layout this runs in, because the thing
# it drives (`catena-cli install`) is in that same checkout.
_REPO_ROOT = Path(__file__).resolve().parents[2]
ANSIBLE_DIR = _REPO_ROOT / "ansible"


def ansible_module(name: str):
    """A module of the sibling ansible/ tree (seed, catena_cli), imported from
    there."""
    ansible = str(ANSIBLE_DIR)
    if ansible not in sys.path:
        sys.path.insert(0, ansible)
    return importlib.import_module(name)


def load() -> dict:
    return ansible_module("helpers.render_knobs").load()


def steps(doc: dict) -> list[dict]:
    """The installer's sections, in order."""
    return list(doc.get("gui_steps") or [])


def step_fields(doc: dict, step: str) -> list[dict]:
    """What one section asks for: secrets first, then the values they
    configure, the order the settings page uses too."""
    entries = [*(doc.get("secrets") or []), *(doc.get("config") or [])]
    return [entry for entry in entries if entry.get("step") == step]


def is_secret(doc: dict, key: str) -> bool:
    return any(entry.get("key") == key for entry in doc.get("secrets") or [])


def default_for(entry: dict) -> str:
    """What a field starts filled with.

    From the `.env` default when the knob has one, because that is the same
    value `catena-cli init` writes. A knob with none starts blank, which for
    every one of them is a real answer.
    """
    return str((entry.get("env") or {}).get("default") or "")


def example_for(entry: dict) -> str:
    """What a blank field shows greyed out, never as its value: the registry's
    `env.example`."""
    return str((entry.get("env") or {}).get("example") or "")


def cli_script() -> str:
    """The console-script name `ansible/pyproject.toml` gives the installer CLI.

    Read, not written down here: the launcher runs `catena-cli install`, and a
    name held in two places is a name that drifts. The one whose target is the
    CLI's entry function is the one, whatever it is called."""
    import tomllib

    doc = tomllib.loads((ANSIBLE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    for name, target in (doc.get("project", {}).get("scripts") or {}).items():
        if target == CLI_ENTRY:
            return name
    raise LookupError(f"ansible/pyproject.toml declares no script for {CLI_ENTRY}")


# The installer CLI's entry function, as `[project.scripts]` names it.
CLI_ENTRY = "catena_cli:_entry"


def is_required(entry: dict) -> bool:
    """Whether the installer refuses to install without a value. Everything
    else is labelled optional."""
    return bool(entry.get("required"))


def choice_for(entry: dict) -> dict:
    """The labels of the choice the launcher puts in front of a field, by
    option (`public`, `private`) and language, from the registry's
    `gui_choice`, or empty. `public` leaves the field empty; `private` asks
    for it."""
    return dict(entry.get("gui_choice") or {})


def options_for(entry: dict) -> list[str]:
    """The values a field is limited to, from its `env.options`, or an empty
    list for free text."""
    return list((entry.get("env") or {}).get("options") or [])


def _ssh_keys(ssh_dir: Path) -> list[str]:
    """The private keys in `ssh_dir` that have their public half beside them,
    written with `~` the way a client types them."""
    if not ssh_dir.is_dir():
        return []
    home = Path.home()
    out = []
    for pub in sorted(ssh_dir.glob("*.pub")):
        private = pub.with_suffix("")
        if private.is_file():
            try:
                out.append("~/" + str(private.relative_to(home)))
            except ValueError:
                out.append(str(private))
    return out


def suggestions_for(entry: dict, ssh_dir: Path | None = None) -> list[str]:
    """Values the launcher suggests beside a free-text field, from the named
    source in `gui_suggestions_from`. `ssh_keys` is the pairs already in
    ~/.ssh: the one whose public half the provider installed is among them."""
    if entry.get("gui_suggestions_from") == "ssh_keys":
        return _ssh_keys(ssh_dir or Path.home() / ".ssh")
    return []
